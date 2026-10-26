"""Offline AOA HID control, layout and worker tests; no USB/device access."""
import ctypes
import json
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from hu_lab.keyboard_input import (HIDKeyboard, KeyboardInputError,
    KEYBOARD_REPORT_DESCRIPTOR, RELEASE_REPORT, MAX_TEXT_LENGTH, CTRL, SHIFT,
    key_report, text_reports, qt_keyboard_command)
from hu_lab.usb import USBError, USBHandle, USBIdentity


def wait_for(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError('Keyboard worker did not reach expected state')
        time.sleep(.002)


class FakeHandle:
    def __init__(self, protocol=2, maximum=64):
        self.protocol = protocol
        self.descriptor = {'endpoint0_max_packet': maximum}
        self.calls = []
        self.hook = None

    def get_protocol(self):
        self.calls.append((51, 0, 0, b''))
        return self.protocol

    def hid_control(self, request, hid_id, *, index=0, data=b''):
        self.calls.append((request, hid_id, index, data))
        if self.hook:
            self.hook(request, data)
        return data

    def presses(self):
        return [data for request, _, _, data in self.calls if request == 57 and any(data)]


class LayoutTests(unittest.TestCase):
    def test_ascii_golden_hid_usages_and_crlf(self):
        result = text_reports('aA!0[]\\\t\r\n', 'us')
        self.assertEqual([(r[0], r[2]) for r in result],
                         [(0, 4), (2, 4), (2, 0x1e), (0, 0x27), (0, 0x2f),
                          (0, 0x30), (0, 0x31), (0, 0x2b), (0, 0x28)])
        self.assertTrue(all(len(r) == 8 and r[1] == 0 and r[3:] == bytes(5) for r in result))
        self.assertEqual(len(text_reports(''.join(chr(i) for i in range(32, 127)))), 95)

    def test_russian_ytsuken_and_punctuation_use_actual_physical_keys(self):
        result = text_reports('ФыЁж№.,/', 'ru')
        self.assertEqual([(r[0], r[2]) for r in result],
                         [(2, 4), (0, 0x16), (2, 0x35), (0, 0x33),
                          (2, 0x20), (0, 0x38), (2, 0x38), (2, 0x31)])
        alphabet = 'абвгдеёжзийклмнопрстуфхцчшщъыьэюя'
        self.assertEqual(len(text_reports(alphabet + alphabet.upper(), 'ru')), 66)

    def test_mixed_layout_unicode_and_oversize_are_rejected_without_substitution(self):
        for value, layout in [('abcЯ', 'us'), ('абвA', 'ru'), ('secret🙂', 'us'), ('x\x00', 'us')]:
            with self.subTest(layout=layout), self.assertRaises(KeyboardInputError) as caught:
                text_reports(value, layout)
            self.assertNotIn(value, str(caught.exception))
            self.assertNotIn('U+', str(caught.exception))
        with self.assertRaisesRegex(KeyboardInputError, '4096'):
            text_reports('a' * (MAX_TEXT_LENGTH + 1))
        with self.assertRaises(KeyboardInputError):
            text_reports('a', 'auto')
        self.assertEqual(text_reports(''), ())

    def test_invalid_usage_and_modifiers_cannot_be_encoded(self):
        for usage, modifier in [(3, 0), (102, 0), (4, 256), (True, 0), (4, -1)]:
            with self.assertRaises(KeyboardInputError):
                key_report(usage, modifier)


class KeyboardWorkerTests(unittest.TestCase):
    def keyboard(self, handle=None):
        handle = handle or FakeHandle()
        keyboard = HIDKeyboard(handle, key_interval=0)
        self.addCleanup(keyboard.close)
        return keyboard, handle

    def test_register_descriptor_chunks_release_and_unregister(self):
        keyboard, handle = self.keyboard(FakeHandle(maximum=8))
        self.assertTrue(keyboard.start()['available'])
        self.assertEqual(handle.calls[0], (51, 0, 0, b''))
        self.assertEqual(handle.calls[1], (54, 0x434c, len(KEYBOARD_REPORT_DESCRIPTOR), b''))
        chunks = [call for call in handle.calls if call[0] == 56]
        self.assertEqual([c[2] for c in chunks], list(range(0, len(KEYBOARD_REPORT_DESCRIPTOR), 8)))
        self.assertEqual(b''.join(c[3] for c in chunks), KEYBOARD_REPORT_DESCRIPTOR)
        self.assertTrue(all(len(c[3]) <= 8 for c in chunks))
        self.assertEqual(handle.calls[-1], (57, 0x434c, 0, RELEASE_REPORT))
        keyboard.close()
        self.assertEqual(handle.calls[-1], (55, 0x434c, 0, b''))
        count = len(handle.calls)
        keyboard.close()
        self.assertEqual(len(handle.calls), count)
        self.assertFalse(keyboard.snapshot()['worker_alive'])
        self.assertFalse(keyboard.snapshot()['registered'])

    def test_aoa1_and_tcp_are_explicitly_unavailable_without_register(self):
        keyboard, handle = self.keyboard(FakeHandle(protocol=1))
        self.assertEqual(keyboard.start()['status'], 'unavailable')
        self.assertEqual([call[0] for call in handle.calls], [51])
        with self.assertRaises(KeyboardInputError):
            keyboard.submit_text('no')
        client = SimpleNamespace(backend='TCP_STREAM', sock=object())
        tcp = HIDKeyboard.for_client(client)
        self.assertEqual(tcp.start()['status'], 'unavailable')
        self.assertIn('TCP', tcp.snapshot()['reason'])
        tcp.close()

    def test_native_factory_reuses_owned_handle_without_opening_usb(self):
        handle = FakeHandle()
        client = SimpleNamespace(backend='DIRECT_USB_AOA', sock=SimpleNamespace(handle=handle))
        keyboard = HIDKeyboard.for_client(client)
        self.addCleanup(keyboard.close)
        self.assertIs(keyboard.handle, handle)
        self.assertEqual(handle.calls, [])

    def test_failed_descriptor_unregisters_and_does_not_close_shared_handle(self):
        keyboard, handle = self.keyboard()
        def hook(request, _data):
            if request == 56:
                raise USBError('descriptor rejected', -9)
        handle.hook = hook
        result = keyboard.start()
        self.assertEqual(result['status'], 'failed')
        self.assertFalse(result['registered'])
        self.assertEqual(handle.calls[-1][0], 55)
        self.assertFalse(result['worker_alive'])

    def test_worker_launch_failure_unregisters_and_remains_safe_to_close(self):
        keyboard, handle = self.keyboard()
        with patch('hu_lab.keyboard_input.threading.Thread.start', side_effect=RuntimeError('cannot start new thread')):
            state = keyboard.start()
        self.assertEqual(state['status'], 'failed')
        self.assertIsNone(keyboard.worker)
        self.assertFalse(state['registered'])
        self.assertEqual(handle.calls[-1][0], 55)
        count = len(handle.calls)
        self.assertEqual(keyboard.close()['status'], 'failed')
        self.assertEqual(len(handle.calls), count)

    def test_android_registration_wait_retries_only_empty_release(self):
        keyboard, handle = self.keyboard()
        pending = [2]
        def hook(request, data):
            if request == 57 and pending[0]:
                self.assertEqual(data, RELEASE_REPORT)
                pending[0] -= 1
                raise USBError('HID not initialized yet', -9)
        handle.hook = hook
        self.assertTrue(keyboard.start()['available'])
        self.assertEqual(sum(c[0] == 54 for c in handle.calls), 1)
        self.assertEqual(sum(c[0] == 57 for c in handle.calls), 3)

    def test_async_key_and_text_are_atomic_press_release_without_plaintext_diagnostics(self):
        keyboard, handle = self.keyboard()
        keyboard.start()
        keyboard.submit_text('Secret')
        keyboard.submit_key(0x50, CTRL | SHIFT)
        wait_for(lambda: keyboard.snapshot()['pending_keys'] == 0)
        self.assertEqual(len(handle.presses()), 7)
        events = [data for req, _, _, data in handle.calls if req == 57][1:]
        self.assertEqual(len(events), 14)
        self.assertTrue(all(events[n] == RELEASE_REPORT for n in range(1, 14, 2)))
        self.assertEqual(events[-2], key_report(0x50, CTRL | SHIFT))
        self.assertNotIn('Secret', json.dumps(keyboard.snapshot()))
        self.assertFalse(keyboard.snapshot()['device_text_delivery_verified'])

    def test_invalid_whole_paste_has_no_prefix_on_wire(self):
        keyboard, handle = self.keyboard()
        keyboard.start()
        count = len(handle.calls)
        with self.assertRaises(KeyboardInputError):
            keyboard.submit_text('valid prefix🙂')
        self.assertEqual(len(handle.calls), count)
        self.assertEqual(keyboard.snapshot()['pending_keys'], 0)

    def test_cancel_drops_queued_text_and_releases_inflight_key_without_blocking_ui(self):
        keyboard, handle = self.keyboard()
        keyboard.start()
        entered, release = threading.Event(), threading.Event()
        def hook(request, data):
            if request == 57 and any(data) and not entered.is_set():
                entered.set()
                if not release.wait(2):
                    raise RuntimeError('test release timeout')
        handle.hook = hook
        keyboard.submit_text('abcdef')
        self.assertTrue(entered.wait(1))
        keyboard.submit_text('ZZ')
        started = time.monotonic()
        keyboard.cancel_pending()
        self.assertLess(time.monotonic() - started, .05)
        release.set()
        wait_for(lambda: keyboard.snapshot()['pending_keys'] == 0)
        self.assertEqual(handle.presses(), [key_report(4)])
        self.assertEqual(keyboard.snapshot()['cancelled_keys'], 7)
        keyboard.submit_text('b')
        wait_for(lambda: keyboard.snapshot()['sent_keys'] == 2)
        self.assertEqual(handle.presses()[-1], key_report(5))

    def test_queue_bound_rejects_entire_next_batch(self):
        keyboard, handle = self.keyboard()
        keyboard.start()
        entered, release = threading.Event(), threading.Event()
        def hook(request, data):
            if request == 57 and any(data):
                entered.set()
                if not release.wait(2):
                    raise RuntimeError('test release timeout')
        handle.hook = hook
        keyboard.submit_text('a' * 4096)
        self.assertTrue(entered.wait(1))
        try:
            with self.assertRaisesRegex(KeyboardInputError, 'queue is full'):
                keyboard.submit_text('b')
            self.assertEqual(keyboard.snapshot()['pending_keys'], 4096)
        finally:
            keyboard.cancel_pending()
            release.set()
        wait_for(lambda: keyboard.snapshot()['pending_keys'] == 0)

    def test_press_timeout_is_not_replayed_and_keyboard_failure_is_local(self):
        keyboard, handle = self.keyboard()
        keyboard.start()
        def hook(request, data):
            if request == 57 and any(data):
                raise USBError('input timeout', -7)
        handle.hook = hook
        keyboard.submit_text('abc')
        wait_for(lambda: not keyboard.snapshot()['worker_alive'])
        self.assertEqual(keyboard.snapshot()['status'], 'failed')
        self.assertFalse(keyboard.snapshot()['registered'])
        self.assertEqual(handle.presses(), [key_report(4)])
        self.assertEqual(handle.calls[-1][0], 55)
        with self.assertRaises(KeyboardInputError):
            keyboard.submit_text('d')


class USBControlTests(unittest.TestCase):
    def handle(self, transfer, close=lambda _handle: None):
        lib = SimpleNamespace(libusb_control_transfer=transfer,
            libusb_close=close, libusb_release_interface=lambda *args: 0)
        return USBHandle(SimpleNamespace(lib=lib), 123,
                         USBIdentity(1, (2,), 0x18d1, 0x2d00, 3), {})

    def test_hid_uses_value_and_offset_without_changing_bulk_state(self):
        calls = []
        def transfer(handle, rt, request, value, index, data, length, timeout):
            calls.append((rt, request, value, index, bytes(data[:length]), timeout))
            return length
        handle = self.handle(transfer)
        with self.assertRaisesRegex(RuntimeError, 'not claimed'):
            handle.hid_control(54, 4)
        handle.pair = SimpleNamespace(interface=0)
        handle.hid_control(56, 0x434c, index=8, data=b'fixture')
        self.assertEqual(calls, [(0x40, 56, 0x434c, 8, b'fixture', 200)])
        with self.assertRaises(ValueError):
            handle.hid_control(53, 0)
        handle.close()
        with self.assertRaises(RuntimeError):
            handle._control(0x40, 57, data=bytes(8), value=1)

    def test_close_waits_for_inflight_ep0_without_serializing_bulk_locks(self):
        entered, release, closed = threading.Event(), threading.Event(), threading.Event()
        def transfer(*args):
            entered.set()
            if not release.wait(2):
                raise RuntimeError('test release timeout')
            return args[-2]
        handle = self.handle(transfer, lambda _: closed.set())
        errors = []
        def control():
            try:
                handle._control(0x40, 57, data=bytes(8), value=1)
            except Exception as error:
                errors.append(error)
        worker = threading.Thread(target=control)
        worker.start()
        self.assertTrue(entered.wait(1))
        self.assertTrue(handle.read_lock.acquire(blocking=False))
        handle.read_lock.release()
        self.assertTrue(handle.write_lock.acquire(blocking=False))
        handle.write_lock.release()
        closer = threading.Thread(target=handle.close)
        closer.start()
        self.assertFalse(closed.wait(.03))
        release.set()
        worker.join(1); closer.join(1)
        self.assertFalse(worker.is_alive())
        self.assertFalse(closer.is_alive())
        self.assertTrue(closed.is_set())
        self.assertEqual(errors, [])


class QtKeyboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def event(self, key, modifiers=None, text=''):
        from PySide6.QtCore import QEvent, Qt
        from PySide6.QtGui import QKeyEvent
        return QKeyEvent(QEvent.KeyPress, key, Qt.NoModifier if modifiers is None else modifiers, text)

    def test_navigation_fallback_never_turns_backspace_into_android_back(self):
        from PySide6.QtCore import Qt
        self.assertEqual(qt_keyboard_command(self.event(Qt.Key_Left)).hard_key, 'left')
        self.assertEqual(qt_keyboard_command(self.event(Qt.Key_Return)).hard_key, 'ok')
        self.assertIsNone(qt_keyboard_command(self.event(Qt.Key_Backspace)).hard_key)
        selected = qt_keyboard_command(self.event(Qt.Key_Left, Qt.ShiftModifier))
        self.assertEqual((selected.usage, selected.modifiers, selected.hard_key), (0x50, SHIFT, None))

    def test_paste_is_explicit_request_without_clipboard_read_and_shortcuts_are_hid(self):
        from PySide6.QtCore import Qt
        paste = qt_keyboard_command(self.event(Qt.Key_V, Qt.ControlModifier))
        self.assertEqual(paste.kind, 'paste')
        shortcut = qt_keyboard_command(self.event(Qt.Key_A, Qt.ControlModifier))
        self.assertEqual((shortcut.kind, shortcut.usage, shortcut.modifiers), ('key', 4, CTRL))
        self.assertEqual(qt_keyboard_command(self.event(Qt.Key_Shift, Qt.ShiftModifier)).kind, 'ignore')

    def test_printable_text_uses_selected_layout_and_media_uses_carlife(self):
        from PySide6.QtCore import Qt
        command = qt_keyboard_command(self.event(Qt.Key_A, text='a'))
        self.assertEqual((command.kind, command.text), ('text', 'a'))
        with self.assertRaises(KeyboardInputError):
            qt_keyboard_command(self.event(Qt.Key_unknown, text='я'), 'us')
        media = qt_keyboard_command(self.event(Qt.Key_MediaNext))
        self.assertEqual((media.kind, media.hard_key), ('unsupported', 'next'))


if __name__ == '__main__':
    unittest.main()
