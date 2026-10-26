"""Audio queue and Qt output contract checks, without opening speakers or mic."""
from types import SimpleNamespace
import os
import threading
import unittest
from unittest.mock import patch

# Share a widget-capable application with later UI tests in a combined suite.
# No window is shown by these tests and native speaker sinks remain mocked.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from hu_lab import desktop_audio as audio
from hu_lab.protocol import Packet, MEDIA, TTS, VR, protobuf


def init(channel=MEDIA, rate=48000, channels=2, bits=16):
    return Packet(channel, audio._IDS[channel][0], protobuf(f1=rate, f2=channels, f3=bits))


class PCMPlaybackTest(unittest.TestCase):
    def test_fragmented_stereo_frames_survive_packet_and_read_boundaries(self):
        queue = audio.PCMPlaybackBuffer()
        queue.submit(init())
        queue.submit(Packet(MEDIA, audio.MEDIA_DATA, b"\x01\x00\x02"))
        self.assertEqual(queue.read(MEDIA, 1, 100), b"")
        queue.submit(Packet(MEDIA, audio.MEDIA_DATA, b"\x00\x03\x00\x04\x00"))
        self.assertEqual(queue.read(MEDIA, 1, 7), b"\x01\x00\x02\x00")
        self.assertEqual(queue.read(MEDIA, 1, 100), b"\x03\x00\x04\x00")
        self.assertEqual(queue.snapshot()["MEDIA"]["partial_bytes"], 0)

    def test_overflow_keeps_newest_whole_stereo_frames(self):
        queue = audio.PCMPlaybackBuffer(max_seconds=.001)
        queue.submit(init(rate=8000))  # 32 bytes / eight frames maximum.
        queue.submit(Packet(MEDIA, audio.MEDIA_DATA, bytes(range(24))))
        queue.submit(Packet(MEDIA, audio.MEDIA_DATA, bytes(range(24, 48))))
        self.assertEqual(queue.snapshot()["MEDIA"]["dropped_bytes"], 16)
        self.assertEqual(queue.read(MEDIA, 1, 100), bytes(range(16, 48)))

    def test_one_oversized_chunk_is_bounded_and_keeps_frame_alignment(self):
        queue = audio.PCMPlaybackBuffer(max_seconds=.001)
        queue.submit(init(rate=8000))
        queue.submit(Packet(MEDIA, audio.MEDIA_DATA, bytes(range(81))))
        self.assertEqual(queue.read(MEDIA, 1, 100), bytes(range(48, 80)))
        row = queue.snapshot()["MEDIA"]
        self.assertEqual((row["partial_bytes"], row["dropped_bytes"]), (1, 48))

    def test_stale_chunks_expire_after_main_thread_stall(self):
        now = [0.]
        queue = audio.PCMPlaybackBuffer(clock=lambda: now[0])
        queue.submit(init())
        queue.submit(Packet(MEDIA, audio.MEDIA_DATA, b"old!"))
        now[0] = .3
        queue.submit(Packet(MEDIA, audio.MEDIA_DATA, b"new!"))
        self.assertEqual(queue.read(MEDIA, 1, 100), b"new!")
        self.assertEqual(queue.snapshot()["MEDIA"]["dropped_bytes"], 4)

    def test_pause_resume_stop_and_generation_fence(self):
        queue = audio.PCMPlaybackBuffer()
        queue.submit(init())
        queue.submit(Packet(MEDIA, audio.MEDIA_DATA, b"old!x"))
        queue.submit(Packet(MEDIA, audio.MEDIA_PAUSE))
        queue.submit(Packet(MEDIA, audio.MEDIA_DATA, b"skip"))
        queue.submit(Packet(MEDIA, audio.MEDIA_RESUME))
        queue.submit(Packet(MEDIA, audio.MEDIA_DATA, b"new!"))
        generation = queue.state(MEDIA)[0]
        self.assertEqual(queue.read(MEDIA, 1, 100), b"")
        self.assertEqual(queue.read(MEDIA, generation, 100), b"new!")
        self.assertEqual(queue.snapshot()["MEDIA"]["dropped_bytes"], 9)
        queue.submit(Packet(MEDIA, audio.MEDIA_STOP))
        queue.submit(Packet(MEDIA, audio.MEDIA_RESUME))
        self.assertEqual(queue.state(MEDIA)[1], "DISABLED")

    def test_tts_vr_independent_formats_and_stops(self):
        queue = audio.PCMPlaybackBuffer()
        queue.submit(init(TTS, rate=16000, channels=1))
        queue.submit(init(VR, rate=8000, channels=1, bits=8))
        queue.submit(Packet(TTS, audio.TTS_DATA, b"\x01\x00"))
        queue.submit(Packet(VR, audio.VR_DATA, b"\x80\x90\x80"))
        queue.submit(Packet(TTS, audio.TTS_STOP))
        self.assertEqual(queue.read(TTS, queue.state(TTS)[0], 100), b"")
        self.assertEqual(queue.read(VR, 1, 100), b"\x80\x90\x80")
        queue.submit(Packet(VR, audio.VR_STOP))
        self.assertEqual(queue.state(VR)[1], "STOPPED")

    def test_bad_lane_recovers_only_after_valid_init_and_other_lanes_continue(self):
        queue = audio.PCMPlaybackBuffer()
        queue.submit(init(TTS))
        queue.submit(Packet(MEDIA, audio.MEDIA_DATA, b"bad!"))
        queue.submit(Packet(TTS, audio.TTS_DATA, b"good"))
        self.assertEqual(queue.state(MEDIA)[1], "DISABLED")
        self.assertEqual(queue.read(TTS, 1, 100), b"good")
        queue.submit(init(bits=24))
        self.assertEqual(queue.state(MEDIA)[1], "DISABLED")
        queue.submit(init())
        queue.submit(Packet(MEDIA, audio.MEDIA_DATA, b"okay"))
        self.assertEqual(queue.read(MEDIA, queue.state(MEDIA)[0], 100), b"okay")

    def test_close_rejects_packets_and_reset_requires_new_init(self):
        queue = audio.PCMPlaybackBuffer()
        queue.submit(init())
        queue.reset(close=True)
        queue.submit(init())
        queue.submit(Packet(MEDIA, audio.MEDIA_DATA, b"nope"))
        self.assertEqual(queue.state(MEDIA)[1], "CLOSED")
        queue.reset()
        queue.submit(Packet(MEDIA, audio.MEDIA_DATA, b"nope"))
        self.assertEqual(queue.state(MEDIA)[1], "DISABLED")

    def test_concurrent_ingestion_has_exact_byte_accounting(self):
        queue = audio.PCMPlaybackBuffer(max_seconds=2, max_age=2)
        queue.submit(init())
        def producer():
            for _ in range(300):
                queue.submit(Packet(MEDIA, audio.MEDIA_DATA, b"abcd"))
        workers = [threading.Thread(target=producer) for _ in range(3)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        self.assertEqual(queue.read(MEDIA, 1, 10000), b"abcd" * 900)
        row = queue.snapshot()["MEDIA"]
        self.assertEqual(row["received_bytes"], row["dequeued_bytes"])
        self.assertEqual(row["dropped_bytes"], 0)


class _FakeDevice:
    def id(self): return b"test-device"
    def description(self): return "Test output"
    def isNull(self): return False
    def isFormatSupported(self, fmt): return True


class _FakeDevices:
    def __init__(self, parent):
        self.audioOutputsChanged = SimpleNamespace(connect=lambda callback: None)
    @staticmethod
    def defaultAudioOutput(): return _FakeDevice()
    @staticmethod
    def audioOutputs(): return [_FakeDevice()]


class _FakeSink:
    instances = []
    def __init__(self, device, fmt, parent):
        self.data = bytearray()
        self.was_reset = False
        self.volume = None
        self.instances.append(self)
    def setBufferSize(self, value): pass
    def setVolume(self, value): self.volume = value
    def start(self): return self
    def error(self): return SimpleNamespace(name="NoError")
    def state(self): return SimpleNamespace(name="ActiveState")
    def reset(self): self.was_reset = True
    def deleteLater(self): pass
    def bytesFree(self): return 128
    def write(self, data):
        # An odd, short write exposes dropped/duplicated stereo sample bugs.
        count = min(3, len(data))
        self.data.extend(data[:count])
        return count


@unittest.skipIf(audio.QTimer is None, "desktop Qt runtime is optional")
class QtPlaybackContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.devices = patch.object(audio, "QMediaDevices", _FakeDevices)
        self.sinks = patch.object(audio, "QAudioSink", _FakeSink)
        self.devices.start()
        self.sinks.start()
        _FakeSink.instances = []
        self.player = audio.DesktopAudioPlayer()
        self.player.timer.stop()

    def tearDown(self):
        self.player.close()
        self.devices.stop()
        self.sinks.stop()

    def test_short_device_writes_preserve_every_byte_and_volume(self):
        self.player.set_volume(.4)
        self.player.submit(init())
        self.player.submit(Packet(MEDIA, audio.MEDIA_DATA, b"12345678"))
        for _ in range(4):
            self.player._pump()
        sink = _FakeSink.instances[0]
        self.assertEqual(bytes(sink.data), b"12345678")
        self.assertEqual(sink.volume, .4)
        self.player.set_muted(True)
        self.assertEqual(sink.volume, 0)
        self.player.set_muted(False)
        self.assertEqual(sink.volume, .4)

    def test_reinit_discards_old_partial_device_write(self):
        self.player.submit(init())
        self.player.submit(Packet(MEDIA, audio.MEDIA_DATA, b"old-data"))
        self.player._pump()  # old sink accepts three bytes only.
        old = _FakeSink.instances[0]
        self.player.submit(init(rate=16000, channels=1))
        self.player.submit(Packet(MEDIA, audio.MEDIA_DATA, b"NEW!"))
        self.player._pump()
        self.player._pump()
        self.assertTrue(old.was_reset)
        self.assertEqual(bytes(_FakeSink.instances[-1].data), b"NEW!")

    def test_disconnected_output_is_visible_without_silent_fallback(self):
        self.player.set_output_device("missing")
        self.player.submit(init())
        self.player._pump()
        row = self.player.snapshot()["lanes"]["MEDIA"]
        self.assertIn("disconnected", row["output_error"])
        self.assertEqual(len(_FakeSink.instances), 0)
        self.player.set_output_device(None)
        self.player._pump()
        self.assertIsNone(self.player.snapshot()["lanes"]["MEDIA"]["output_error"])

    def test_new_session_resets_counters_and_old_sink(self):
        self.player.submit(init())
        self.player.submit(Packet(MEDIA, audio.MEDIA_DATA, b"1234"))
        self.player._pump()
        old_sink = _FakeSink.instances[0]
        self.player.reset()
        row = self.player.snapshot()["lanes"]["MEDIA"]
        self.assertTrue(old_sink.was_reset)
        self.assertEqual(row["state"], "IDLE")
        for key in ("packets", "received_bytes", "written_bytes", "dequeued_bytes", "dropped_bytes", "errors"):
            self.assertEqual(row[key], 0, key)


if __name__ == "__main__":
    unittest.main()
