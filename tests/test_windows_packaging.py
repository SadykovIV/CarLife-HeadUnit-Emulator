"""Cross-platform regressions for the portable Windows package's dependencies."""
import importlib.util
import ctypes
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch


SPEC = importlib.util.spec_from_file_location(
    "windows_builder", Path(__file__).resolve().parents[1] / "windows" / "build.py")
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)
PROBE_SPEC = importlib.util.spec_from_file_location(
    "windows_libusb_probe", Path(__file__).resolve().parents[1] / "windows" / "verify_usb_runtime.py")
probe = importlib.util.module_from_spec(PROBE_SPEC)
PROBE_SPEC.loader.exec_module(probe)


class WindowsPackagingTests(unittest.TestCase):
    def test_library_probe_accepts_closed_null_context_on_empty_bus(self):
        from hu_lab import usb

        lib = Mock()
        def init(context):
            ctypes.cast(context, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.c_void_p(1234)
            return 0
        lib.libusb_init.side_effect = init
        lib.libusb_get_version.return_value = ctypes.pointer(probe.LibraryVersion(1, 0, 30, 0))
        lib.libusb_get_device_list.return_value = 0
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dll = root / "libusb-1.0.dll"
            dll.write_bytes(b"test binary")
            info = {"version": builder.VERSION, "usb_library": {"version": "1.0.30",
                    "sha256": hashlib.sha256(dll.read_bytes()).hexdigest(),
                    "required_exports": list(builder.REQUIRED_USB_EXPORTS)},
                    "windows_usb": {"modules": [], "system_exports": {"winusb.dll": ["WinUsb_QueryPipe"]}}}
            (root / "build-info.json").write_text(json.dumps(info), encoding="utf-8")
            report = {}
            with patch.object(probe.sys, "platform", "win32"), \
                    patch("hu_lab.platform_support.bundled_libusb", return_value=str(dll)), \
                    patch.object(probe.ctypes, "WinDLL", create=True) as win_dll, \
                    patch.object(usb.C, "CDLL", return_value=lib):
                probe.check(root, report)
        # Production LibUSB.close leaves a null ctypes.c_void_p(), not None.
        self.assertTrue(report["context_closed"])
        self.assertEqual((report["status"], report["device_count"]), ("PASS", 0))
        win_dll.assert_called_once_with("winusb.dll", winmode=0x800, use_last_error=True)
        win_dll.return_value.WinUsb_QueryPipe.assert_not_called()
        lib.libusb_free_device_list.assert_called_once()
        lib.libusb_exit.assert_called_once()
        lib.libusb_open.assert_not_called()
        lib.libusb_claim_interface.assert_not_called()
        lib.libusb_control_transfer.assert_not_called()

    def make_package(self, stage):
        qt = stage / "runtime/Lib/site-packages/PySide6"
        files = [f"{name}.pyd" for name in
                 ("QtCore", "QtGui", "QtWidgets", "QtNetwork", "QtMultimedia", "QtWebEngineCore")]
        files += ["Qt6Multimedia.dll", "Qt6Network.dll", "opengl32sw.dll"]
        files += ["plugins/" + name for name in (
            "platforms/qwindows.dll", "platforms/qoffscreen.dll", "platforms/qminimal.dll",
            "styles/qmodernwindowsstyle.dll", "multimedia/windowsmediaplugin.dll",
            "multimedia/ffmpegmediaplugin.dll", "tls/qschannelbackend.dll",
            "tls/qcertonlybackend.dll", "networkinformation/qnetworklistmanager.dll",
            "iconengines/qsvgicon.dll")]
        for name in files:
            target = qt / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"fixture")
        return qt

    def imports(self, path):
        # Native PE imports retain Qt6Network.dll, but never QtNetwork.pyd.
        return {
            "QtMultimedia.pyd": ["Qt6Multimedia.dll"],
            "Qt6Multimedia.dll": ["Qt6Network.dll"],
            "QtNetwork.pyd": ["Qt6Network.dll"],
        }.get(path.name, [])

    def test_multimedia_keeps_network_binding_beyond_pe_import_closure(self):
        with tempfile.TemporaryDirectory() as directory:
            stage = Path(directory)
            qt = self.make_package(stage)
            with patch.object(builder, "inspect_pe", side_effect=self.imports):
                native, _ = builder.trim_qt_and_verify(stage)
            self.assertTrue((qt / "QtNetwork.pyd").is_file())
            self.assertTrue((qt / "Qt6Network.dll").is_file())
            self.assertIn(str((qt / "QtNetwork.pyd").relative_to(stage)), native)
            self.assertFalse((qt / "QtWebEngineCore.pyd").exists())

    def test_missing_required_network_binding_fails_packaging(self):
        with tempfile.TemporaryDirectory() as directory:
            stage = Path(directory)
            qt = self.make_package(stage)
            (qt / "QtNetwork.pyd").unlink()
            with patch.object(builder, "inspect_pe", side_effect=self.imports):
                with self.assertRaisesRegex(RuntimeError, r"Required runtime binary missing: .*QtNetwork\.pyd"):
                    builder.trim_qt_and_verify(stage)
