"""Keyboard UI with a real loopback CarLife peer and fake USB HID endpoint.

No USB/device access, native activity assertions, or host clipboard reads: the
clipboard entry point is replaced with a spy for explicit-paste tests.
"""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tests import test_desktop_ui as ui_helpers
from hu_lab.keyboard_input import KeyboardInputError, text_reports
from hu_lab.protocol import HARD_KEY, fields

try:
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QAction, QInputMethodEvent, QKeySequence
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from hu_lab import desktop
except ImportError:
    desktop = None


class FakeKeyboard:
    def __init__(self, client, status='ready'):
        self.client = client
        self.status = status
        self.texts = []
        self.keys = []
        self.pending = 0
        self.cancels = 0
        self.start_thread = self.close_thread = None
        self.closed_before_client_stop = None
        self.fail_submit = False

    def start(self):
        self.start_thread = threading.get_ident()
        self.start_state = self.client.session.state
        if self.status == 'idle': self.status = 'ready'
        return self.snapshot()

    def snapshot(self):
        return {'status': self.status, 'available': self.status == 'ready',
                'error': 'optional HID fixture failed' if self.status == 'failed' else None,
                'reason': None, 'sent_keys': 0, 'pending_keys': self.pending,
                'worker_alive': False, 'registered': self.status == 'ready'}

    def submit_text(self, text, layout='us'):
        if self.fail_submit:
            raise KeyboardInputError('fixture keyboard is unavailable')
        reports = text_reports(text, layout)
        self.texts.append((text, layout))
        self.pending += len(reports)

    def submit_key(self, usage, modifiers=0):
        if self.fail_submit:
            raise KeyboardInputError('fixture keyboard is unavailable')
        self.keys.append((usage, modifiers))
        self.pending += 1

    def cancel_pending(self):
        self.cancels += 1
        self.pending = 0

    def close(self):
        self.close_thread = threading.get_ident()
        self.closed_before_client_stop = not self.client.stopped
        self.cancel_pending()
        self.status = 'closed'
        return self.snapshot()


