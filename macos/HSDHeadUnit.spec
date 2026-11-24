# Native Apple Silicon app: source snapshot, CPython, Qt, PyAV and libusb.
from pathlib import Path
import json
import os
from macholib.MachO import MachO
from macholib.mach_o import LC_BUILD_VERSION, LC_VERSION_MIN_MACOSX

project = Path(SPECPATH).parent
assets = Path(os.environ["CARLIFE_BUILD_ASSETS"])
usb_library = Path(os.environ["CARLIFE_BUILD_LIBUSB"])

a = Analysis(
    [str(project / "macos" / "entry.py")],
    pathex=[str(project)],
    binaries=[(str(usb_library), "."), (str(assets / "libhsd_hicar_auth.dylib"), "hu_lab/hicar")],
    datas=[(str(assets / "build-info.json"), "."),
           (str(assets / "licenses"), "licenses"),
           (str(project / "hu_lab" / "assets"), "hu_lab/assets"),
           (str(assets / "HeadUnit.icns"), ".")],
    hiddenimports=["PySide6.QtCore", "PySide6.QtGui", "PySide6.QtWidgets",
                   "PySide6.QtMultimedia", "av"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "pytest", "IPython", "jupyter", "grpc_tools"],
    noarchive=False,
)
# Wheel tags alone are insufficient: Homebrew Python/libusb can target a newer OS.
minimum = 14 << 16
for destination, source, kind in a.binaries:
    try:
        binary = MachO(source)
    except (ValueError, OSError):
        continue
    for header in binary.headers:
        if header.header.cputype != 0x0100000C:  # CPU_TYPE_ARM64
            continue
        for command, data, extra in header.commands:
            if command.cmd == LC_BUILD_VERSION and data.platform == 1:
                minimum = max(minimum, data.minos)
            elif command.cmd == LC_VERSION_MIN_MACOSX:
                minimum = max(minimum, data.version)
minimum_macos = f"{minimum >> 16}.{(minimum >> 8) & 255}.{minimum & 255}"
metadata_path = assets / "build-info.json"
metadata = json.loads(metadata_path.read_text())
metadata["minimum_macos"] = minimum_macos
metadata_path.write_text(json.dumps(metadata, indent=2) + "\n")
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="HSD HeadUnit Emulator",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    argv_emulation=False,
    target_arch="arm64",
    codesign_identity=None,
    entitlements_file=None,
)
collection = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="HSD HeadUnit Emulator",
)
app = BUNDLE(
    collection,
    name="HSD HeadUnit Emulator.app",
    icon=str(assets / "HeadUnit.icns"),
    bundle_identifier="org.carlife.headunit.lab",
    info_plist={
        "CFBundleDisplayName": "HSD HeadUnit Emulator",
        "CFBundleShortVersionString": metadata["version"].split("-")[0],
        "CFBundleVersion": "14",
        "CarLifeVersion": metadata["version"],
        "LSMinimumSystemVersion": minimum_macos,
        "LSApplicationCategoryType": "public.app-category.developer-tools",
        "NSHighResolutionCapable": True,
        "NSHumanReadableCopyright": "HSD HeadUnit Emulator contributors; Apache-2.0",
        "NSLocalNetworkUsageDescription": "Connect to a phone for CarLife, CarbitLink and Huawei HiCar projection on the selected network.",
    },
)
