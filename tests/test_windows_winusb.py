"""WinUSB handle validation and outstanding I/O lifetime, without hardware."""
import copy
import ctypes as C
from dataclasses import asdict, replace
import hashlib
import threading
import unittest
from unittest.mock import patch

from hu_lab import windows_winusb as win
from hu_lab.usb import RecoveryBlocked, USBIdentity


SERIAL = 'A\u00e9\U0001f600z'
ASCII_DIGEST = hashlib.sha256(b'A???z').hexdigest()  # non-ASCII per UTF-16 code unit
IDENTITY = USBIdentity(1, (1, 7), 0x18d1, 0x2d01, 2, ASCII_DIGEST)
PIPES = [dict(type=2, address=0x81, max_packet=512), dict(type=2, address=1, max_packet=512)]
LIVE = dict(number=0, alternate=0, usb_class=255, subclass=255, protocol=0, pipes=PIPES)


def descriptor(identity=IDENTITY):
    return dict(identity=asdict(identity), configuration=1, interfaces=[dict(
        number=0, alternate=0, **{'class': 255}, subclass=255, protocol=0,
        endpoints=[dict(attributes=p['type'], address=p['address'], max_packet=p['max_packet']) for p in PIPES])])


class Candidate:
    path = 'private-device-path-DO-NOT-LOG'
    role = 'mi_00'

    def __init__(self):
        self.checks = 0
        self.stale_on = None

    def verify_present(self):
        self.checks += 1
        return self.checks != self.stale_on


class FakeAPI:
    def __init__(self):
        self.calls = []
        self.live = copy.deepcopy(LIVE)
        self.vid = IDENTITY.vid
        self.serial = SERIAL
        self.transfer_impl = None

    def open_file(self, path):
        self.calls.append('open')
        return 101

    def initialize(self, file):
        self.calls.append('initialize')
        return 202

    def interface_info(self, interface):
        self.calls.append('query')
        return self.live

    def descriptor(self, interface, kind, index, language, capacity):
        self.calls.append(('descriptor', kind, index, language))
        if kind == 1:
            raw = bytearray(18)
            raw[:2] = bytes((18, 1))
            raw[8:10] = self.vid.to_bytes(2, 'little')
            raw[10:12] = IDENTITY.pid.to_bytes(2, 'little')
            raw[16] = 3
            return bytes(raw)
        if index == 0:
            return bytes((6, 3, 9, 4))  # real bLength may exceed requested 4
        serial = self.serial.encode('utf-16le')
        return bytes((len(serial) + 2, 3)) + serial

    def free(self, interface):
        self.calls.append(('free', interface))

    def close_file(self, file):
        self.calls.append(('close', file))

    def transfer(self, file, interface, op, pipe, buffer, length, deadline, cancel, setup=None):
        self.calls.append(('transfer', file, interface, op, pipe, bytes(buffer[:length]),
                           None if setup is None else (setup.type, setup.request, setup.value, setup.index, setup.length)))
        if self.transfer_impl:
            return self.transfer_impl(op, buffer, length, cancel)
        return length


class Gate:
    def require(self, identity):
        assert identity == IDENTITY


