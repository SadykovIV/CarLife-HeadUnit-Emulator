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
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile

PROJECT = Path(__file__).resolve().parents[1]
VERSION = "0.4.1"
NAME = f"CarLife-HeadUnit-Emulator-{VERSION}-Windows-x64"
_release, _, _rc = VERSION.partition("-rc.")
RELEASE_CHANNEL = "diagnostic-test" if _rc else "stable"
PE_FLAGS = 0x02 if _rc else 0  # VS_FF_PRERELEASE only for RCs.
PE_VERSION = ",".join((*_release.split("."), _rc or "0"))
# ctypes resolves these at runtime, so they are not covered by PE import tables.
REQUIRED_USB_EXPORTS = tuple("libusb_" + name for name in (
    "get_version", "init", "exit", "get_device_list", "free_device_list", "get_session_data",
    "get_device_descriptor", "get_bus_number", "get_device_address", "get_port_numbers",
    "get_active_config_descriptor", "free_config_descriptor", "open", "close",
    "claim_interface", "release_interface", "control_transfer", "get_string_descriptor_ascii",
    "bulk_transfer"))
WINDOWS_USB_MODULES = ("hu_lab.windows_accessory", "hu_lab.windows_usb_interfaces", "hu_lab.windows_winusb",
                       "hu_lab.windows_driver_setup", "hu_lab.windows_setup_launcher")
WINDOWS_USB_SYSTEM_EXPORTS = {
    "winusb.dll": ("WinUsb_Initialize", "WinUsb_Free", "WinUsb_GetDescriptor",
                   "WinUsb_GetCurrentAlternateSetting", "WinUsb_QueryInterfaceSettings",
                   "WinUsb_QueryPipe", "WinUsb_ReadPipe", "WinUsb_WritePipe",
                   "WinUsb_ControlTransfer", "WinUsb_GetOverlappedResult"),
    "kernel32.dll": ("CreateFileW", "CreateEventW", "CloseHandle", "WaitForSingleObject", "CancelIoEx",
                     "GetExitCodeProcess"),
    "shell32.dll": ("ShellExecuteExW",),
    "cfgmgr32.dll": ("CM_Get_Device_ID_Size", "CM_Get_Device_IDW", "CM_Locate_DevNodeW",
                     "CM_Get_Parent", "CM_Get_Child", "CM_Get_Sibling", "CM_Get_DevNode_Registry_PropertyW",
                     "CM_Enumerate_Classes", "CM_Get_Device_Interface_List_SizeW", "CM_Get_Device_Interface_ListW"),
}
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
    paths = list((PROJECT / "hu_lab").rglob("*.py"))
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


def verify_usb_library(library, record):
    digest = sha256(library)
    if digest != record["dll_sha256"]:
        raise RuntimeError("Packaged libusb DLL differs from the pinned Windows binary")
    listing = subprocess.run([llvm_tool("llvm-readobj"), "--coff-exports", str(library)],
                             check=True, capture_output=True, text=True).stdout
    exports = set(re.findall(r"^\s*Name: (\S+)\s*$", listing, re.MULTILINE))
    missing = sorted(set(REQUIRED_USB_EXPORTS) - exports)
    if missing:
        raise RuntimeError("Missing libusb exports: " + ", ".join(missing))
    return {"name": library.name, "sha256": digest, "version": record["dll_version"],
            "required_exports": list(REQUIRED_USB_EXPORTS), "static_export_check": "PASS"}


def prepare_native_headers(archive, record, work):
    """Materialize only the pinned upstream Windows headers; nothing is installed on the host."""
    if sha256(archive) != record["sha256"]:
        raise RuntimeError("Windows headers archive hash mismatch")
    source, output, prefix = work / "sdk-source", work / "sdk-build", work / "sdk"
    for folder in (source, output, prefix):
        folder.mkdir(parents=True, exist_ok=True)
    member_prefix = f"mingw-w64-{record['commit']}/mingw-w64-headers/"
    with tarfile.open(archive, "r:gz") as upstream:
        for member in upstream:
            if not member.name.startswith(member_prefix) or not member.isfile():
                continue
            rel = Path(member.name[len(member_prefix):])
            if rel.is_absolute() or ".." in rel.parts:
                raise RuntimeError("Unsafe Windows SDK archive member")
            file = source / rel
            file.parent.mkdir(parents=True, exist_ok=True)
            with upstream.extractfile(member) as src, file.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            file.chmod(0o755 if member.mode & 0o111 else 0o644)
    subprocess.run([str(source / "configure"), "--host=x86_64-w64-mingw32", "--without-widl",
                    "--enable-sdk=all", "--enable-crt", "--prefix=" + str(prefix)],
                   cwd=output, stdout=subprocess.DEVNULL, check=True)
    subprocess.run(["make", "install"], cwd=output, stdout=subprocess.DEVNULL, check=True)
    include = prefix / "include"
    for name in ("windows.h", "setupapi.h", "newdev.h", "cfgmgr32.h", "winusb.h"):
        if not (include / name).is_file():
            raise RuntimeError(f"Missing required Windows SDK header: {name}")
    return include


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
        f'1 VERSIONINFO\nFILEVERSION {PE_VERSION}\nPRODUCTVERSION {PE_VERSION}\n'
        f'FILEFLAGSMASK 0x3F\nFILEFLAGS 0x{PE_FLAGS:02X}\nFILEOS 0x40004\nFILETYPE 1\nBEGIN\n'
        'BLOCK "StringFileInfo" BEGIN BLOCK "040904B0" BEGIN\n'
        'VALUE "FileDescription", "CarLife HeadUnit Emulator"\nVALUE "ProductName", "CarLife HeadUnit Emulator"\n'
        f'VALUE "FileVersion", "{VERSION}"\nVALUE "ProductVersion", "{VERSION}"\n'
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


