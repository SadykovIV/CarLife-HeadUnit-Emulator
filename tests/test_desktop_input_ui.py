"""Qt input/theme behavior with a real synthetic MD; never acquires USB.

The loopback demo runs the actual framing, video decoder and touch receiver.
USB discovery and the optional process-activity backend are isolated from the
host; audio is muted. Run from the published repository with desktop extras.
"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from tests import dispose_qt_object

try:
    import av
    from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
    from PySide6.QtGui import QInputDevice, QNativeGestureEvent, QTouchEvent, QWheelEvent
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from hu_lab import desktop
except ImportError:
    desktop = None

from hu_lab.macos_activity import UserInitiatedActivity
from hu_lab.protocol import FrameParser, Packet, TOUCH, TOUCH_ACTION
from hu_lab.touch import ACTION3, CANCEL, DOWN, MOVE, POINTER_DOWN, POINTER_UP, UP, TouchDecoder


@unittest.skipIf(desktop is None, "PySide6 and PyAV desktop extras are required")
class DesktopInputWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        discovery = patch.object(desktop, "list_usb_devices", return_value=[])
        discovery.start()
        self.addCleanup(discovery.stop)
        activity = patch.object(desktop, "UserInitiatedActivity", side_effect=lambda *args, **kwargs:
            UserInitiatedActivity(*args, **kwargs, platform="test"))
        activity.start()
        self.addCleanup(activity.stop)
        self.window = desktop.HeadUnitWindow(Path(self.tmp.name))
        self.window.muted.setChecked(True)
        self.window.transport.setCurrentIndex(self.window.transport.findData("demo"))
        self.window.width.setValue(320)
        self.window.height.setValue(240)
        self.window.fps.setValue(15)
        self.window.resize(1060, 680)
        self.window.show()
        self.app.processEvents()
        self.wait_for(lambda: not self.window.busy)

    def tearDown(self):
        self.window.close()
        self.wait_for(lambda: not self.window.isVisible() and not self.window.busy, 12)
        dispose_qt_object(self.window)

    def wait_for(self, predicate, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return
            QTest.qWait(10)
        client = self.window.client
        self.fail(f"input GUI timeout: busy={self.window.busy}, "
                  f"state={client.session.state if client else None}, "
                  f"client_error={client.error if client else None}, "
                  f"ui_error={self.window.error_label.text()}")

    def start_demo(self):
        QTest.mouseClick(self.window.connect_button, Qt.LeftButton)
        self.wait_for(lambda: self.window.client is not None and
                      self.window.client.decoded_frames >= 3 and self.window.video.transform is not None)
        self.assertIsNone(self.window.client.error)
        self.assertIsNone(self.window.demo.error)
        return self.window.client, self.window.demo

    def point(self, x=160, y=120):
        px, py = self.window.video.transform.video_to_view(x, y)
        return QPoint(round(px), round(py))

    def frames(self, client, peer):
        decoder = TouchDecoder(client.touch_profile)
        frames = [decoder.decode(Packet(TOUCH, message, payload))
                  for message, payload in tuple(peer.commands)
                  if message in (TOUCH_ACTION, ACTION3)]
        return frames, decoder

    def select_tab(self, text):
        tabs = self.window.tabs
        index = next(i for i in range(tabs.count()) if tabs.tabText(i) == text)
        QTest.mouseClick(tabs, Qt.LeftButton, pos=tabs.tabRect(index).center())
        self.app.processEvents()

    def alt_down(self):
        surface = self.window.video
        QTest.mousePress(surface, Qt.LeftButton, Qt.AltModifier, self.point())
        self.wait_for(lambda: surface.touch_input.active)
        self.assertEqual(len(surface.touch_input.pointers), 2)

    def wheel(self, delta):
        surface = self.window.video
        pos = self.point()
        event = QWheelEvent(QPointF(pos), QPointF(surface.mapToGlobal(pos)), QPoint(),
                            QPoint(0, delta), Qt.NoButton, Qt.ControlModifier,
                            Qt.ScrollPhase.ScrollUpdate, False)
        QApplication.sendEvent(surface, event)
        self.app.processEvents()

    def test_alt_drag_and_ctrl_wheel_emit_live_two_contact_sequences(self):
        client, peer = self.start_demo()
        surface = self.window.video
        self.alt_down()
        initial = surface.touch_input.pointers
        QTest.mouseMove(surface, self.point(200, 120))
        self.app.processEvents()
        wider = surface.touch_input.pointers
        self.assertGreater(wider[1].x - wider[0].x, initial[1].x - initial[0].x)
        QTest.mouseRelease(surface, Qt.LeftButton, Qt.AltModifier, self.point(210, 130))
        self.wait_for(lambda: len(peer.commands) >= 5 and not client.pending_touch)
        self.assertFalse(surface.touch_input.active)
        self.wheel(120)
        self.assertTrue(surface.touch_input.pinching)
        before = surface.touch_input.pointers
        self.wheel(-120)
        after = surface.touch_input.pointers
        self.assertLess(after[1].x - after[0].x, before[1].x - before[0].x)
        self.wait_for(lambda: not surface.touch_input.active and not client.pending_touch)
        self.wait_for(lambda: self.frames(client, peer)[0][-1].action == UP)
        frames, decoder = self.frames(client, peer)
        self.assertEqual(sum(frame.action == DOWN for frame in frames), 2)
        self.assertEqual(sum(frame.action == POINTER_DOWN for frame in frames), 2)
        self.assertTrue(any(frame.action == MOVE and len(frame.pointers) == 2 for frame in frames))
        self.assertFalse(decoder.tracker.active)
        self.assertIsNone(peer.error)

    def test_native_trackpad_zoom_uses_two_contacts_and_releases_on_end(self):
        client, peer = self.start_demo()
        surface = self.window.video
        device = QTest.createTouchDevice(QInputDevice.DeviceType.TouchPad)
        pos = QPointF(self.point())
        global_pos = QPointF(surface.mapToGlobal(self.point()))
        def native(kind, value=0):
            event = QNativeGestureEvent(kind, device, 2, pos, pos, global_pos, value, QPointF())
            QApplication.sendEvent(surface, event)
            self.app.processEvents()
        native(Qt.NativeGestureType.BeginNativeGesture)
        native(Qt.NativeGestureType.ZoomNativeGesture, .2)
        self.assertTrue(surface.touch_input.pinching)
        first = surface.touch_input.pointers
        native(Qt.NativeGestureType.ZoomNativeGesture, -.1)
        self.assertLess(surface.touch_input.pointers[1].x - surface.touch_input.pointers[0].x,
                        first[1].x - first[0].x)
        native(Qt.NativeGestureType.EndNativeGesture)
        self.wait_for(lambda: not client.pending_touch and len(self.frames(client, peer)[0]) >= 6)
        frames, decoder = self.frames(client, peer)
        self.assertEqual([f.action for f in frames], [DOWN, POINTER_DOWN, MOVE, MOVE, POINTER_UP, UP])
        self.assertFalse(decoder.tracker.active)

    def test_native_touchscreen_contacts_move_independently_and_keep_final_release_position(self):
        client, peer = self.start_demo()
        surface = self.window.video
        device = QTest.createTouchDevice()
        sequence = QTest.touchEvent(surface, device, False)
        sequence.press(0, self.point(80, 90), surface).press(1, self.point(220, 160), surface).commit()
        self.wait_for(lambda: surface.touch_input.active)
        sequence.move(0, self.point(100, 100), surface).move(1, self.point(230, 150), surface).commit()
        self.app.processEvents()
        sequence.release(0, self.point(110, 105), surface).stationary(1).commit()
        self.app.processEvents()
        self.assertEqual(len(surface.touch_input.pointers), 1)
        sequence.release(1, self.point(240, 140), surface).commit()
        self.wait_for(lambda: not client.pending_touch and len(self.frames(client, peer)[0]) >= 5)
        frames, decoder = self.frames(client, peer)
        self.assertEqual([f.action for f in frames], [DOWN, POINTER_DOWN, MOVE, POINTER_UP, UP])
        self.assertEqual((frames[-2].pointers[0].x, frames[-2].pointers[0].y), (110, 105))
        self.assertEqual((frames[-1].pointers[0].x, frames[-1].pointers[0].y), (240, 140))
        self.assertFalse(decoder.tracker.active)

    def test_touch_cancel_does_not_restart_on_late_update(self):
        client, peer = self.start_demo()
        surface = self.window.video
        device = QTest.createTouchDevice()
        sequence = QTest.touchEvent(surface, device, False)
        sequence.press(0, self.point(80, 90), surface).press(1, self.point(220, 160), surface).commit()
        self.wait_for(lambda: surface.touch_input.active)
        QApplication.sendEvent(surface, QTouchEvent(QEvent.TouchCancel, device))
        self.assertFalse(surface.touch_input.active)
        self.assertFalse(client.pending_touch)
        sequence.move(0, self.point(100, 110), surface).stationary(1).commit()
        sequence.release(0, self.point(100, 110), surface).release(1, self.point(220, 160), surface).commit()
        self.wait_for(lambda: len(self.frames(client, peer)[0]) >= 3)
        frames, decoder = self.frames(client, peer)
        self.assertEqual([f.action for f in frames], [DOWN, POINTER_DOWN, CANCEL])
        self.assertFalse(decoder.tracker.active)

    def test_tab_pause_and_disconnect_release_active_gestures(self):
        client, peer = self.start_demo()
        surface = self.window.video
        self.alt_down()
        self.select_tab("Видео")
        self.assertFalse(surface.touch_input.active)
        self.assertFalse(client.pending_touch)
        self.select_tab("Экран")
        self.wait_for(lambda: surface.transform is not None)
        self.alt_down()
        QTest.mouseClick(self.window.pause_button, Qt.LeftButton)
        self.wait_for(lambda: client.session.state == "PAUSED")
        self.assertFalse(surface.touch_input.active)
        self.assertFalse(client.pending_touch)
        QTest.mouseClick(self.window.pause_button, Qt.LeftButton)
        self.wait_for(lambda: client.session.state == "STREAMING" and surface.transform is not None)
        # Tab and pause keep the connection open, so verify remote receipt.
        self.wait_for(lambda: sum(frame.action == CANCEL for frame in self.frames(client, peer)[0]) == 2)
        self.assertFalse(self.frames(client, peer)[1].tracker.active)
        self.alt_down()
        QTest.mouseClick(self.window.disconnect_button, Qt.LeftButton)
        self.wait_for(lambda: self.window.client is None and not self.window.busy)
        self.assertFalse(surface.touch_input.active)
        self.assertFalse(surface.wheel_finish.isActive())
        self.assertFalse(client.pending_touch)
        self.assertEqual(client.commands.unfinished_tasks, 0)
        # Disconnect guarantees the final CANCEL is sent before socket closure,
        # but cannot guarantee that a peer scheduled later consumes it before
        # DemoPeer.close(). tx.bin is recorded only after sendall succeeds and
        # is drained by client.stop(), so this checks actual transmission.
        packets = FrameParser().feed((client.output_dir / "tx.bin").read_bytes())
        decoder = TouchDecoder(client.touch_profile)
        frames = [decoder.decode(packet) for packet in packets
                  if packet.message in (TOUCH_ACTION, ACTION3)]
        self.assertEqual(sum(frame.action == CANCEL for frame in frames), 3)
        self.assertEqual(sum(frame.action == DOWN for frame in frames), 3)
        self.assertFalse(decoder.tracker.active)

    def test_crop_and_rotation_keep_both_generated_contacts_visible(self):
        client, _ = self.start_demo()
        surface = self.window.video
        self.window.fit.setCurrentIndex(self.window.fit.findData("crop"))
        for rotation in (0, 90, 180, 270):
            self.window.rotation.setCurrentIndex(self.window.rotation.findData(rotation))
            self.app.processEvents()
            self.wait_for(lambda: surface.transform is not None)
            position = QPoint(8, surface.height() // 2)
            QTest.mousePress(surface, Qt.LeftButton, Qt.AltModifier, position)
            self.assertTrue(surface.touch_input.pinching)
            surface.touch_input.scale_pinch(100)
            # Bounds are quantized to the nearest video pixel. Its centre can
            # lie half a video pixel beyond a cropped edge; in Qt logical
            # coordinates that tolerance grows with the rendering scale.
            tolerance = surface.transform.scale / 2 + 1e-6
            for pointer in surface.touch_input.pointers:
                x, y = surface.transform.video_to_view(pointer.x, pointer.y)
                self.assertGreaterEqual(x, -tolerance)
                self.assertGreaterEqual(y, -tolerance)
                self.assertLessEqual(x, surface.width() + tolerance)
                self.assertLessEqual(y, surface.height() + tolerance)
                self.assertTrue(client.input_content_contains(pointer.x, pointer.y))
            QTest.mouseRelease(surface, Qt.LeftButton, Qt.AltModifier, position)
            self.assertFalse(client.pending_touch)

    def reopen(self, settings=None):
        self.window.close()
        self.wait_for(lambda: not self.window.isVisible() and not self.window.busy, 12)
        dispose_qt_object(self.window)
        if settings is not None:
            (Path(self.tmp.name) / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
        self.window = desktop.HeadUnitWindow(Path(self.tmp.name))
        self.window.muted.setChecked(True)
        self.window.show()
        self.app.processEvents()
        self.wait_for(lambda: not self.window.busy)

    def test_default_dual_and_legacy_settings_migration_preserve_explicit_new_choices(self):
        self.assertEqual(self.window.touch_profile.currentData(), "legacy-dual")
        self.reopen(dict(touch_profile="legacy-single"))
        self.assertEqual(self.window.touch_profile.currentData(), "legacy-dual")
        self.reopen(dict(touch_profile="legacy-single", settings_version=2))
        self.assertEqual(self.window.touch_profile.currentData(), "legacy-single")
        self.reopen(dict(touch_profile="action3-multi", settings_version=1))
        self.assertEqual(self.window.touch_profile.currentData(), "action3-multi")

    @staticmethod
    def rendered_color(widget, point=QPoint(2, 2)):
        image = widget.grab().toImage()
        ratio = image.devicePixelRatio()
        return image.pixelColor(round(point.x() * ratio), round(point.y() * ratio)).name()

    def test_light_theme_persists_blank_surface_but_does_not_recolor_video(self):
        window = self.window
        self.assertEqual(self.rendered_color(window.video), "#080e14")
        window.theme.setCurrentIndex(window.theme.findData("light"))
        self.app.processEvents()
        self.assertEqual(window.theme_variant, "light")
        self.assertEqual(self.rendered_color(window.video), "#edf2f8")
        self.reopen()
        window = self.window
        self.assertEqual(window.theme.currentData(), "light")
        self.assertEqual(self.rendered_color(window.video), "#edf2f8")
        client, _ = self.start_demo()
        QTest.mouseClick(window.pause_button, Qt.LeftButton)
        self.wait_for(lambda: client.session.state == "PAUSED")
        image = window.video.image.copy()
        light_video = self.rendered_color(window.video, self.point())
        self.assertEqual(self.rendered_color(window.video), "#080e14")
        window.theme.setCurrentIndex(window.theme.findData("dark"))
        self.app.processEvents()
        self.assertEqual(window.video.image, image)
        self.assertEqual(self.rendered_color(window.video, self.point()), light_video)
        window.theme.setCurrentIndex(window.theme.findData("system"))
        expected = "dark" if self.app.styleHints().colorScheme() == Qt.ColorScheme.Dark else "light"
        self.assertEqual(window.theme_variant, expected)
        self.assertEqual(window.video.image, image)


if __name__ == "__main__":
    unittest.main()
