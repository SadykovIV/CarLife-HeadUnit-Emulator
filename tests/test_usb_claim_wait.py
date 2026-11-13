"""Darwin exclusive ownership and reconnect using the selected accessory handle."""
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from hu_lab.desktop_session import ExplicitAOATransportFactory
from hu_lab.native_usb import USBInterfaceOwned
from hu_lab.platform_support import lock_owner, open_owner_lock, unlock_owner
from hu_lab.public import prepare_usb
from hu_lab.usb import USBError
from tests.test_usb_acquisition import ACCESSORY, NORMAL, MemoryBus


class DesktopPreflightLockTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch('hu_lab.native_usb.usb_lock_directory', return_value=self.root))
        path = self.root / ('carlife-usb-' + ACCESSORY.physical_key.replace(':', '-') + '.lock')
        self.other = self.enterContext(open_owner_lock(path))
        self.cancel = threading.Event()

    def desktop(self, make_usb, on_wait=None):
        return ExplicitAOATransportFactory(ACCESSORY, self.root/'attempt', usb_factory=make_usb,
                                           on_claim_wait=on_wait, cancel=self.cancel)

    def test_port_stays_owned_from_first_preflight_open_through_stream_close(self):
        lock_owner(self.other)
        old_released = []
        bus = MemoryBus(ACCESSORY)
        def released(name):
            self.assertFalse(bus.opens)
            unlock_owner(self.other)
            old_released.append(name)

        def make_usb():
            # WinUSB initialization can already fail at the preflight open.
            self.assertEqual(old_released, ['CarLife HeadUnit Emulator'])
            with self.assertRaises(BlockingIOError):
                lock_owner(self.other)
            context = bus.factory()
            context.lib.libusb_release_interface = lambda *_: 0
            return context

        with patch('hu_lab.native_usb.open_owner_lock', wraps=open_owner_lock) as opened:
            stream = self.desktop(make_usb, released)()
            try:
                opened.assert_called_once()
                self.assertEqual((len(bus.opens), bus.claims, bus.controls), (2, [0], []))
                with self.assertRaises(BlockingIOError):
                    lock_owner(self.other)
            finally:
                stream.close()
        lock_owner(self.other)

    def test_cancel_while_old_window_owns_port_skips_preflight(self):
        lock_owner(self.other)
        make_usb = Mock(side_effect=AssertionError('opened USB before cancel'))
        with self.assertRaises(InterruptedError) as failure:
            self.desktop(make_usb, lambda _: self.cancel.set())()
        self.assertEqual(failure.exception.connection_failure['stage'], 'usb_port_lock')
        make_usb.assert_not_called()
        self.assertFalse((self.root/'attempt/usb-selection.json').exists())

    def test_preflight_open_failure_releases_port_and_keeps_its_usb_error(self):
        bus = MemoryBus(ACCESSORY, fail_open=1)
        with self.assertRaises(USBError) as failure:
            self.desktop(bus.factory)()
        self.assertEqual(failure.exception.connection_failure['stage'], 'preflight_accessory_open')
        self.assertEqual((len(bus.opens), bus.claims), (1, []))
        lock_owner(self.other)


