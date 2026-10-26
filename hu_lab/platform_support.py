"""Small host differences shared by the desktop and addressed USB transport."""
import errno
import os
from pathlib import Path
import sys
import tempfile


def bundled_libusb():
    """Resolve only a library distributed with this app, when one is present."""
    roots = [Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))]
    roots.append(Path(sys.executable).resolve().parent)
    name = "libusb-1.0.dll" if sys.platform == "win32" else "libusb-1.0.dylib"
    return next((str(root / name) for root in roots if (root / name).is_file()), None)


def usb_lock_directory():
    # Preserve the existing shared location for command-line HU processes.
    return Path(tempfile.gettempdir() if sys.platform == "win32" else "/tmp")


def open_owner_lock(path):
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    return os.fdopen(os.open(path, flags, 0o600), "r+", encoding="utf-8")


def lock_owner(owner):
    """Acquire immediately; closing the file also releases the OS lock."""
    if sys.platform == "win32":
        import msvcrt
        owner.seek(0)
        try:
            msvcrt.locking(owner.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as error:
            if error.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise BlockingIOError(error.errno, "USB port is already locked") from error
            raise
    else:
        import fcntl
        fcntl.flock(owner.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def unlock_owner(owner):
    if sys.platform == "win32":
        import msvcrt
        owner.seek(0)
        msvcrt.locking(owner.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(owner.fileno(), fcntl.LOCK_UN)
