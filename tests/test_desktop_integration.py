"""Local synthetic MD → real framing/decoder/audio/control integration.

These tests do not create Qt windows, open audio devices, or access USB/ADB.
The peer listens on a fresh ephemeral loopback port for each session.
"""
from contextlib import contextmanager
from fractions import Fraction
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from hu_lab.client import annexb_nals
from hu_lab.desktop_audio import PCMPlaybackBuffer
from hu_lab.desktop_demo import DemoPeer
from hu_lab.desktop_session import DesktopConfig, create_client
from hu_lab.protocol import (HARD_KEY, TOUCH_ACTION, VIDEO_RESET, VIDEO_START,
    CMD, VIDEO, VIDEO_DATA, MD_INFO, MEDIACODEC_AU_MARKER, Packet, ProtocolError, fields, protobuf)
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
    def held_frame_resume(self, profile, *, idr):
        with demo_session(profile) as (client, peer, sink, frames):
            entered, release = threading.Event(), threading.Event()
            held = []
            original = peer._send
            def send(*packets):
                for packet in packets:
                    if packet.channel != VIDEO or packet.message != VIDEO_DATA or entered.is_set():
                        continue
                    kinds = [kind for kind, _ in annexb_nals(packet.payload)]
                    if (5 in kinds) == idr:
                        held.append((packet, kinds))
                        entered.set()
                        if not release.wait(3):
                            raise RuntimeError('held demo AU was not released')
                return original(*packets)
            with patch.object(peer, '_send', side_effect=send):
                try:
                    self.assertTrue(entered.wait(3), peer.error)
                    before, generation = client.decoded_frames, client.decoder_generation
                    client.pause()
                    client.resume()
                finally:
                    release.set()
                await_state(lambda: client.decoded_frames >= before + 3 and
                            sum(message == VIDEO_START for message, _ in peer.commands) >= 2,
                            client, peer)
                self.assertIsNone(client.error)
                self.assertIsNone(peer.error)
                self.assertEqual(len(held), 1)
                if idr:
                    self.assertTrue(all(kind in held[0][1] for kind in (7, 8, 5)))
                    self.assertGreater(held[0][0].timestamp, 0)
                    self.assertEqual(client.resume_pts_restarts, 1)
                    self.assertEqual(client.decoder_generation, generation + 2)
                else:
                    self.assertNotIn(5, held[0][1])
                    self.assertGreaterEqual(client.resume_discarded_aus, 1)
                    self.assertGreaterEqual(client.resume_discarded_bytes, len(held[0][0].payload))
                    self.assertEqual(client.decoder_generation, generation + 1)

    def test_resume_discards_a_real_old_p_frame_already_in_flight(self):
        for profile in ('v1', 'honda-v2'):
            with self.subTest(profile=profile):
                self.held_frame_resume(profile, idr=False)

    def test_resume_accepts_one_new_encoder_after_an_old_idr_in_flight(self):
        for profile in ('v1', 'honda-v2'):
            with self.subTest(profile=profile):
                self.held_frame_resume(profile, idr=True)

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


class ResumeReferenceBoundsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import av
        codec = av.CodecContext.create('libx264', 'w')
        codec.width, codec.height, codec.pix_fmt = 320, 240, 'yuv420p'
        codec.time_base, codec.framerate = Fraction(1, 15), Fraction(15, 1)
        codec.options = {'preset': 'ultrafast', 'tune': 'zerolatency',
                         'x264-params': 'keyint=15:scenecut=0:repeat-headers=1'}
        packets = []
        for pts in (0, 1):
            frame = av.VideoFrame(320, 240, 'yuv420p')
            for index, plane in enumerate(frame.planes):
                plane.update(bytes([16 if index == 0 else 128]) * plane.buffer_size)
            frame.pts = pts
            packets.extend(bytes(packet) for packet in codec.encode(frame))
        cls.idr, cls.dependent = packets

    def setUp(self):
        self.output = tempfile.TemporaryDirectory()
        self.addCleanup(self.output.cleanup)
        self.client = create_client(DesktopConfig(width=320, height=240, fps=15), self.output.name)
        self.client.session.state = 'STREAMING'
        self.client._receive_packet(Packet(CMD, MD_INFO, protobuf(f15=MEDIACODEC_AU_MARKER)), None)
        def close():
            self.client.halt.set()
            self.client.stop()
        self.addCleanup(close)

    def receive(self, data, timestamp):
        self.client._receive_packet(Packet(VIDEO, VIDEO_DATA, data, timestamp), None)

    def resume(self):
        self.receive(self.idr, 100)
        self.client.pause()
        self.client.resume()

    def test_initial_dependent_au_stays_fatal_even_after_pause_without_a_first_frame(self):
        self.client.pause()
        self.client.resume()
        with self.assertRaisesRegex(ProtocolError, 'first complete AU'):
            self.receive(self.dependent, 0)

    def test_resume_does_not_allow_backwards_pts_on_a_dependent_frame(self):
        self.resume()
        self.receive(self.idr, 200)
        with self.assertRaisesRegex(ProtocolError, 'PTS moved backwards'):
            self.receive(self.dependent, 199)

    def test_resume_allows_only_one_header_proven_pts_restart(self):
        self.resume()
        self.receive(self.idr, 200)
        self.receive(self.idr, 100)
        self.assertEqual(self.client.resume_pts_restarts, 1)
        with self.assertRaisesRegex(ProtocolError, 'PTS moved backwards'):
            self.receive(self.idr, 0)

    def test_resume_wait_is_bounded_by_time_even_without_more_video(self):
        self.resume()
        with patch('hu_lab.client.time.monotonic', return_value=time.monotonic() + 9):
            with self.assertRaisesRegex(ProtocolError, 'resume SPS/PPS/IDR wait exceeded deadline'):
                self.client._resume_recovery_window()

    def test_resume_wait_is_bounded_by_bytes(self):
        self.resume()
        # A complete opaque dependent AU remains bounded even if its slice is
        # large. It is discarded before decoding and is never spliced or fixed.
        large_dependent = self.dependent + bytes(1024 * 1024)
        with self.assertRaisesRegex(ProtocolError, 'resume SPS/PPS/IDR wait exceeded byte_limit'):
            for _ in range(5):
                self.receive(large_dependent, 200)

    def test_pts_restart_is_rejected_after_the_resume_window_expires(self):
        self.resume()
        self.receive(self.idr, 200)
        with patch('hu_lab.client.time.monotonic', return_value=time.monotonic() + 9):
            with self.assertRaisesRegex(ProtocolError, 'PTS moved backwards'):
                self.receive(self.idr, 0)


if __name__ == "__main__":
    unittest.main()