def compile_driver_setup(stage, work, header_archive, header_record):
    """Link a standalone x64 EXE: never elevate the interpreter or a script."""
    name = "CarLifeWinUsbSetup.exe"
    include = prepare_native_headers(header_archive, header_record, work / "native-sdk")
    helper = work / "helper"
    helper.mkdir()
    # The only imported DLL at process startup is a Windows KnownDLL. All
    # installer APIs are loaded by the native helper from System32 explicitly.
    exports = ("SetDefaultDllDirectories", "GetSystemDirectoryW", "GetSystemWindowsDirectoryW", "LoadLibraryExW", "GetProcAddress",
               "GetLastError", "GetCommandLineW", "ExitProcess", "CompareStringOrdinal", "LocalFree", "Sleep")
    (helper / "kernel32.def").write_text("LIBRARY kernel32.dll\nEXPORTS\n" + "\n".join(exports) + "\n")
    subprocess.run([llvm_tool("llvm-dlltool"), "-m", "i386:x86-64", "-d", "kernel32.def", "-l", "kernel32.lib"],
                   check=True, cwd=helper)
    (helper / "setup.rc").write_text(
        '1 24 "setup.manifest"\n'
        f'1 VERSIONINFO\nFILEVERSION {PE_VERSION}\nPRODUCTVERSION {PE_VERSION}\n'
        f'FILEFLAGSMASK 0x3F\nFILEFLAGS 0x{PE_FLAGS:02X}\nFILEOS 0x40004\nFILETYPE 1\nBEGIN\n'
        'BLOCK "StringFileInfo" BEGIN BLOCK "040904B0" BEGIN\n'
        'VALUE "FileDescription", "CarLife HeadUnit Emulator - AOA MI_00 WinUSB setup"\n'
        'VALUE "ProductName", "CarLife HeadUnit Emulator"\n'
        f'VALUE "FileVersion", "{VERSION}"\nVALUE "ProductVersion", "{VERSION}"\n'
        f'VALUE "OriginalFilename", "{name}"\nEND END\n'
        'BLOCK "VarFileInfo" BEGIN VALUE "Translation", 0x409, 1200 END\nEND\n', encoding="ascii")
    (helper / "setup.manifest").write_text(
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0">'
        '<trustInfo xmlns="urn:schemas-microsoft-com:asm.v3"><security><requestedPrivileges>'
        '<requestedExecutionLevel level="asInvoker" uiAccess="false"/>'
        '</requestedPrivileges></security></trustInfo></assembly>', encoding="ascii")
    subprocess.run([llvm_tool("llvm-rc"), "/fo", "setup.res", "setup.rc"], cwd=helper, check=True)
    subprocess.run([llvm_tool("clang"), "--target=x86_64-w64-windows-gnu", "-isystem", str(include),
                    "-D_WIN32_WINNT=0x0A00", "-O2", "-ffreestanding",
                    "-fno-builtin", "-fno-stack-protector", "-mno-stack-arg-probe",
                    "-Werror=implicit-function-declaration", "-Werror=incompatible-pointer-types",
                    "-c", str(PROJECT / "windows/winusb_setup.c"), "-o", "setup.obj"],
                   cwd=helper, check=True)
    exe = stage / name
    subprocess.run([llvm_tool("lld-link"), "/machine:x64", "/subsystem:windows", "/entry:mainCRTStartup",
                    "/nodefaultlib", "/dynamicbase", "/nxcompat", "/highentropyva", "/timestamp:0",
                    "/stack:4194304,4194304", "/out:" + str(exe), "setup.obj", "setup.res", "kernel32.lib"],
                   cwd=helper, check=True)
    if {n.lower() for n in inspect_pe(exe)} != {"kernel32.dll"}:
        raise RuntimeError("Unexpected native driver helper startup import")
    return {"name": name, "sha256": sha256(exe), "bytes": exe.stat().st_size,
            "architecture": "x64", "startup_imports": ["kernel32.dll"], "authenticode_signed": False,
            "scope": "Exact AOA 18D1:2D01 MI_00 only, with user confirmation and Windows UAC"}


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
    # QtMultimedia imports the QtNetwork Python binding through Shiboken during
    # module initialization. Qt6Network.dll's PE dependency cannot retain this
    # separately loaded .pyd; both the binding and its DLL closure are required.
    selected.update(qt / f"{name}.pyd" for name in
                    ("QtCore", "QtGui", "QtWidgets", "QtNetwork", "QtMultimedia"))
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
    parser.add_argument("--headers-cache-dir", type=Path, default=PROJECT / "build" / "windows-sdk-cache")
    parser.add_argument("--dist-dir", type=Path, default=PROJECT / "dist")
    parser.add_argument("--download-only", action="store_true")
    parser.add_argument("--replace", action="store_true", help="Replace this builder's versioned output")
    args = parser.parse_args()
    args.cache_dir.mkdir(parents=True, exist_ok=True)
    dependencies = json.loads((PROJECT / "windows" / "dependencies.json").read_text())
    records = [dependencies["python"], *dependencies["wheels"]]
    with ThreadPoolExecutor(max_workers=4) as workers:
        list(workers.map(lambda record: download(record, args.cache_dir), records))
    args.headers_cache_dir.mkdir(parents=True, exist_ok=True)
    header_record = dependencies["build_headers"]
    header_archive = download(header_record, args.headers_cache_dir)
    if args.download_only:
        print("Verified all Windows runtime downloads.")
        return 0
    args.dist_dir.mkdir(parents=True, exist_ok=True)
    target = args.dist_dir.resolve() / NAME
    if target.exists() and not args.replace:
        parser.error(f"Output exists: {target}; pass --replace to rebuild it")
    before = snapshot()
    for module in WINDOWS_USB_MODULES:
        filename = module.replace(".", "/") + ".py"
        if filename not in before:
            raise RuntimeError(f"Missing required Windows USB module: {filename}")
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
        shutil.copy2(PROJECT / "windows" / "verify_usb_runtime.py", stage / "verify_usb_runtime.py")
        shutil.copy2(PROJECT / "macos" / "entry.py", stage / "bundle_entry.py")
        usb_dlls = list((packages / "libusb_package").rglob("libusb-1.0.dll"))
        if len(usb_dlls) != 1:
            raise RuntimeError(f"Expected one libusb DLL, found {usb_dlls}")
        shutil.copy2(usb_dlls[0], stage / "libusb-1.0.dll")
        usb_record = next(record for record in dependencies["wheels"] if record["name"] == "libusb-package")
        usb_library = verify_usb_library(stage / "libusb-1.0.dll", usb_record)
        licenses = stage / "licenses"
        licenses.mkdir()
        for name in ("LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md"):
            shutil.copy2(PROJECT / name, licenses / name)
        shutil.copy2(PROJECT / "hu_lab" / "assets" / "ICON_SOURCE.json", licenses / "Baidu-CarLife-artwork.json")
        shutil.copy2(PROJECT / "windows" / "README.ru.txt", stage / "README.ru.txt")
        (stage / "Verify.cmd").write_text('@echo off\r\n"%~dp0CarLifeHeadUnitEmulator.exe" --bundle-check --check-output "%TEMP%\\carlife-bundle-check.json"\r\nif errorlevel 1 exit /b 1\r\ntype "%TEMP%\\carlife-bundle-check.json"\r\n', encoding="ascii")
        (stage / "VerifyUsbRuntime.cmd").write_bytes(
            b'@echo off\r\n"%~dp0runtime\\python.exe" -X utf8 "%~dp0verify_usb_runtime.py" --output "%TEMP%\\carlife-libusb-check.json"\r\nexit /b %errorlevel%\r\n')
        compile_launcher(stage, work)
        driver_setup_helper = compile_driver_setup(stage, work, header_archive, header_record)
        # All extracted licenses and original distribution metadata stay present.
        native, system_imports = trim_qt_and_verify(stage)
        after = snapshot()
        if before != after:
            raise RuntimeError("Source changed during packaging; rerun after edits finish")
        info = {"name": "CarLife HeadUnit Emulator", "version": VERSION, "architecture": "x64", "platform": "win32",
                "python": dependencies["python"]["version"], "built_utc": datetime.now(timezone.utc).isoformat(),
                "release_channel": RELEASE_CHANNEL, "usb_library": usb_library,
                "driver_setup_helper": driver_setup_helper, "build_headers": header_record,
                "windows_usb": {"modules": list(WINDOWS_USB_MODULES),
                                "system_exports": WINDOWS_USB_SYSTEM_EXPORTS},
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
