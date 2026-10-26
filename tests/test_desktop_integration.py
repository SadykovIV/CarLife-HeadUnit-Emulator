"""Local synthetic MD → real framing/decoder/audio/control integration.

These tests do not create Qt windows, open audio devices, or access USB/ADB.
The peer listens on a fresh ephemeral loopback port for each session.
"""
from contextlib import contextmanager
import tempfile
import time
import unittest

from hu_lab.desktop_audio import PCMPlaybackBuffer
from hu_lab.desktop_demo import DemoPeer
from hu_lab.desktop_session import DesktopConfig, create_client
from hu_lab.protocol import HARD_KEY, TOUCH_ACTION, VIDEO_RESET, fields
from hu_lab.touch import ACTION3, two_pointer_gesture


def await_state(predicate, client, peer, timeout=4):
    deadline = time.monotonic() + timeout
    while not predicate():
        if client.error or peer.error:
            raise AssertionError(f"client={client.error!r}; demo={peer.error!r}")
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out: state={client.session.state}, frames={client.decoded_frames}")
        time.sleep(.01)


@contextmanager
def demo_session(profile="v1", width=320, height=240, touch_profile="legacy-single"):
    peer = DemoPeer(touch_profile=touch_profile)
    client = None
    with tempfile.TemporaryDirectory(prefix="headunit-integration-") as output:
        sink = PCMPlaybackBuffer()
        frames = []
        try:
            config = DesktopConfig(profile=profile, port=peer.port, width=width,
                                   height=height, fps=15, touch_profile=touch_profile)
            client = create_client(config, output, sink,
                on_decoded_frame=lambda record, pixels, w, h, stride: frames.append((record, w, h, stride)))
            client.start()
            await_state(lambda: client.decoded_frames >= 2 and sink.snapshot()["MEDIA"]["received_bytes"] > 0,
                        client, peer)
            yield client, peer, sink, frames
        finally:
            if client is not None:
                client.stop()
            peer.close()


class DesktopIntegrationTest(unittest.TestCase):
    def test_both_profiles_decode_audio_input_pause_and_resume(self):
        for profile in ("v1", "honda-v2"):
            with self.subTest(profile=profile), demo_session(profile) as (client, peer, sink, frames):
                self.assertEqual(client.session.accepted["width"], 320)
                self.assertEqual(client.session.accepted["fps"], 15)
                self.assertEqual(frames[-1][1:3], (320, 240))
                self.assertEqual(sink.snapshot()["MEDIA"]["format"],
                                 {"sample_rate": 48000, "channels": 2, "bits": 16})
                client.touch(0, 100, 120)
                client.touch(1, 100, 120)
                client.back()
                await_state(lambda: any(message == HARD_KEY for message, _ in peer.commands), client, peer)
                touches = [fields(payload) for message, payload in peer.commands if message == TOUCH_ACTION]
                self.assertEqual(touches[-2:], [{1: 0, 2: 100, 3: 120}, {1: 1, 2: 100, 3: 120}])
                self.assertEqual([fields(payload)[1] for message, payload in peer.commands if message == HARD_KEY][-1], 14)
                client.pause()
                await_state(lambda: sink.state(3)[1] == "PAUSED", client, peer)
                self.assertEqual(client.session.state, "PAUSED")
                before, generation = client.decoded_frames, client.decoder_generation
                client.resume()
                await_state(lambda: client.decoded_frames >= before + 2, client, peer)
                self.assertGreater(client.decoder_generation, generation)
                self.assertEqual(sink.state(3)[1], "PLAYING")
                self.assertIsNone(client.error)
                self.assertIsNone(peer.error)
                self.assertEqual(client.summary()["audio_callback"]["errors"], 0)

    def test_v2_keyframe_reset_keeps_monotonic_pts_and_decoding(self):
        with demo_session("honda-v2") as (client, peer, sink, frames):
            before = client.decoded_frames
            generation = client.decoder_generation
            client.reset()
            await_state(lambda: any(message == VIDEO_RESET for message, _ in peer.commands), client, peer)
            await_state(lambda: client.decoded_frames >= before + 3, client, peer)
            self.assertEqual(client.decoder_generation, generation)
            self.assertIsNone(client.error)
            self.assertIsNone(peer.error)

    def test_action3_gesture_does_not_parse_binary_touch_as_protobuf(self):
        with demo_session(touch_profile="action3-multi") as (client, peer, sink, frames):
            for frame in two_pointer_gesture(320, 240, "pinch-in", 0, 4, client.decoder_generation):
                client.send_touch_frame(frame)
                time.sleep(.01)
            await_state(lambda: any(message == ACTION3 for message, _ in peer.commands), client, peer)
            before = client.decoded_frames
            await_state(lambda: client.decoded_frames >= before + 2, client, peer)
            self.assertIsNone(client.error)
            self.assertIsNone(peer.error)

    def test_reconnection_negotiates_new_resolution_without_stale_frames(self):
        with demo_session(width=320, height=240) as (first, _, _, frames):
            first_id = first.session_id
            self.assertEqual(frames[-1][1:3], (320, 240))
        self.assertTrue(first.stopped)
        self.assertTrue(all(not thread.is_alive() for thread in first.threads))
        with demo_session(width=640, height=360) as (second, _, _, frames):
            self.assertNotEqual(first_id, second.session_id)
            self.assertEqual(frames[-1][1:3], (640, 360))
            self.assertEqual(second.session.requested["width"], 640)
            self.assertIsNone(second.error)


if __name__ == "__main__":
    unittest.main()
