"""Live touch lifecycle through the real HU client and independent wire decoder.

The client's wire send is captured before transport acquisition. These tests
open no USB, network, audio device, or Qt window.
"""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from hu_lab.desktop_input import DesktopTouchInput
from hu_lab.desktop_session import DesktopConfig, create_client
from hu_lab.geometry import Rect
from hu_lab.protocol import ProtocolError
from hu_lab.touch import CANCEL, DOWN, MOVE, POINTER_DOWN, POINTER_UP, UP, TouchDecoder


class DesktopTouchInputTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.number = 0

    def client(self, profile="legacy-dual"):
        self.number += 1
        client = create_client(DesktopConfig(width=320, height=240, touch_profile=profile),
                               Path(self.temporary.name) / str(self.number))
        client.session.state = "STREAMING"
        client.packets = []
        sender = patch.object(client, "send", side_effect=client.packets.append)
        sender.start()
        self.addCleanup(sender.stop)
        def cleanup():
            client.halt.set()
            client.stop()
        self.addCleanup(cleanup)
        return client

    def decode(self, client):
        decoder = TouchDecoder(client.touch_profile)
        frames = [decoder.decode(packet) for packet in client.packets]
        return frames, decoder

    def test_independent_contacts_keep_ids_when_qt_order_changes_and_first_finger_lifts(self):
        for profile in ("legacy-dual", "action3-multi"):
            with self.subTest(profile=profile):
                client = self.client(profile)
                control = DesktopTouchInput()
                g = client.decoder_generation
                control.contacts(client, [(42, 10, 20)], g)
                control.contacts(client, [(42, 12, 21), (81, 200, 150)], g)
                control.contacts(client, [(81, 210, 160), (42, 20, 25)], g)
                control.contacts(client, [(81, 220, 165)], g)
                control.contacts(client, [(81, 230, 170)], g)
                control.finish()
                frames, decoder = self.decode(client)
                self.assertEqual([f.action for f in frames],
                                 [DOWN, POINTER_DOWN, MOVE, POINTER_UP, MOVE, UP])
                self.assertEqual([(p.id, p.x, p.y) for p in frames[2].pointers],
                                 [(0, 20, 25), (1, 210, 160)])
                self.assertEqual(frames[3].action_index, 0)
                self.assertEqual(frames[-1].pointers[0].id, 1)
                self.assertFalse(decoder.tracker.active)
                self.assertFalse(client.pending_touch)
                self.assertFalse(control.active)

    def test_contact_replacement_releases_before_reusing_wire_id(self):
        client = self.client()
        control = DesktopTouchInput()
        g = client.decoder_generation
        control.contacts(client, [(10, 20, 20), (20, 100, 100)], g)
        control.contacts(client, [(20, 105, 110), (30, 200, 120)], g)
        control.finish()
        frames, decoder = self.decode(client)
        self.assertEqual([f.action for f in frames],
                         [DOWN, POINTER_DOWN, POINTER_UP, POINTER_DOWN, POINTER_UP, UP])
        self.assertEqual([p.id for p in frames[3].pointers], [1, 0])
        self.assertFalse(decoder.tracker.active)

    def test_lift_uses_qt_final_position_without_creating_an_extra_contact(self):
        client = self.client()
        control = DesktopTouchInput()
        g = client.decoder_generation
        control.contacts(client, [(10, 20, 20), (20, 100, 100)], g)
        control.contacts(client, [(20, 105, 110)], g, released=[(10, 25, 35)])
        control.contacts(client, [], g, released=[(20, 120, 130)])
        frames, decoder = self.decode(client)
        self.assertEqual([f.action for f in frames], [DOWN, POINTER_DOWN, POINTER_UP, UP])
        self.assertEqual((frames[2].pointers[0].x, frames[2].pointers[0].y), (25, 35))
        self.assertEqual((frames[3].pointers[0].x, frames[3].pointers[0].y), (120, 130))
        self.assertFalse(decoder.tracker.active)

    def test_alt_drag_and_incremental_zoom_send_two_continuously_moving_contacts(self):
        for profile in ("legacy-dual", "action3-multi"):
            with self.subTest(profile=profile):
                client = self.client(profile)
                control = DesktopTouchInput()
                control.begin_pinch(client, 160, 120, client.decoder_generation)
                initial = control.pointers
                control.drag_pinch(180, 120)
                wider = control.pointers
                control.scale_pinch(.5)
                narrower = control.pointers
                self.assertGreater(wider[1].x - wider[0].x, initial[1].x - initial[0].x)
                self.assertLess(narrower[1].x - narrower[0].x, wider[1].x - wider[0].x)
                control.drag_pinch(175, 135)
                self.assertNotEqual(control.pointers[0].y, control.pointers[1].y)
                control.finish()
                frames, decoder = self.decode(client)
                self.assertEqual([f.action for f in frames],
                                 [DOWN, POINTER_DOWN, MOVE, MOVE, MOVE, POINTER_UP, UP])
                self.assertTrue(all(len(f.pointers) == 2 for f in frames if f.action == MOVE))
                self.assertFalse(decoder.tracker.active)

    def test_zoom_at_source_letterbox_edges_never_sends_outside_content(self):
        client = self.client()
        client.source_size = (1920, 1080)
        client.source_policy = "fit"
        # The 320x240 stream has active 320x180 source content at y=30..209.
        for x, y in ((0, 30), (319, 30), (0, 209), (319, 209), (160, 120)):
            with self.subTest(point=(x, y)):
                control = DesktopTouchInput()
                control.begin_pinch(client, x, y, client.decoder_generation)
                for factor in (100, .001, 4, 100):
                    control.scale_pinch(factor)
                control.drag_pinch(x + 1000, y + 1000)
                control.finish()
        frames, decoder = self.decode(client)
        self.assertTrue(all(client.input_content_contains(p.x, p.y)
                            for frame in frames for p in frame.pointers))
        self.assertFalse(client.pending_touch)
        self.assertFalse(decoder.tracker.active)
        before = len(client.packets)
        with self.assertRaisesRegex(ProtocolError, "outside active content"):
            control.begin_pinch(client, 160, 10, client.decoder_generation)
        self.assertEqual(len(client.packets), before)

    def test_invalid_or_stale_updates_cancel_live_contacts_without_new_down(self):
        for invalid in ([(0, -1, 100), (1, 180, 100)],
                        [(0, 10, 20), (0, 30, 40)],
                        [(0, float("nan"), 20)],
                        [(0, 10, 20), (1, 20, 30), (2, 40, 50)]):
            with self.subTest(invalid=invalid):
                client = self.client()
                control = DesktopTouchInput()
                control.begin_pinch(client, 160, 120, client.decoder_generation)
                with self.assertRaises(ProtocolError):
                    control.contacts(client, invalid, client.decoder_generation)
                frames, decoder = self.decode(client)
                self.assertEqual(frames[-1].action, CANCEL)
                self.assertFalse(decoder.tracker.active)
                self.assertFalse(control.active)
        client = self.client()
        control = DesktopTouchInput()
        control.begin_pinch(client, 160, 120, client.decoder_generation)
        with self.assertRaisesRegex(ProtocolError, "generation"):
            control.contacts(client, [(0, 10, 20)], client.decoder_generation + 1)
        self.assertEqual(self.decode(client)[0][-1].action, CANCEL)
        self.assertFalse(control.active)

    def test_displayed_crop_limits_generated_pair_without_extending_md_content(self):
        client = self.client()
        client.source_size = (1920, 1080)
        client.source_policy = "fit"
        control = DesktopTouchInput()
        crop = Rect(100, 0, 100, 240)
        control.begin_pinch(client, 101, 31, client.decoder_generation, bounds=crop)
        control.scale_pinch(100)
        control.drag_pinch(200, 160)
        control.finish()
        frames, _ = self.decode(client)
        self.assertTrue(all(crop.contains(p.x, p.y) and client.input_content_contains(p.x, p.y)
                            for frame in frames for p in frame.pointers))
        before = len(client.packets)
        with self.assertRaisesRegex(ProtocolError, "displayed crop"):
            control.begin_pinch(client, 50, 120, client.decoder_generation, bounds=crop)
        self.assertEqual(len(client.packets), before)

    def test_single_profile_rejects_multi_before_sending_but_accepts_touchscreen_tap(self):
        client = self.client("legacy-single")
        control = DesktopTouchInput()
        with self.assertRaisesRegex(ProtocolError, "Legacy dual"):
            control.begin_pinch(client, 160, 120, client.decoder_generation)
        self.assertFalse(client.packets)
        control.contacts(client, [(44, 120, 100)], client.decoder_generation)
        control.finish()
        self.assertEqual([f.action for f in self.decode(client)[0]], [DOWN, UP])

    def test_failed_second_contact_cancels_the_successful_first_contact(self):
        client = self.client()
        control = DesktopTouchInput()
        failed = False
        def send(packet):
            nonlocal failed
            if len(client.packets) == 1 and not failed:
                failed = True
                raise OSError("synthetic send failure")
            client.packets.append(packet)
        with patch.object(client, "send", side_effect=send):
            with self.assertRaisesRegex(OSError, "synthetic send failure"):
                control.begin_pinch(client, 160, 120, client.decoder_generation)
        self.assertEqual([f.action for f in self.decode(client)[0]], [DOWN, CANCEL])
        self.assertFalse(client.pending_touch)
        self.assertFalse(control.active)
        self.assertFalse(control.pinching)

    def test_explicit_cancel_is_idempotent_and_clears_pinch(self):
        client = self.client()
        control = DesktopTouchInput()
        control.begin_pinch(client, 160, 120, client.decoder_generation)
        control.cancel("viewport_changed")
        before = len(client.packets)
        control.cancel("focus_lost")
        control.finish()
        self.assertEqual(len(client.packets), before)
        self.assertFalse(control.active)
        self.assertFalse(control.scale_pinch(2))
        self.assertEqual(self.decode(client)[0][-1].action, CANCEL)


if __name__ == "__main__":
    unittest.main()
