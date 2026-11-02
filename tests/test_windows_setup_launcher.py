"""Check the elevation boundary with fake targets; no Windows process or UAC is launched."""
import ctypes
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from hu_lab import windows_setup_launcher as setup


class Target:
    status = "installable"

    def __init__(self):
        self.proof = {"parent_devinst": 123, "child_devinst": 456, "adb_devinst": 457,
                      "ports": (1, 7), "hardware_id": r"USB\VID_18D1&PID_2D01&MI_00"}
        self.handoff = Mock(side_effect=lambda: self.proof.copy())


class SetupLauncherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.helper = self.root / setup.HELPER_NAME
        self.helper.write_bytes(b"fixture PE; never executed")
        self.meta = {"driver_setup_helper": {"name": setup.HELPER_NAME,
                     "sha256": hashlib.sha256(self.helper.read_bytes()).hexdigest()}}
        self.save_meta()
        self.target, self.launch = Target(), Mock(return_value=0)

    def save_meta(self):
        (self.root / "build-info.json").write_text(json.dumps(self.meta), encoding="utf-8")

    def run_fake(self):
        return setup._launch(self.target, self.root, lambda path: path.open("rb"), self.launch)

    def test_fixed_native_executable_and_numeric_only_arguments(self):
        self.assertEqual(self.run_fake(), "installed")
        self.launch.assert_called_once_with(self.helper, "--install-mi00 123 456 1.7")
        self.target.handoff.assert_called_once()
        self.target.status = "already_winusb"
        self.assertEqual(self.run_fake(), "registered")
        self.launch.assert_called_with(self.helper, "--register-mi00 123 456 1.7")

    def test_missing_modified_or_redirected_helper_cannot_request_elevation(self):
        self.helper.unlink()
        with self.assertRaises(RuntimeError):
            self.run_fake()
        self.helper.write_bytes(b"modified")
        with self.assertRaisesRegex(RuntimeError, "SHA256"):
            self.run_fake()
        self.meta["driver_setup_helper"]["name"] = "../other.exe"
        self.save_meta()
        with self.assertRaises(RuntimeError):
            self.run_fake()
        self.launch.assert_not_called()
        self.target.handoff.assert_not_called()

    def test_link_to_another_executable_is_not_the_packaged_helper(self):
        alternate = self.root / "arbitrary.exe"
        self.helper.rename(alternate)
        try:
            self.helper.symlink_to(alternate)
        except OSError:
            self.skipTest("Host does not allow creating a test symlink")
        with self.assertRaisesRegex(RuntimeError, "Путь"):
            self.run_fake()
        self.launch.assert_not_called()

    def test_stale_parent_adb_and_noninteger_input_are_rejected_before_uac(self):
        original = self.target.proof.copy()
        for change in ({"hardware_id": r"USB\VID_18D1&PID_2D01"},
                       {"hardware_id": r"USB\VID_18D1&PID_2D01&MI_01"},
                       {"hardware_id": r"USB\VID_18D1&PID_2D01&ADB"},
                       {"child_devinst": 123}, {"child_devinst": 457},
                       {"parent_devinst": True}, {"ports": ("7 --other",)}, {"ports": ()}):
            with self.subTest(change=change):
                self.target.proof = {**original, **change}
                with self.assertRaises(RuntimeError):
                    self.run_fake()
        self.target.handoff.side_effect = RuntimeError("changed")
        with self.assertRaisesRegex(RuntimeError, "changed"):
            self.run_fake()
        self.launch.assert_not_called()

    def test_result_classification_never_conflates_failure_cancel_or_reboot_with_install(self):
        for code, label in ((10, "already_ready"), (11, "reconnect"), (12, "reboot"), (13, "cancelled")):
            self.launch.return_value = code
            self.assertEqual(self.run_fake(), label)
        for code in (20, 21, 22, 23, 24, 99):
            self.launch.return_value = code
            with self.assertRaises(RuntimeError):
                self.run_fake()

    def test_shell_execute_uses_runas_on_fixed_exe_and_propagates_uac_cancellation(self):
        shell, kernel = Mock(), Mock()
        def rejected(raw):
            info = ctypes.cast(raw, ctypes.POINTER(setup._ExecuteInfo)).contents
            self.assertEqual(info.lpVerb, "runas")
            self.assertEqual(info.lpFile, str(self.helper))
            self.assertEqual(info.lpParameters, "--register-mi00 123 456 1.7")
            self.assertEqual(info.fMask, 0x140)
            return 0
        shell.ShellExecuteExW.side_effect = rejected
        with patch.object(setup.C, "WinDLL", create=True, side_effect=(shell, kernel)), \
             patch.object(setup.C, "get_last_error", create=True, return_value=1223):
            self.assertEqual(setup._elevate(self.helper, "--register-mi00 123 456 1.7"), 13)
        kernel.WaitForSingleObject.assert_not_called()