class VerifiedHandleTests(unittest.TestCase):
    def setUp(self):
        self.api, self.candidate = FakeAPI(), Candidate()

    def open(self):
        return win.open_verified_winusb(self.candidate, IDENTITY, descriptor(), api=self.api)

    def test_adb_and_other_interface_roles_never_opened(self):
        for role in ('adb', 'mi_01', 'other'):
            self.candidate.role = role
            with self.subTest(role=role), self.assertRaises(win.WinUSBCandidateMismatch):
                self.open()
        self.assertEqual(self.api.calls, [])

    def test_unlabelled_child_with_actual_adb_or_wrong_pipes_is_closed_without_traffic(self):
        for override in (dict(usb_class=255, subclass=66, protocol=1), dict(number=1), dict(alternate=1),
                         dict(pipes=[dict(PIPES[0], max_packet=64), PIPES[1]]),
                         dict(pipes=[dict(PIPES[0], address=0x82), PIPES[1]]),
                         dict(pipes=[dict(PIPES[0], type=3), PIPES[1]])):
            with self.subTest(override=override):
                self.setUp()
                self.candidate.role = 'unlabelled'
                self.api.live.update(override)
                with self.assertRaises(win.WinUSBCandidateMismatch) as caught:
                    self.open()
                self.assertEqual(caught.exception.actual_interface, self.api.live)
                self.assertEqual(self.api.calls, ['open', 'initialize', 'query', ('free', 202), ('close', 101)])
                self.assertNotIn(self.candidate.path, str(caught.exception))

    def test_serial_utf16_matches_libusb_and_is_not_exposed(self):
        with self.open() as handle:
            self.assertEqual(handle.serial_sha256(), ASCII_DIGEST)
            self.assertIn(('descriptor', 3, 3, 0x409), self.api.calls)
            self.assertNotIn(self.api.serial, str(handle.verified_interface))
        self.api.serial = 'different-private-serial'
        with self.assertRaises(win.WinUSBIdentityMismatch) as caught:
            self.open()
        self.assertNotIn(self.api.serial, str(caught.exception))
        self.assertNotIn(self.candidate.path, str(caught.exception))
        self.assertEqual(self.api.calls[-2:], [('free', 202), ('close', 101)])

    def test_vid_and_parent_disappearance_fail_closed(self):
        self.api.vid = 0x1234
        with self.assertRaises(win.WinUSBIdentityMismatch):
            self.open()
        self.api.vid = IDENTITY.vid
        self.candidate.stale_on = self.candidate.checks + 2
        with self.assertRaises(win.WinUSBIdentityMismatch):
            self.open()
        self.assertEqual(self.api.calls[-2:], [('free', 202), ('close', 101)])
        self.assertFalse(any(isinstance(c, tuple) and c[0] == 'transfer' for c in self.api.calls))

    def test_only_claimed_same_handle_can_read_write_and_register_hid(self):
        handle = self.open()
        with self.assertRaises(RecoveryBlocked):
            handle.hid_control(54, 1, index=63)
        with self.assertRaises(RecoveryBlocked):
            handle.read(65536)
        handle.claim_accessory(Gate())
        def short_transfer(op, buffer, size, cancel):
            if op == 'read':
                buffer[:3] = b'abc'
                return 3
            return min(size, 2)
        self.api.transfer_impl = short_transfer
        self.assertEqual(handle.read(65536), b'abc')
        self.assertEqual(handle.write(b'abcde'), 5)
        handle.hid_control(54, 1, index=63)
        with self.assertRaises(ValueError):
            handle.hid_control(53, 1)
        emitted = [c for c in self.api.calls if isinstance(c, tuple) and c[0] == 'transfer']
        self.assertTrue(all(c[1:3] == (101, 202) for c in emitted))
        self.assertEqual([(c[4], c[5]) for c in emitted if c[3] == 'write'],
                         [(1, b'abcde'), (1, b'cde'), (1, b'e')])
        self.assertEqual(emitted[0][4], 0x81)
        self.assertEqual(emitted[-1][-1], (0x40, 54, 1, 63, 0))
        handle.close()
        handle.close()
        self.assertEqual(self.api.calls.count(('free', 202)), 1)

    def test_write_failure_with_unknown_prefix_is_never_resubmitted(self):
        with self.open() as handle:
            handle.claim_accessory(Gate())
            def fail(op, buffer, length, cancel):
                raise win.WinUSBError('WinUSB write completion', 87, issued=True)
            self.api.transfer_impl = fail
            with self.assertRaises(win.WinUSBError) as caught:
                handle.write(b'CarLife')
            self.assertEqual(caught.exception.winerror, 87)
            self.assertTrue(caught.exception.request_may_have_run)
            with self.assertRaisesRegex(OSError, 'replay'):
                handle.write(b'CarLife')
            self.assertEqual(sum(isinstance(c, tuple) and c[0] == 'transfer' for c in self.api.calls), 1)

    def test_close_does_not_free_handle_before_inflight_operation_drains(self):
        handle = self.open()
        handle.claim_accessory(Gate())
        submitted, drained = threading.Event(), threading.Event()
        def transfer(op, buffer, length, cancel):
            submitted.set()
            if not cancel.wait(2):
                raise AssertionError('expected cancellation')
            self.api.calls.append('drained')
            drained.set()
            raise InterruptedError('cancelled')
        self.api.transfer_impl = transfer
        errors = []
        def read():
            try:
                handle.read(65536)
            except InterruptedError as error:
                errors.append(error)
        worker = threading.Thread(target=read)
        worker.start()
        self.assertTrue(submitted.wait(1))
        handle.close()
        worker.join(1)
        self.assertTrue(drained.is_set())
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertLess(self.api.calls.index('drained'), self.api.calls.index(('free', 202)))


class OverlappedTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.error = 997
        self.now = 0
        self.ready = False
        self.complete_code = 0
        self.count = 4
        self.immediate = False
        tests = self
        class DLL:
            def CreateEventW(self, security, manual, initial, name):
                tests.assertEqual((manual, initial), (1, 0))
                return 303
            def WinUsb_ReadPipe(self, handle, pipe, buffer, size, count, target):
                tests.events.append('submit')
                C.cast(count, C.POINTER(win.U32))[0] = 9  # not the OVERLAPPED result
                if tests.immediate:
                    return 1
                tests.error = 997
                return 0
            def WaitForSingleObject(self, event, timeout):
                tests.events.append('wait')
                tests.now += .05
                return 0 if tests.ready else 258
            def CancelIoEx(self, file, target):
                tests.events.append('cancel')
                return 1
            def WinUsb_GetOverlappedResult(self, interface, target, count, wait):
                tests.assertEqual(wait, 1)
                tests.events.append('drain')
                tests.error = tests.complete_code
                C.cast(count, C.POINTER(win.U32))[0] = tests.count
                return tests.complete_code == 0
            def CloseHandle(self, file):
                tests.events.append('close_event')
                return 1
        self.api = win.WinUSBAPI.__new__(win.WinUSBAPI)
        self.api.kernel = self.api.usb = DLL()

    def read(self):
        with patch.object(C, 'get_last_error', create=True, side_effect=lambda: self.error), patch(
                'hu_lab.windows_winusb.time.monotonic', side_effect=lambda: self.now):
            return self.api.transfer(101, 202, 'read', 0x81, (win.U8 * 10)(), 10, .04, threading.Event())

    def test_timeout_drains_cancelled_io_before_event_or_buffer_release(self):
        self.complete_code = 995
        with self.assertRaises(TimeoutError) as caught:
            self.read()
        self.assertEqual(self.events, ['submit', 'wait', 'cancel', 'drain', 'close_event'])
        self.assertTrue(caught.exception.request_may_have_run)

    def test_completion_winning_cancel_race_preserves_confirmed_progress(self):
        self.assertEqual(self.read(), 4)
        self.assertEqual(self.events, ['submit', 'wait', 'cancel', 'drain', 'close_event'])

    def test_async_error_87_preserved_without_resubmission(self):
        self.ready = True
        self.complete_code = 87
        with self.assertRaises(win.WinUSBError) as caught:
            self.read()
        self.assertEqual(self.events, ['submit', 'wait', 'drain', 'close_event'])
        self.assertEqual(caught.exception.winerror, 87)
        self.assertTrue(caught.exception.request_may_have_run)

    def test_immediate_true_still_uses_overlapped_result_count(self):
        self.immediate = True
        self.assertEqual(self.read(), 4)
        self.assertEqual(self.events, ['submit', 'drain', 'close_event'])

    def test_ffi_layout_matches_windows_x64_structures(self):
        self.assertEqual(C.sizeof(win._Setup), 8)
        self.assertEqual(C.sizeof(win._Interface), 9)
        self.assertEqual((C.sizeof(win._Pipe), win._Pipe.packet.offset), (12, 6))
        if C.sizeof(C.c_void_p) == 8:
            self.assertEqual((C.sizeof(win._Overlapped), win._Overlapped.event.offset), (32, 24))
