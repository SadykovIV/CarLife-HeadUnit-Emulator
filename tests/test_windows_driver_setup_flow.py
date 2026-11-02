"""Unit-test the boundary between the USB probe, user confirmation and elevation."""
from contextlib import nullcontext
from dataclasses import asdict, replace
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from hu_lab import windows_driver_setup as setup
from hu_lab.usb import USBIdentity
from hu_lab.windows_usb_interfaces import WindowsInterfaceError


class DriverSetupFlowTests(unittest.TestCase):
    def setUp(self):
        self.ident = USBIdentity(3, (6,), 0x18d1, 0x2d01, 2)
        self.row = dict(identity=asdict(self.ident), windows_devinst=20)
        self.target = Mock(status='installable')
        self.plan = setup.SetupPlan('install', self.ident, 20, self.target)
        folder = self.enterContext(TemporaryDirectory())
        self.enterContext(patch.object(setup, 'usb_lock_directory', return_value=Path(folder)))
        self.usb = Mock()
        self.factory = Mock(side_effect=lambda: nullcontext(self.usb))
        self.usb.discover.return_value = [self.row]
        self.find = self.enterContext(patch.object(setup, 'find_driver_setup_target', return_value=self.target))
        self.launch = Mock(return_value='installed')

    def handle(self):
        handle = SimpleNamespace(descriptor=self.row, verified_interface={'number': 0})
        return nullcontext(handle)

    def inspect(self):
        return setup.inspect_setup(self.ident, usb_factory=self.factory)

    def apply(self):
        return setup.apply_setup(self.plan, usb_factory=self.factory, launcher=self.launch)

    def test_already_working_winusb_has_no_install_target(self):
        self.usb.open_exact.return_value = self.handle()
        plan = self.inspect()
        self.assertEqual(plan.action, 'ready')
        self.assertIsNone(plan.target)
        self.find.assert_not_called()

    def test_exact_selected_target_only_after_missing_path(self):
        self.usb.open_exact.side_effect = WindowsInterfaceError('adb_only')
        self.assertEqual(self.inspect().action, 'install')
        self.find.assert_called_with(self.ident, self.row)
        self.target.status = 'already_winusb'
        self.assertEqual(self.inspect().action, 'register')
        self.find.side_effect = WindowsInterfaceError('adb_not_separate')
        with self.assertRaises(setup.SetupUnavailable):
            self.inspect()

    def test_non_aoa_stale_and_busy_ports_are_refused_before_open(self):
        for pid in (0x6860, 0x2d00):
            with self.assertRaises(setup.SetupUnavailable):
                setup.inspect_setup(replace(self.ident, pid=pid), usb_factory=self.factory)
        self.factory.assert_not_called()
        self.row['identity']['address'] += 1
        with self.assertRaises(setup.SetupUnavailable):
            self.inspect()
        self.row['identity']['address'] -= 1
        with patch.object(setup, 'lock_owner', side_effect=BlockingIOError):
            with self.assertRaises(setup.SetupUnavailable):
                self.apply()
        self.usb.open_exact.assert_not_called()
        self.launch.assert_not_called()

    def test_cancel_does_not_probe_again_or_retry(self):
        self.usb.open_exact.side_effect = WindowsInterfaceError('adb_only')
        self.launch.return_value = 'cancelled'
        self.assertEqual(self.apply(), 'cancelled')
        self.assertEqual(self.usb.open_exact.call_count, 1)
        self.launch.assert_called_once_with(self.target)

    def test_working_since_confirmation_or_changed_target_never_elevate(self):
        self.usb.open_exact.return_value = self.handle()
        self.assertEqual(self.apply(), 'ready')
        self.target.verify_present.side_effect = WindowsInterfaceError('device_changed')
        with self.assertRaises(WindowsInterfaceError):
            self.apply()
        self.launch.assert_not_called()

    def test_success_requires_fresh_verified_handle_to_original_device(self):
        self.usb.open_exact.side_effect = [WindowsInterfaceError('no_winusb'), self.handle()]
        self.assertEqual(self.apply(), 'ready')
        self.assertEqual(self.factory.call_count, 2)
        self.usb.open_exact.side_effect = WindowsInterfaceError('adb_only')
        self.assertEqual(self.apply(), 'reconnect')
        self.usb.discover.side_effect = [[self.row], [dict(self.row, windows_devinst=21)]]
        self.assertEqual(self.apply(), 'reconnect')
        self.assertEqual(self.usb.open_exact.call_count, 5)
