"""Cross-platform USB ownership: Windows dispatch and portable DLL location."""
import errno
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from hu_lab import platform_support as support


class PlatformSupportTests(unittest.TestCase):
    def test_windows_lock_uses_one_byte_and_reports_contention(self):
        backend = types.SimpleNamespace(LK_NBLCK=2, LK_UNLCK=0, locking=Mock())
        with tempfile.TemporaryFile(mode="w+") as owner:
            with patch.object(sys, "platform", "win32"), patch.dict(sys.modules, msvcrt=backend):
                owner.write("old owner")
                support.lock_owner(owner)
                self.assertEqual(owner.tell(), 0)
                backend.locking.assert_called_with(owner.fileno(), backend.LK_NBLCK, 1)
                backend.locking.side_effect = OSError(errno.EACCES, "locked")
                with self.assertRaises(BlockingIOError):
                    support.lock_owner(owner)
                backend.locking.side_effect = None
                owner.seek(4)
                support.unlock_owner(owner)
                self.assertEqual(owner.tell(), 0)
                backend.locking.assert_called_with(owner.fileno(), backend.LK_UNLCK, 1)

    def test_windows_lock_preserves_non_contention_errors(self):
        backend = types.SimpleNamespace(LK_NBLCK=2, locking=Mock(side_effect=OSError(errno.EBADF, "closed")))
        with tempfile.TemporaryFile(mode="w+") as owner:
            with patch.object(sys, "platform", "win32"), patch.dict(sys.modules, msvcrt=backend):
                with self.assertRaises(OSError) as caught:
                    support.lock_owner(owner)
                self.assertNotIsInstance(caught.exception, BlockingIOError)

    def test_windows_bundle_resolves_dll_without_searching_cwd(self):
        with tempfile.TemporaryDirectory() as root:
            dll = Path(root) / "libusb-1.0.dll"
            dll.write_bytes(b"fixture")
            with patch.object(sys, "platform", "win32"), patch.object(sys, "_MEIPASS", root, create=True):
                self.assertEqual(support.bundled_libusb(), str(dll))


if __name__ == "__main__":
    unittest.main()
