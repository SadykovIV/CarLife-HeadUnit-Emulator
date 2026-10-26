"""Finder/PyInstaller entry point; no workspace or Homebrew runtime dependency."""
from __future__ import annotations

import ctypes
import json
import platform
from pathlib import Path
import sys


def bundle_check() -> int:
    """Validate packaged imports and native libraries without USB or a window."""
    import av
    import PySide6
    from PySide6 import QtCore, QtGui, QtMultimedia, QtWidgets
    from hu_lab import audio, client, desktop, desktop_session, h264, protocol

    root = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    from hu_lab.platform_support import bundled_libusb
    library = bundled_libusb()
    if not library:
        raise RuntimeError("Packaged libusb library is missing")
    ctypes.CDLL(str(library))
    av.CodecContext.create("h264", "r")
    av.CodecContext.create("libx264", "w")

    def bundle_path(path):
        # Shareable verification reports must not expose the installation path.
        try:
            return Path(path).relative_to(root).as_posix()
        except ValueError:
            return Path(path).name

    result = {
        "status": "PASS",
        "architecture": platform.machine(),
        "frozen": bool(getattr(sys, "frozen", False)),
        "python": platform.python_version(),
        "pyside6": PySide6.__version__,
        "pyav": av.__version__,
        "libusb": bundle_path(library),
        "platform": sys.platform,
        "portable": bool(getattr(sys, "_carlife_portable", False)),
        "source": bundle_path(desktop.__file__),
        "h264_decoder": "available",
        "demo_encoder": "libx264",
    }
    if "--check-output" in sys.argv:
        target = Path(sys.argv[sys.argv.index("--check-output") + 1])
        target.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    elif sys.stdout is not None:
        print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    if "--bundle-check" in sys.argv:
        raise SystemExit(bundle_check())
    from hu_lab.desktop import main
    raise SystemExit(main())
