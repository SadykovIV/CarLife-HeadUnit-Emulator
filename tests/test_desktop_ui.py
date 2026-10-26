"""Offscreen desktop integration with a loopback MD; no USB acquisition.

Uses the real wire parser, H.264 encoder/decoder, Qt events, and async session
ownership. Audio is muted and the peer emits silence. Requires desktop extras
and permission to bind a loopback socket in sandboxed execution environments.
"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path
import json
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from tests import dispose_qt_object

try:
    import av
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QCheckBox
    from hu_lab import desktop
except ImportError:
    desktop = None

from hu_lab.protocol import VIDEO_RESET, VIDEO_PAUSE, VIDEO_START, HARD_KEY, fields
from hu_lab.touch import ACTION3
from hu_lab.macos_activity import UserInitiatedActivity


class ActivityBackend:
    """Exercise session ownership without making process assertions on the host."""
    def __init__(self):
        self.begun = []
        self.ended = []
        self.released = []

    def begin(self, reason, options):
        token = object()
        self.begun.append(token)
        return token

    def end(self, token):
        self.ended.append(token)

    def release(self, token):
        self.released.append(token)


@unittest.skipIf(desktop is None, "PySide6 and PyAV desktop extras are required")
class DesktopWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # The window schedules USB discovery on startup. Keep the entire test
        # independent of whether the host happens to have a phone attached.
        self.discovery = patch.object(desktop, "list_usb_devices", return_value=[])
        self.discover = self.discovery.start()
        self.addCleanup(self.discovery.stop)
        self.activity_backend = ActivityBackend()
        self.activity_patch = patch.object(desktop, "UserInitiatedActivity", side_effect=lambda *args, **kwargs:
            UserInitiatedActivity(*args, **kwargs, platform="darwin", backend_factory=lambda: self.activity_backend))
        self.activity_patch.start()
        self.addCleanup(self.activity_patch.stop)
        self.window = desktop.HeadUnitWindow(Path(self.tmp.name))
        self.window.muted.setChecked(True)
        self.window.transport.setCurrentIndex(self.window.transport.findData("demo"))
        self.window.profile.setCurrentIndex(self.window.profile.findData("honda-v2"))
        self.window.touch_profile.setCurrentIndex(self.window.touch_profile.findData("action3-multi"))
        self.window.width.setValue(320)
        self.window.height.setValue(240)
        self.window.fps.setValue(15)
        self.window.show()
        self.app.processEvents()
        self.wait_for(lambda: not self.window.busy)

    def tearDown(self):
        try:
            self.window.close()
            self.wait_for(lambda: not self.window.isVisible() and not self.window.busy, 12)
            dispose_qt_object(self.window)
        finally:
            self.tmp.cleanup()

    def wait_for(self, condition, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if condition():
                return
            QTest.qWait(10)
        client = self.window.client
        self.fail(f"GUI condition timed out: busy={self.window.busy}; "
                  f"state={client.session.state if client else None}; "
                  f"client_error={client.error if client else None}; "
                  f"ui_error={self.window.error_label.text()}")

    def start_demo(self):
        QTest.mouseClick(self.window.connect_button, Qt.LeftButton)
        self.wait_for(lambda: self.window.client is not None and
                      self.window.client.decoded_frames >= 4 and
                      self.window.video.transform is not None)
        self.assertIsNone(self.window.client.error)
        self.assertIsNone(self.window.demo.error)
        self.assertTrue(self.window.audio.muted)
        return self.window.client, self.window.demo

    def select_tab(self, title):
        index = next(n for n in range(self.window.tabs.count())
                     if self.window.tabs.tabText(n) == title)
        QTest.mouseClick(self.window.tabs, Qt.LeftButton,
                         pos=self.window.tabs.tabRect(index).center())
        self.app.processEvents()

    def test_header_selection_survives_discovery_and_usb_connect_enables_aoa(self):
        window = self.window
        normal = dict(bus=1, ports=[2, 3], vid=0x1234, pid=0x5678, address=4)
        accessory = dict(bus=1, ports=[2, 4], vid=0x18d1, pid=0x2d00, address=5)
        rows = [dict(identity=normal, label="Selected phone", desktop_supported=False),
                dict(identity=accessory, label="Other accessory", desktop_supported=True)]
        self.discover.return_value = rows
        window.transport.setCurrentIndex(window.transport.findData("usb"))
        window.refresh_usb()
        self.wait_for(lambda: not window.busy)
        window.usb_devices.setCurrentIndex(0)
        self.discover.return_value = list(reversed(rows))
        QTest.mouseClick(window.scan_button, Qt.LeftButton)
        self.wait_for(lambda: not window.busy)
        self.assertEqual(window.usb_devices.currentData(), normal)
        self.assertTrue(window.header.isAncestorOf(window.usb_devices))
        self.assertTrue(window.usb_devices.isVisible())
        self.assertFalse(window.tcp_group.isVisible())
        # Accessory mode is unconditional; the separate optional AOA 2.0 HID
        # keyboard checkbox must not reintroduce a switch for USB transport.
        self.assertFalse(any("AOA" in box.text() for box in window.findChildren(QCheckBox)))
        self.assertFalse(hasattr(window, "aoa_start"))
        # Capture the connection boundary, never acquire a physical USB device.
        with patch.object(desktop, "create_client", side_effect=RuntimeError("Synthetic USB stop")) as create:
            for identity in (normal, accessory):
                window.usb_devices.setCurrentIndex(window.usb_devices.findData(identity))
                QTest.mouseClick(window.connect_button, Qt.LeftButton)
                self.wait_for(lambda: not window.busy)
                config = create.call_args.args[0]
                self.assertEqual(config.usb_identity.ports, tuple(identity["ports"]))
                self.assertEqual(config.usb_identity.address, identity["address"])
                self.assertTrue(config.aoa_start)
                self.assertEqual(config.transport, "usb")
                self.assertIsNone(window.client)
                self.assertFalse(window.activity.snapshot()["active"])
        self.assertEqual(self.activity_backend.ended, self.activity_backend.begun)
        self.assertEqual(self.activity_backend.released, self.activity_backend.begun)

    def test_tabs_keep_header_and_only_show_relevant_work_area(self):
        window = self.window
        self.assertTrue(window.video.isVisible())
        self.assertFalse(window.inspector.isVisible())
        for title, control in (("Подключение", window.profile), ("Видео", window.width),
                               ("Звук", window.volume)):
            self.select_tab(title)
            self.assertTrue(window.header.isVisible())
            self.assertTrue(window.video.isVisible())
            self.assertTrue(window.inspector.isVisible())
            self.assertTrue(control.isVisible())
            self.assertFalse(window.log.isVisible())
        self.select_tab("Диагностика")
        self.assertTrue(window.header.isVisible())
        self.assertTrue(window.log.isVisible())
        self.assertFalse(window.video.isVisible())
        self.assertFalse(window.inspector.isVisible())
        self.select_tab("Экран")
        self.assertTrue(window.video.isVisible())
        self.assertFalse(window.inspector.isVisible())
        self.assertFalse(window.log.isVisible())

    def test_saved_settings_restore_connection_and_shared_audio_controls(self):
        window = self.window
        window.transport.setCurrentIndex(window.transport.findData("tcp"))
        window.host.setText("127.0.0.2")
        window.port.setValue(8123)
        window.profile.setCurrentIndex(window.profile.findData("v1"))
        window.touch_profile.setCurrentIndex(window.touch_profile.findData("legacy-dual"))
        window.width.setValue(640); window.height.setValue(360); window.fps.setValue(24)
        window.fit.setCurrentIndex(window.fit.findData("crop"))
        window.rotation.setCurrentIndex(window.rotation.findData(90))
        window.header_volume.setValue(23)
        window.sound_enabled.setChecked(False)
        window.fallback.setChecked(False)
        window.close()
        self.wait_for(lambda: not window.isVisible() and not window.busy)
        dispose_qt_object(window)
        saved_path = Path(self.tmp.name) / "settings.json"
        saved = json.loads(saved_path.read_text())
        self.assertNotIn("aoa_start", saved)
        # A legacy preference must never turn off automatic desktop AOA.
        saved["aoa_start"] = False
        saved_path.write_text(json.dumps(saved))
        self.window = window = desktop.HeadUnitWindow(Path(self.tmp.name))
        window.show()
        self.wait_for(lambda: not window.busy)
        config = window.configuration()
        self.assertEqual((config.transport, config.host, config.port), ("tcp", "127.0.0.2", 8123))
        self.assertFalse(config.aoa_start)
        self.assertEqual((config.width, config.height, config.fps), (640, 360, 24))
        self.assertEqual((config.profile, config.touch_profile), ("v1", "legacy-dual"))
        self.assertFalse(config.allow_mode_fallback)
        self.assertTrue(window.tcp_group.isVisible())
        self.assertFalse(window.usb_group.isVisible())
        self.assertEqual((window.volume.value(), window.header_volume.value()), (23, 23))
        self.assertEqual(window.header_volume_label.text(), "23%")
        self.assertTrue(window.muted.isChecked())
        self.assertFalse(window.sound_enabled.isChecked())
        self.assertEqual((window.fit.currentData(), window.rotation.currentData()), ("crop", 90))
        window.volume.setValue(31)
        window.muted.setChecked(False)
        self.assertEqual(window.header_volume.value(), 31)
        self.assertTrue(window.sound_enabled.isChecked())
        window.transport.setCurrentIndex(window.transport.findData("usb"))
        window.usb_devices.clear()
        window.usb_devices.addItem("Selected phone", dict(
            bus=1, ports=[2, 3], vid=0x1234, pid=0x5678, address=4))
        self.assertTrue(window.configuration().aoa_start)

    def test_decoder_wakes_display_without_periodic_timer_and_disconnect_releases_activity(self):
        window = self.window
        first, peer = self.start_demo()
        self.assertTrue(window.activity.snapshot()["active"])
        before = window.video.index
        window.timer.stop()
        try:
            self.wait_for(lambda: window.video.index >= before + 3)
            self.assertGreater(window.frame_wake.snapshot()["consumed"], 0)
        finally:
            window.timer.start(33)
        for control in (window.transport, window.usb_devices, window.host, window.port, window.profile):
            self.assertFalse(control.isEnabled())
        QTest.mouseClick(window.disconnect_button, Qt.LeftButton)
        self.wait_for(lambda: window.client is None and not window.busy)
        self.assertTrue(first.stopped)
        self.assertFalse(peer.thread.is_alive())
        self.assertFalse(window.activity.snapshot()["active"])
        self.assertEqual(self.activity_backend.ended, self.activity_backend.begun)
        self.assertEqual(self.activity_backend.released, self.activity_backend.begun)
        self.assertIsNone(window.video.image)
        saved_report = json.loads((first.output_dir / "desktop-summary.json").read_text(encoding="utf-8"))
        self.assertFalse(saved_report["macos_activity"]["active"])
        self.assertEqual(saved_report["macos_activity"]["status"], "closed")
        self.assertGreater(saved_report["frame_wake"]["consumed"], 0)
        self.assertTrue(window.connect_button.isEnabled())
        self.assertTrue(window.transport.isEnabled())
        second, _ = self.start_demo()
        self.assertIsNot(first, second)
        self.assertTrue(window.activity.snapshot()["active"])
        self.assertEqual(len(self.activity_backend.begun), 2)

    def test_activity_failure_is_diagnostic_and_does_not_prevent_streaming(self):
        window = self.window
        with patch.object(self.activity_backend, "begin", side_effect=RuntimeError("Synthetic activity failure")):
            client, _ = self.start_demo()
        self.assertFalse(window.activity.snapshot()["active"])
        self.assertIn("Synthetic activity failure", window.log.toPlainText())
        QTest.mouseClick(window.disconnect_button, Qt.LeftButton)
        self.wait_for(lambda: window.client is None and not window.busy)
        saved_report = json.loads((client.output_dir / "desktop-summary.json").read_text(encoding="utf-8"))
        self.assertEqual(saved_report["macos_activity"]["status"], "failed")
        self.assertIn("Synthetic activity failure", saved_report["macos_activity"]["error"])
        self.assertGreater(saved_report["decoded_frames"], 0)
        self.assertFalse(saved_report["desktop_cleanup_errors"])

    def test_disconnect_marks_demo_closing_before_client_socket_shutdown(self):
        client, peer = self.start_demo()
        abort = threading.Event()
        send, stop = peer._send, client.stop
        error = OSError('Windows established connection aborted')
        error.winerror = 10053

        def send_until_close(*packets):
            if abort.is_set():
                raise error
            return send(*packets)

        def stop_client():
            # Emulate Windows aborting the peer's next socket operation while
            # the client closes, before disconnect calls DemoPeer.close().
            abort.set()
            peer.thread.join(2)
            self.assertFalse(peer.thread.is_alive())
            stop()

        with patch.object(peer, '_send', side_effect=send_until_close), \
                patch.object(client, 'stop', side_effect=stop_client):
            self.window.disconnect_session()
            self.wait_for(lambda: self.window.client is None and not self.window.busy)
        report = self.window.last_report
        self.assertGreater(report['decoded_frames'], 0)
        self.assertEqual(report['state'], 'DISCONNECTED')
        self.assertIsNone(report['error'])
        self.assertIsNone(report['demo_error'])
        self.assertFalse(report['desktop_cleanup_errors'])
        self.assertFalse(report['workers_alive'])

    def test_controls_reset_resize_reconnect_and_async_close(self):
        window = self.window
        first, peer = self.start_demo()
        QTest.mouseClick(window.video, Qt.LeftButton, pos=window.video.rect().center())
        self.wait_for(lambda: sum(message == ACTION3 for message, _ in peer.commands) >= 2)
        self.assertFalse(first.pending_touch)
        self.assertIsNone(peer.error)

        QTest.mouseClick(window.stream_buttons[1], Qt.LeftButton)
        self.wait_for(lambda: any(message == HARD_KEY and fields(payload).get(1) == 14
                                 for message, payload in peer.commands))

        QTest.mouseClick(window.pause_button, Qt.LeftButton)
        self.wait_for(lambda: first.session.state == "PAUSED" and
                      any(message == VIDEO_PAUSE for message, _ in peer.commands) and
                      not window.stream_buttons[0].isEnabled())
        self.assertFalse(window.stream_buttons[0].isEnabled())
        before = first.decoded_frames
        QTest.mouseClick(window.pause_button, Qt.LeftButton)
        self.wait_for(lambda: first.session.state == "STREAMING" and first.decoded_frames >= before + 3)

        self.select_tab("Видео")
        generation, before = first.decoder_generation, first.decoded_frames
        QTest.mouseClick(window.reset_button, Qt.LeftButton)
        self.wait_for(lambda: any(message == VIDEO_RESET for message, _ in peer.commands)
                      and first.decoded_frames >= before + 3)
        self.assertEqual(first.decoder_generation, generation)
        self.assertIsNone(first.error)
        self.assertIsNone(peer.error)

        window.width.setValue(800)
        window.height.setValue(480)
        window.fps.setValue(20)
        QTest.mouseClick(window.apply_button, Qt.LeftButton)
        self.wait_for(lambda: window.client is not None and window.client is not first
                      and window.client.decoded_frames >= 3, 12)
        second = window.client
        self.assertEqual((second.session.width, second.session.height, second.session.fps), (800, 480, 20))
        self.assertTrue(first.stopped)
        self.assertTrue(peer.halt.is_set())
        self.assertFalse(peer.thread.is_alive())
        self.assertEqual(len(window.history), 1)
        self.assertIsNone(second.error)
        self.assertFalse(window.busy)

        window.close()
        self.wait_for(lambda: window.client is None and not window.busy and not window.isVisible(), 12)
        self.assertTrue(second.stopped)
        self.assertFalse(window.timer.isActive())
        self.assertTrue(window.audio.closed)
        self.assertEqual(len(window.history), 2)

    def test_close_while_connection_worker_is_pending_finishes_cleanup(self):
        window = self.window
        window.connect_session()
        self.assertTrue(window.busy)
        window.close()
        self.assertTrue(window.closing)
        self.wait_for(lambda: not window.busy and window.client is None and not window.isVisible(), 12)
        self.assertEqual(len(window.history), 1)
        self.assertFalse(window.pending)
        self.assertTrue(window.audio.closed)


if __name__ == "__main__":
    unittest.main()