class ClaimWaitTests(unittest.TestCase):
    def factory(self, initial, owners, *, failures=(), cancel_wait=False, on_wait=None):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.lock_path = root / ('carlife-usb-' + initial.physical_key.replace(':', '-') + '.lock')
        self.bus = MemoryBus(initial)
        self.releases = []
        self.claim_results = iter(failures)
        def claim(handle, interface):
            self.bus.claims.append(interface)
            return next(self.claim_results, 0)

        def context():
            usb = self.bus.factory()
            usb._darwin_claim_wait = True
            usb.lib.libusb_claim_interface = claim
            usb.lib.libusb_release_interface = lambda handle, index: self.releases.append(index) or 0
            return usb

        self.enterContext(patch('hu_lab.native_usb.usb_lock_directory', return_value=root))
        self.now = 100.
        self.waits = []
        self.enterContext(patch('hu_lab.native_usb.time.monotonic', side_effect=lambda: self.now))
        self.lookup = self.enterContext(patch('hu_lab.macos_usb_owner.accessory_interface_owner',
                                              side_effect=owners))
        stop = threading.Event()
        def wait(delay):
            self.waits.append(delay)
            self.now += delay
            if cancel_wait:
                stop.set()
            return stop.is_set()
        self.cancel = Mock(is_set=stop.is_set, set=stop.set, wait=wait)
        return prepare_usb(initial.physical_key, root/'attempt', aoa_start=True, usb_factory=context,
                           expected_identity=initial, on_claim_wait=on_wait, cancel=self.cancel)

    def test_wait_for_owner_release_then_connect_on_existing_or_new_aoa(self):
        for initial in (ACCESSORY, NORMAL):
            with self.subTest(initial=initial):
                factory = self.factory(initial, ['pid 123, media host', None])
                stream = factory()
                try:
                    self.assertEqual(stream.evidence['entry'], 'ALREADY_ACCESSORY' if initial == ACCESSORY
                                     else 'HOST_INITIATED_51_52_53')
                    self.assertEqual(self.bus.claims, [0])
                    self.assertEqual(self.bus.controls, [] if initial == ACCESSORY else [51] + [52]*6 + [53])
                    lookup_handles = [c.args[0] for c in self.lookup.call_args_list]
                    self.assertEqual(lookup_handles, [stream.handle, stream.handle])
                finally:
                    stream.close()
                self.assertEqual(self.releases, [0])

    def test_persistent_owner_expires_without_unsuccessful_native_claims(self):
        owner = 'pid 123, media host'
        factory = self.factory(ACCESSORY, lambda _: owner)
        with self.assertRaises(USBInterfaceOwned) as caught:
            factory()
        self.assertEqual(caught.exception.claim_owner, owner)
        self.assertEqual(caught.exception.claim_attempts, 0)
        self.assertEqual((self.bus.controls, self.bus.claims, self.releases), ([], [], []))
        self.assertIsNone(caught.exception.connection_failure['code'])
        self.assertAlmostEqual(sum(self.waits), 3)

    def test_unknown_owner_transient_access_or_busy_then_success(self):
        factory = self.factory(ACCESSORY, lambda _: None, failures=[-3, -6])
        stream = factory()
        try:
            self.assertEqual((self.bus.claims, self.bus.controls), ([0, 0, 0], []))
            self.assertAlmostEqual(sum(self.waits), 3)
        finally:
            stream.close()

    def test_real_claim_failure_preserves_native_error_and_last_observed_owner(self):
        def owner(_):
            return 'pid 123, media host' if self.bus.claims else None
        factory = self.factory(ACCESSORY, owner, failures=[-3])
        with self.assertRaises(USBError) as caught:
            factory()
        self.assertEqual(caught.exception.code, -3)
        self.assertEqual(caught.exception.claim_owner, 'pid 123, media host')
        self.assertEqual(caught.exception.claim_attempts, 1)
        self.assertEqual((self.bus.claims, self.bus.controls, self.releases), ([0], [], []))

    def test_cancel_while_waiting_for_owner(self):
        factory = self.factory(ACCESSORY, lambda _: 'pid 123, media host', cancel_wait=True)
        with self.assertRaises(InterruptedError):
            factory()
        self.assertEqual((self.bus.claims, self.bus.controls), ([], []))
        self.assertEqual(len(self.waits), 1)

    def test_report_owner_changes_and_claim_after_user_releases_interface(self):
        notified = []
        factory = self.factory(ACCESSORY, ['first'] * 8 + ['second'] * 8 + [None],
                               on_wait=notified.append)
        stream = factory()
        try:
            self.assertEqual(notified, ['first', 'second'])
            self.assertEqual(sum(self.waits), 8)
            self.assertEqual((self.bus.claims, self.bus.controls), ([0], []))
        finally:
            stream.close()

    def test_visible_wait_for_persistent_owner_is_bounded(self):
        notified = []
        factory = self.factory(ACCESSORY, lambda _: 'media host', on_wait=notified.append)
        with self.assertRaises(USBInterfaceOwned) as caught:
            factory()
        self.assertEqual(sum(self.waits), 60)
        self.assertEqual(notified, ['media host'])
        self.assertEqual(caught.exception.connection_failure['reason'], 'interface_owned')
        self.assertEqual((self.bus.controls, self.bus.claims), ([], []))

    def test_claims_selected_accessory_after_another_emulator_unlocks_the_port(self):
        notified = []
        def release(name):
            notified.append(name)
            unlock_owner(old)
        factory = self.factory(ACCESSORY, lambda _: None, on_wait=release)
        with open_owner_lock(self.lock_path) as old:
            lock_owner(old)
            stream = factory()
            try:
                self.assertEqual(notified, ['CarLife HeadUnit Emulator'])
                self.assertEqual(self.bus.claims, [0])
                self.assertEqual(self.bus.controls, [])
                with self.assertRaises(BlockingIOError):
                    lock_owner(old)
            finally:
                stream.close()
            lock_owner(old)

    def test_expired_and_cancelled_lock_wait_leave_the_original_port_lock_held(self):
        for cancel in (True, False):
            with self.subTest(cancel=cancel):
                notified = []
                factory = self.factory(ACCESSORY, lambda _: None, cancel_wait=cancel, on_wait=notified.append)
                with open_owner_lock(self.lock_path) as old:
                    lock_owner(old)
                    with self.assertRaises(InterruptedError if cancel else USBInterfaceOwned) as caught:
                        factory()
                    self.assertEqual(caught.exception.connection_failure['stage'], 'usb_port_lock')
                    if not cancel:
                        self.assertEqual(caught.exception.connection_failure['reason'], 'interface_owned')
                        self.assertAlmostEqual(sum(self.waits), 60)
                    self.assertEqual(notified, ['CarLife HeadUnit Emulator'])
                    self.assertEqual((self.bus.controls, self.bus.claims), ([], []))
                    with open_owner_lock(self.lock_path) as probe, self.assertRaises(BlockingIOError):
                        lock_owner(probe)

    def test_cancellation_before_start_or_during_aoa_identification_never_sends_start(self):
        for cancel_after in (None, 51, 52):
            with self.subTest(cancel_after=cancel_after):
                factory = self.factory(NORMAL, lambda _: None)
                event = factory.event
                def cancel_on_request(stage, **details):
                    event(stage, **details)
                    if details.get('request') == cancel_after:
                        self.cancel.set()
                if cancel_after is None:
                    self.cancel.set()
                else:
                    factory.event = cancel_on_request
                with self.assertRaises(InterruptedError):
                    factory()
                self.assertNotIn(53, self.bus.controls)
                self.assertEqual(self.bus.claims, [])

    def test_disconnected_device_or_non_contention_error_is_not_retried(self):
        for code in (-4, -9):
            with self.subTest(code=code):
                factory = self.factory(ACCESSORY, lambda _: None, failures=[code])
                with self.assertRaises(USBError) as caught:
                    factory()
                self.assertEqual(caught.exception.code, code)
                self.assertEqual((self.bus.claims, self.bus.controls, self.waits), ([0], [], []))
