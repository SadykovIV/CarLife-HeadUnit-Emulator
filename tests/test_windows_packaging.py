"""Cross-platform regressions for the portable Windows package's dependencies."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "windows_builder", Path(__file__).resolve().parents[1] / "windows" / "build.py")
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


class WindowsPackagingTests(unittest.TestCase):
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