@unittest.skipIf(desktop is None, 'Desktop extras required')
class DesktopKeyboardWindowTests(unittest.TestCase):
    # Reuse only fixture helpers. Do not inherit/re-run another TestCase's tests.
    wait_for = ui_helpers.DesktopWindowTests.wait_for
    start_demo = ui_helpers.DesktopWindowTests.start_demo

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.created = []
        self.initial_status = 'ready'
        def factory(client):
            keyboard = FakeKeyboard(client, self.initial_status)
            self.created.append(keyboard)
            return keyboard
        self.factory_patch = patch.object(desktop.HIDKeyboard, 'for_client', side_effect=factory)
        self.factory = self.factory_patch.start()
        self.addCleanup(self.factory_patch.stop)
        ui_helpers.DesktopWindowTests.setUp(self)

    def tearDown(self):
        ui_helpers.DesktopWindowTests.tearDown(self)

    def tab(self, title):
        for n in range(self.window.tabs.count()):
            if self.window.tabs.tabText(n) == title:
                self.window.tabs.setCurrentIndex(n)
                self.app.processEvents()
                return
        self.fail('Missing tab: ' + title)

    def focus_video(self):
        self.window.video.setFocus(Qt.OtherFocusReason)
        self.app.processEvents()

    def test_clipboard_read_requires_explicit_video_paste_or_button(self):
        contents = 'Private clipboard fixture'
        read = Mock(return_value=contents)
        with patch.object(desktop.QApplication, 'clipboard', return_value=SimpleNamespace(text=read)) as clipboard:
            self.start_demo()
            keyboard = self.created[-1]
            self.focus_video()
            QTest.keyClick(self.window.video, Qt.Key_A)
            self.assertEqual(keyboard.texts, [('a', 'us')])
            clipboard.assert_not_called()
            QTest.keyClick(self.window.video, Qt.Key_V, Qt.ControlModifier)
            self.assertEqual(read.call_count, 1)
            self.assertEqual(keyboard.texts[-1], (contents, 'us'))
            self.tab('Ввод')
            self.window.update_keyboard_status()
            QTest.mouseClick(self.window.paste_button, Qt.LeftButton)
            self.assertEqual(read.call_count, 2)
            self.assertEqual(keyboard.texts[-1], (contents, 'us'))
            self.assertNotIn(contents, self.window.log.toPlainText())
            self.assertNotIn(contents, self.window.report().__repr__())

    def test_unavailable_paused_and_disconnected_paste_never_read_clipboard(self):
        self.initial_status = 'failed'
        self.start_demo()
        keyboard = self.created[-1]
        with patch.object(desktop.QApplication, 'clipboard') as clipboard:
            self.focus_video()
            QTest.keyClick(self.window.video, Qt.Key_V, Qt.ControlModifier)
            self.window.paste_clipboard()
            self.assertEqual(keyboard.texts, [])
            clipboard.assert_not_called()
            keyboard.status = 'ready'
            self.window.toggle_pause()
            self.window.paste_clipboard()
            clipboard.assert_not_called()
            self.window.disconnect_session()
            self.wait_for(lambda: self.window.client is None and not self.window.busy)
            self.window.paste_clipboard()
            clipboard.assert_not_called()

    def test_local_editor_and_settings_keys_do_not_type_remote(self):
        self.start_demo()
        keyboard = self.created[-1]
        self.tab('Ввод')
        self.window.text_input.setFocus()
        QTest.keyClicks(self.window.text_input, 'Local editor only')
        QTest.keyClick(self.window.text_input, Qt.Key_Left)
        self.assertEqual(keyboard.texts, [])
        self.assertEqual(keyboard.keys, [])
        self.assertIn('Local editor only', self.window.text_input.toPlainText())
        self.tab('Видео')
        self.window.width.setFocus()
        QTest.keyClick(self.window.width, Qt.Key_Up)
        self.assertEqual(keyboard.texts, [])
        self.assertEqual(keyboard.keys, [])

    def test_ime_commit_routes_selected_layout_but_preedit_is_not_sent(self):
        self.start_demo()
        keyboard = self.created[-1]
        self.window.keyboard_layout.setCurrentIndex(self.window.keyboard_layout.findData('ru'))
        self.focus_video()
        self.assertTrue(self.window.video.inputMethodQuery(Qt.ImEnabled))
        self.assertEqual(self.window.video.inputMethodQuery(Qt.ImSurroundingText), '')
        QApplication.sendEvent(self.window.video, QInputMethodEvent('Пр', []))
        self.assertEqual(keyboard.texts, [])
        event = QInputMethodEvent('', [])
        event.setCommitString('Привет')
        QApplication.sendEvent(self.window.video, event)
        self.assertEqual(keyboard.texts, [('Привет', 'ru')])
        self.assertNotIn('Привет', self.window.log.toPlainText())
        # Replacement requires remote editor context which HID cannot read;
        # it must not silently append the replacement text instead.
        replacement = QInputMethodEvent('', [])
        replacement.setCommitString('другой', -3, 3)
        QApplication.sendEvent(self.window.video, replacement)
        self.assertEqual(keyboard.texts, [('Привет', 'ru')])
        self.assertTrue(self.window.error_label.isVisible())
        self.assertNotIn('другой', self.window.log.toPlainText())

    def test_pending_typing_cancelled_by_focus_tab_pause_and_disconnect(self):
        client, _ = self.start_demo()
        keyboard = self.created[-1]
        self.assertNotEqual(keyboard.start_thread, threading.get_ident())
        self.tab('Ввод')
        self.focus_video()
        self.window.send_text('focus')
        self.assertGreater(keyboard.pending, 0)
        self.window.text_input.setFocus()
        self.app.processEvents()
        self.assertEqual(keyboard.pending, 0)
        self.focus_video()
        self.window.send_text('tab')
        self.tab('Диагностика')
        self.assertEqual(keyboard.pending, 0)
        self.tab('Экран')
        self.window.send_text('pause')
        self.window.toggle_pause()
        self.assertEqual(keyboard.pending, 0)
        self.assertFalse(self.window.keyboard_ready())
        self.window.toggle_pause()
        self.wait_for(lambda: client.session.state == 'STREAMING')
        self.window.send_text('disconnect')
        self.window.disconnect_session()
        self.wait_for(lambda: self.window.client is None and not self.window.busy)
        self.assertEqual(keyboard.pending, 0)
        self.assertTrue(keyboard.closed_before_client_stop)
        self.assertNotEqual(keyboard.close_thread, threading.get_ident())
        self.assertEqual(self.window.last_report['keyboard']['status'], 'closed')

    def test_usb_keyboard_starts_after_streaming_without_blocking_video(self):
        self.initial_status = 'idle'
        create_client = desktop.create_client
        class USBDesktopClient:
            # The GUI sees a USB connection. The real loopback client's decoder
            # retains TCP framing, so this tests lifecycle without fake USB IO.
            backend = 'DIRECT_USB_AOA'
            def __init__(self, client):
                self.client = client
            def __getattr__(self, name):
                return getattr(self.client, name)
        def create(*args, **kwargs):
            return USBDesktopClient(create_client(*args, **kwargs))
        with patch.object(desktop, 'create_client', side_effect=create):
            client, peer = self.start_demo()
        keyboard = self.created[-1]
        self.wait_for(lambda: keyboard.start_thread is not None)
        self.assertEqual(keyboard.start_state, 'STREAMING')
        self.assertNotEqual(keyboard.start_thread, threading.get_ident())
        self.assertFalse(self.window.busy)
        self.assertTrue(self.window.keyboard_start_requested)
        before = client.decoded_frames
        self.wait_for(lambda: client.decoded_frames >= before + 2)
        self.assertIsNone(client.error)
        self.assertIsNone(peer.error)

    def test_optional_hid_failure_keeps_video_and_navigation_working(self):
        self.initial_status = 'failed'
        client, peer = self.start_demo()
        self.assertIn('fixture failed', self.window.keyboard_status.text())
        self.focus_video()
        QTest.keyClick(self.window.video, Qt.Key_Left)
        self.wait_for(lambda: any(message == HARD_KEY and fields(payload).get(1) == 0x15
                                 for message, payload in peer.commands))
        before = client.decoded_frames
        self.wait_for(lambda: client.decoded_frames >= before + 2)
        self.assertIsNone(client.error)
        self.assertIsNone(peer.error)
        self.assertEqual(client.session.state, 'STREAMING')
        keyboard = self.created[-1]
        keyboard.status = 'ready'
        keyboard.fail_submit = True
        self.window.send_text('never logged')
        self.assertNotIn('never logged', self.window.log.toPlainText())
        before = client.decoded_frames
        self.wait_for(lambda: client.decoded_frames >= before + 2)
        self.assertIsNone(client.error)

    def test_tab_key_is_sent_to_android_while_video_has_focus(self):
        self.start_demo()
        keyboard = self.created[-1]
        self.focus_video()
        QTest.keyClick(self.window.video, Qt.Key_Tab)
        self.assertEqual(keyboard.keys, [(0x2b, 0)])
        QTest.keyClick(self.window.video, Qt.Key_Backtab, Qt.ShiftModifier)
        self.assertEqual(keyboard.keys, [(0x2b, 0), (0x2b, 2)])

    def test_application_shortcuts_do_not_steal_video_keyboard_input(self):
        self.start_demo()
        keyboard = self.created[-1]
        observers = {}
        for action in self.window.findChildren(QAction):
            if action.text() in ('Отключить', 'Сохранить снимок', 'Завершить'):
                # Observe activation without opening a file dialog or closing
                # the session under test. The QAction shortcut stays intact.
                action.triggered.disconnect()
                observers[action.text()] = Mock()
                action.triggered.connect(observers[action.text()])
        self.assertEqual(set(observers), {'Отключить', 'Сохранить снимок', 'Завершить'})
        self.focus_video()
        QTest.keyClick(self.window.video, Qt.Key_D, Qt.ControlModifier)
        QTest.keyClick(self.window.video, Qt.Key_S, Qt.ControlModifier)
        self.assertEqual({name: observer.call_count for name, observer in observers.items()},
                         {'Отключить': 0, 'Сохранить снимок': 0, 'Завершить': 0})
        self.assertEqual(keyboard.keys, [(0x07, 1), (0x16, 1)])
        quit_sequence = QKeySequence(QKeySequence.Quit)
        # Some platform plugins (including offscreen) provide no standard
        # Quit binding. An empty sequence sends no event and cannot activate
        # QAction. Verify the exception when the platform defines a binding.
        expected_quit = int(not quit_sequence.isEmpty())
        if expected_quit:
            QTest.keySequence(self.window.video, quit_sequence)
        self.assertEqual(observers['Завершить'].call_count, expected_quit)
        self.assertEqual(keyboard.keys, [(0x07, 1), (0x16, 1)])
        # The interception belongs only to the focused projected screen.
        self.tab('Видео')
        self.window.width.setFocus()
        QTest.keyClick(self.window.width, Qt.Key_D, Qt.ControlModifier)
        QTest.keyClick(self.window.width, Qt.Key_S, Qt.ControlModifier)
        self.assertEqual({name: observer.call_count for name, observer in observers.items()},
                         {'Отключить': 1, 'Сохранить снимок': 1, 'Завершить': expected_quit})
        self.assertEqual(keyboard.keys, [(0x07, 1), (0x16, 1)])


if __name__ == '__main__':
    unittest.main()
