#!/usr/bin/env python3
"""Build an offline Windows x64 portable app on macOS or Linux with LLVM."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import shutil
import struct
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

PROJECT = Path(__file__).resolve().parents[1]
VERSION = "0.3.1"
NAME = f"CarLife-HeadUnit-Emulator-{VERSION}-Windows-x64"
SYSTEM_DLLS = set("""advapi32.dll authz.dll avicap32.dll avrt.dll bcrypt.dll cfgmgr32.dll
combase.dll comctl32.dll comdlg32.dll crypt32.dll cryptbase.dll cryptsp.dll d2d1.dll
d3d9.dll d3d11.dll d3d12.dll d3dcompiler_47.dll dcomp.dll dnsapi.dll dwmapi.dll
dwrite.dll dxgi.dll dxva2.dll evr.dll gdi32.dll hid.dll icu.dll icuuc.dll imagehlp.dll
imm32.dll iphlpapi.dll kernel32.dll mf.dll mfplat.dll mfreadwrite.dll mpr.dll
mscms.dll msvcrt.dll ncrypt.dll netapi32.dll normaliz.dll ntdll.dll ole32.dll
oleaut32.dll opengl32.dll powrprof.dll propsys.dll psapi.dll rpcrt4.dll secur32.dll
setupapi.dll shcore.dll shell32.dll shlwapi.dll ucrtbase.dll uiautomationcore.dll
urlmon.dll user32.dll userenv.dll usp10.dll uxtheme.dll version.dll winhttp.dll
wininet.dll winmm.dll winscard.dll winspool.drv wintrust.dll ws2_32.dll wtsapi32.dll""".split())


def is_system_dll(name):
    return name.lower() in SYSTEM_DLLS or name.lower().startswith(("api-ms-win-", "ext-ms-win-"))


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def download(record, cache):
    target = cache / record["filename"]
    if not target.is_file() or sha256(target) != record["sha256"]:
        partial = target.with_suffix(target.suffix + ".part")
        print("Download:", record["filename"], flush=True)
        with urllib.request.urlopen(record["url"], timeout=120) as response, partial.open("wb") as output:
            shutil.copyfileobj(response, output)
        if sha256(partial) != record["sha256"]:
            raise RuntimeError(f"Upstream SHA256 mismatch: {record['filename']}")
        partial.replace(target)
    return target


def extract(archive, destination):
    with zipfile.ZipFile(archive) as source:
        for member in source.infolist():
            path = Path(member.filename)
            if path.is_absolute() or ".." in path.parts:
                raise RuntimeError(f"Unsafe archive entry: {path}")
        source.extractall(destination)


def snapshot():
    paths = list((PROJECT / "hu_lab").glob("*.py"))
    paths += list((PROJECT / "hu_lab" / "assets").glob("*"))
    paths += list((PROJECT / "windows").glob("*"))
    paths += [PROJECT / "macos" / "entry.py", PROJECT / "macos" / "build.py"]
    return {str(path.relative_to(PROJECT)): sha256(path) for path in sorted(paths) if path.is_file()}


def llvm_tool(name):
    found = shutil.which(name)
    if found:
        return found
    for root in (Path("/opt/homebrew/opt/llvm/bin"), Path("/usr/local/opt/llvm/bin")):
        if (root / name).is_file():
            return str(root / name)
    raise RuntimeError(f"LLVM tool required: {name}")


def compile_launcher(stage, work):
    spec = importlib.util.spec_from_file_location("macos_builder", PROJECT / "macos" / "build.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.make_icon(work / "HeadUnit.ico")
    libraries = []
    for name, exports in {
        "kernel32": ["GetModuleFileNameW", "GetCommandLineW", "CreateProcessW", "WaitForSingleObject",
                     "GetExitCodeProcess", "CloseHandle", "ExitProcess"],
        "user32": ["MessageBoxW"],
    }.items():
        definition = work / (name + ".def")
        definition.write_text(f"LIBRARY {name}.dll\nEXPORTS\n" + "\n".join(exports) + "\n")
        library = work / (name + ".lib")
        subprocess.run([llvm_tool("llvm-dlltool"), "-m", "i386:x86-64", "-d", str(definition), "-l", str(library)], check=True)
        libraries.append(str(library))
    resource = work / "launcher.rc"
    resource.write_text('1 ICON "HeadUnit.ico"\n'
        '1 VERSIONINFO\nFILEVERSION 0,3,1,0\nPRODUCTVERSION 0,3,1,0\nFILEOS 0x40004\nFILETYPE 1\nBEGIN\n'
        'BLOCK "StringFileInfo" BEGIN BLOCK "040904B0" BEGIN\n'
        'VALUE "FileDescription", "CarLife HeadUnit Emulator"\nVALUE "ProductName", "CarLife HeadUnit Emulator"\n'
        'VALUE "FileVersion", "0.3.1"\nVALUE "ProductVersion", "0.3.1"\n'
        'VALUE "OriginalFilename", "CarLifeHeadUnitEmulator.exe"\nEND END\n'
        'BLOCK "VarFileInfo" BEGIN VALUE "Translation", 0x409, 1200 END\nEND\n')
    subprocess.run([llvm_tool("llvm-rc"), "/fo", "launcher.res", "launcher.rc"], cwd=work, check=True)
    subprocess.run([llvm_tool("clang"), "--target=x86_64-pc-windows-msvc", "-O2", "-ffreestanding",
                    "-fno-stack-protector", "-c", str(PROJECT / "windows" / "launcher.c"),
                    "-o", str(work / "launcher.obj")], check=True)
    subprocess.run([llvm_tool("lld-link"), "/machine:x64", "/subsystem:windows", "/entry:mainCRTStartup",
                    "/nodefaultlib", "/dynamicbase", "/nxcompat", "/highentropyva", "/timestamp:0",
                    "/out:" + str(stage / "CarLifeHeadUnitEmulator.exe"), str(work / "launcher.obj"),
                    str(work / "launcher.res"), *libraries], check=True)


def inspect_pe(path):
    """Read architecture and imported DLLs without executing Windows code."""
    data = path.read_bytes()
    if data[:2] != b"MZ":
        raise RuntimeError(f"Not a Windows binary: {path}")
    pe = struct.unpack_from("<I", data, 60)[0]
    if data[pe:pe + 4] != b"PE\0\0" or struct.unpack_from("<H", data, pe + 4)[0] != 0x8664:
        raise RuntimeError(f"Not a Windows x64 binary: {path}")
    sections, optional_size = struct.unpack_from("<H", data, pe + 6)[0], struct.unpack_from("<H", data, pe + 20)[0]
    optional = pe + 24
    if struct.unpack_from("<H", data, optional)[0] != 0x20b:
        raise RuntimeError(f"Not PE32+: {path}")
    section_start = optional + optional_size
    def offset(rva):
        for i in range(sections):
            vsize, va, size, raw = struct.unpack_from("<IIII", data, section_start + i * 40 + 8)
            if va <= rva < va + max(vsize, size):
                return raw + rva - va
        raise RuntimeError(f"Unmapped RVA {rva} in {path}")
    imports = []
    rva, size = struct.unpack_from("<II", data, optional + 120)
    if rva:
        position = offset(rva)
        while any(data[position:position + 20]):
            name_rva = struct.unpack_from("<I", data, position + 12)[0]
            begin = offset(name_rva)
            imports.append(data[begin:data.index(0, begin)].decode("ascii"))
            position += 20
    # Delay-loaded imports are dependencies too (for example Qt's media backend).
    rva, size = struct.unpack_from("<II", data, optional + 112 + 13 * 8)
    if rva:
        position = offset(rva)
        image_base = struct.unpack_from("<Q", data, optional + 24)[0]
        while any(data[position:position + 32]):
            attributes, name_rva = struct.unpack_from("<II", data, position)
            begin = offset(name_rva if attributes & 1 else name_rva - image_base)
            imports.append(data[begin:data.index(0, begin)].decode("ascii"))
            position += 32
    return imports


def trim_qt_and_verify(stage):
    """Package the used QWidget modules and complete native dependency closure."""
    qt = stage / "runtime" / "Lib" / "site-packages" / "PySide6"
    binaries = [p for p in stage.rglob("*") if p.is_file() and p.suffix.lower() in (".exe", ".dll", ".pyd")]
    by_name = {}
    for path in binaries:
        by_name.setdefault(path.name.lower(), []).append(path)
    selected = {p for p in binaries if not p.is_relative_to(qt)}
    selected.update(qt / f"{name}.pyd" for name in ("QtCore", "QtGui", "QtWidgets", "QtMultimedia"))
    # These are loaded by Qt's plugin manager, not in the PE import table.
    plugins = ("platforms/qwindows.dll", "platforms/qoffscreen.dll", "platforms/qminimal.dll",
               "styles/qmodernwindowsstyle.dll", "multimedia/windowsmediaplugin.dll",
               "multimedia/ffmpegmediaplugin.dll", "tls/qschannelbackend.dll",
               "tls/qcertonlybackend.dll", "networkinformation/qnetworklistmanager.dll",
               "iconengines/qsvgicon.dll")
    selected.update(qt / "plugins" / name for name in plugins)
    selected.update(p for p in (qt / "plugins" / "imageformats").glob("*.dll") if p.name != "qpdf.dll")
    selected.add(qt / "opengl32sw.dll")
    pending, imports, system = list(selected), {}, set()
    while pending:
        path = pending.pop()
        if path in imports:
            continue
        if not path.is_file():
            raise RuntimeError(f"Required runtime binary missing: {path}")
        imports[path] = inspect_pe(path)
        for name in imports[path]:
            candidates = by_name.get(name.lower(), [])
            if candidates:
                for dependency in candidates:
                    if dependency not in selected:
                        selected.add(dependency)
                        pending.append(dependency)
            elif is_system_dll(name):
                system.add(name.lower())
            else:
                raise RuntimeError(f"Unbundled non-system import: {path.relative_to(stage)} -> {name}")
    for path in binaries:
        if path not in selected:
            path.unlink()
    for folder in ("qml", "include", "typesystems", "glue", "doc", "examples", "scripts"):
        if (qt / folder).is_dir():
            shutil.rmtree(qt / folder)
    return {str(p.relative_to(stage)): names for p, names in sorted(imports.items())}, sorted(system)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", type=Path, default=PROJECT / "build" / "windows-downloads")
    parser.add_argument("--dist-dir", type=Path, default=PROJECT / "dist")
    parser.add_argument("--download-only", action="store_true")
    parser.add_argument("--replace", action="store_true", help="Replace this builder's versioned output")
    args = parser.parse_args()
    args.cache_dir.mkdir(parents=True, exist_ok=True)
    dependencies = json.loads((PROJECT / "windows" / "dependencies.json").read_text())
    records = [dependencies["python"], *dependencies["wheels"]]
    with ThreadPoolExecutor(max_workers=4) as workers:
        list(workers.map(lambda record: download(record, args.cache_dir), records))
    if args.download_only:
        print("Verified all Windows runtime downloads.")
        return 0
    args.dist_dir.mkdir(parents=True, exist_ok=True)
    target = args.dist_dir.resolve() / NAME
    if target.exists() and not args.replace:
        parser.error(f"Output exists: {target}; pass --replace to rebuild it")
    before = snapshot()
    work_root = PROJECT / "build" / "windows-headunit"
    work_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="package-", dir=work_root) as temporary:
        work = Path(temporary)
        stage = work / NAME
        runtime = stage / "runtime"
        packages = runtime / "Lib" / "site-packages"
        packages.mkdir(parents=True)
        extract(args.cache_dir / dependencies["python"]["filename"], runtime)
        for record in dependencies["wheels"]:
            extract(args.cache_dir / record["filename"], packages)
        (runtime / "python313._pth").write_text("python313.zip\n.\nLib/site-packages\n..\nimport site\n")
        shutil.copytree(PROJECT / "hu_lab", stage / "hu_lab", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shutil.copy2(PROJECT / "windows" / "entry.py", stage / "entry.py")
        shutil.copy2(PROJECT / "macos" / "entry.py", stage / "bundle_entry.py")
        usb_dlls = list((packages / "libusb_package").rglob("libusb-1.0.dll"))
        if len(usb_dlls) != 1:
            raise RuntimeError(f"Expected one libusb DLL, found {usb_dlls}")
        shutil.copy2(usb_dlls[0], stage / "libusb-1.0.dll")
        licenses = stage / "licenses"
        licenses.mkdir()
        for name in ("LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md"):
            shutil.copy2(PROJECT / name, licenses / name)
        shutil.copy2(PROJECT / "hu_lab" / "assets" / "ICON_SOURCE.json", licenses / "Baidu-CarLife-artwork.json")
        shutil.copy2(PROJECT / "windows" / "README.ru.txt", stage / "README.ru.txt")
        (stage / "Verify.cmd").write_text('@echo off\r\n"%~dp0CarLifeHeadUnitEmulator.exe" --bundle-check --check-output "%TEMP%\\carlife-bundle-check.json"\r\nif errorlevel 1 exit /b 1\r\ntype "%TEMP%\\carlife-bundle-check.json"\r\n', encoding="ascii")
        compile_launcher(stage, work)
        # All extracted licenses and original distribution metadata stay present.
        native, system_imports = trim_qt_and_verify(stage)
        after = snapshot()
        if before != after:
            raise RuntimeError("Source changed during packaging; rerun after edits finish")
        info = {"name": "CarLife HeadUnit Emulator", "version": VERSION, "architecture": "x64", "platform": "win32",
                "python": dependencies["python"]["version"], "built_utc": datetime.now(timezone.utc).isoformat(),
                "dependencies": {r["name"]: r["version"] for r in dependencies["wheels"]},
                "source_sha256": before, "downloads": records,
                "validation": {"native_x64_binaries": len(native), "source_unchanged": True,
                               "regular_and_delay_import_closure": "PASS", "system_imports": system_imports,
                               "windows_runtime_test": "not performed by cross-platform builder",
                               "hardware_usb_test": "not performed", "authenticode_signed": False},
                "native_imports": native}
        (stage / "build-info.json").write_text(json.dumps(info, indent=2) + "\n")
        if target.exists():
            shutil.rmtree(target)
        shutil.move(str(stage), target)
    archive = Path(shutil.make_archive(str(args.dist_dir.resolve() / NAME), "zip", args.dist_dir.resolve(), NAME))
    receipt = {"archive": archive.name, "bytes": archive.stat().st_size, "sha256": sha256(archive),
               "version": VERSION, "host": {"platform": sys.platform, "architecture": platform.machine()},
               "validation": info["validation"]}
    (args.dist_dir / (NAME + ".json")).write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
