"""Check the packaged Windows USB library and enumerate devices without opening them."""
import argparse
import ctypes
import hashlib
import importlib
import json
from pathlib import Path
import platform
import sys


class LibraryVersion(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint16) for name in ("major", "minor", "micro", "nano")]
    _fields_ += [("rc", ctypes.c_char_p), ("describe", ctypes.c_char_p)]


def check(root, report):
    from hu_lab.platform_support import bundled_libusb
    from hu_lab.usb import LibUSB

    report["stage"] = "verify_package"
    if sys.platform != "win32" or ctypes.sizeof(ctypes.c_void_p) != 8:
        raise RuntimeError("This check requires the packaged 64-bit Windows interpreter")
    info = json.loads((root / "build-info.json").read_text(encoding="utf-8"))
    expected = info["usb_library"]
    report["package_version"] = info["version"]
    library = root / "libusb-1.0.dll"
    resolved = bundled_libusb()
    if not resolved or Path(resolved).resolve() != library.resolve():
        raise RuntimeError("The runtime did not resolve the libusb DLL in the package root")
    with library.open("rb") as stream:
        report["library_sha256"] = hashlib.file_digest(stream, "sha256").hexdigest()
    if report["library_sha256"] != expected["sha256"]:
        raise RuntimeError("Packaged libusb DLL hash mismatch")

    # Verify the dynamically resolved Windows dependencies used by the
    # accessory function opener without invoking any of their USB operations.
    report["stage"] = "bind_windows_system_apis"
    report["system_exports"] = {}
    for name, symbols in info["windows_usb"]["system_exports"].items():
        report["checking_system_library"] = name
        api = ctypes.WinDLL(name, winmode=0x800, use_last_error=True)  # LOAD_LIBRARY_SEARCH_SYSTEM32
        for symbol in symbols:
            report["checking_system_symbol"] = symbol
            getattr(api, symbol)
        report["system_exports"][name] = len(symbols)
        report.pop("checking_system_symbol", None)
    report.pop("checking_system_library", None)
    report["stage"] = "import_windows_modules"
    report["windows_usb_modules"] = []
    for module in info["windows_usb"]["modules"]:
        importlib.import_module(module)
        report["windows_usb_modules"].append(module)

    report["stage"] = "load_bind_init"
    with LibUSB(str(library)) as usb:
        if not usb.context.value:
            raise RuntimeError("libusb did not return an initialized context")
        report["context_initialized"] = True
        for symbol in expected["required_exports"]:
            getattr(usb.lib, symbol)
        report["resolved_exports"] = len(expected["required_exports"])
        get_version = usb.lib.libusb_get_version
        get_version.argtypes = []
        get_version.restype = ctypes.POINTER(LibraryVersion)
        native = get_version()
        if not native:
            raise RuntimeError("libusb_get_version returned no version")
        version = native.contents
        report["library_version"] = f"{version.major}.{version.minor}.{version.micro}"
        if report["library_version"] != expected["version"]:
            raise RuntimeError("Loaded libusb version differs from the packaged version")
        report["stage"] = "enumerate"
        # This is the GUI's cached descriptor scan. It never calls open_exact.
        report["device_count"] = len(usb.discover())
    if usb.context:
        raise RuntimeError("libusb context did not close")
    report.update(status="PASS", stage="complete", context_closed=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="also write the JSON report to this path")
    args = parser.parse_args(argv)
    report = {"status": "FAIL", "stage": "startup", "platform": sys.platform,
              "architecture": platform.machine(), "library": "libusb-1.0.dll",
              "device_open_attempted": False, "interface_claim_attempted": False,
              "aoa_request_attempted": False}
    try:
        check(Path(__file__).resolve().parent, report)
    except Exception as error:
        # Avoid logging installation paths, USB device IDs or serial numbers.
        report["error_type"] = type(error).__name__
        if isinstance(error, RuntimeError) and not isinstance(error, OSError):
            report["error"] = str(error)
        if hasattr(error, "operation") and hasattr(error, "code"):
            report["libusb_error"] = {"operation": error.operation, "code": error.code,
                                      "name": getattr(error, "name", None)}
        if getattr(error, "winerror", None) is not None:
            report["winerror"] = error.winerror
    payload = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    print(payload, end="")
    if args.output:
        args.output.write_text(payload, encoding="utf-8")
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
