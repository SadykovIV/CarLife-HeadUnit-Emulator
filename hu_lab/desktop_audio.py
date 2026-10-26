"""Bounded CarLife PCM playback through Qt/CoreAudio on macOS.

The transport thread only calls ``submit``. All QAudioSink objects are owned
and driven by the GUI thread. This module never opens a microphone. Incoming
VR is speaker audio; microphone uplink is a separate, unimplemented feature.
"""
from collections import deque
from dataclasses import asdict, dataclass, field
import math
import struct
import threading
import time

from .audio import (PcmFormat, MEDIA_INIT, MEDIA_DATA, MEDIA_STOP, MEDIA_PAUSE,
                    MEDIA_RESUME, TTS_INIT, TTS_DATA, TTS_STOP)
from .protocol import MEDIA, TTS, VR, Packet, ProtocolError, protobuf

VR_INIT, VR_DATA, VR_STOP = 0x50002, 0x50003, 0x50004
LANES = {MEDIA: "MEDIA", TTS: "TTS", VR: "VR"}
_IDS = {MEDIA: (MEDIA_INIT, MEDIA_DATA, MEDIA_STOP),
        TTS: (TTS_INIT, TTS_DATA, TTS_STOP), VR: (VR_INIT, VR_DATA, VR_STOP)}


@dataclass
class _Lane:
    state: str = "IDLE"
    format: PcmFormat | None = None
    generation: int = 0
    chunks: deque = field(default_factory=deque)
    size: int = 0
    partial: bytes = b""
    received_bytes: int = 0
    dequeued_bytes: int = 0
    dropped_bytes: int = 0
    packets: int = 0
    errors: int = 0
    last_error: str | None = None


class PCMPlaybackBuffer:
    """Independent lanes, at most 200 ms pending PCM per lane by default.

    Overflow removes oldest complete frames, preserving interleaved samples.
    Expired chunks are dropped instead of playing old sound after a GUI stall.
    A generation check prevents an old sink consuming a reconfigured stream.
    """
    def __init__(self, max_seconds=.2, max_age=.25, clock=time.monotonic):
        if not 0 < max_seconds <= 2 or not 0 < max_age <= 2:
            raise ValueError("invalid PCM queue limits")
        self.max_seconds, self.max_age, self.clock = max_seconds, max_age, clock
        self.lock = threading.RLock()
        self.lanes = {channel: _Lane() for channel in LANES}
        self.closed = False

    def _discard(self, lane):
        lane.dropped_bytes += lane.size + len(lane.partial)
        lane.chunks.clear()
        lane.size, lane.partial = 0, b""

    def _expire(self, lane):
        while lane.chunks and self.clock() - lane.chunks[0][0] > self.max_age:
            _, data = lane.chunks.popleft()
            lane.size -= len(data)
            lane.dropped_bytes += len(data)

    def submit(self, packet):
        if packet.channel not in self.lanes:
            return False
        with self.lock:
            lane = self.lanes[packet.channel]
            lane.packets += 1
            init, data_id, stop = _IDS[packet.channel]
            if packet.message == data_id:
                lane.received_bytes += len(packet.payload)
            if self.closed:
                if packet.message == data_id:
                    lane.dropped_bytes += len(packet.payload)
                return True
            try:
                if packet.message == init:
                    fmt = PcmFormat.parse(packet.payload)
                    self._discard(lane)
                    lane.format, lane.state, lane.last_error = fmt, "READY", None
                    lane.generation += 1
                elif packet.message == data_id:
                    limit = 100 * 1024 if packet.channel == MEDIA else 50 * 1024
                    if len(packet.payload) > limit:
                        lane.dropped_bytes += len(packet.payload)
                        raise ProtocolError("audio DATA exceeds channel bound")
                    if lane.state in ("PAUSED", "STOPPED", "DISABLED"):
                        lane.dropped_bytes += len(packet.payload)
                        return True
                    if lane.format is None:
                        lane.dropped_bytes += len(packet.payload)
                        raise ProtocolError("audio DATA before INIT")
                    frame = lane.format.frame_bytes
                    joined = lane.partial + packet.payload
                    count = len(joined) // frame * frame
                    pcm, lane.partial = joined[:count], joined[count:]
                    self._expire(lane)
                    maximum = max(frame, int(lane.format.bytes_per_second * self.max_seconds) // frame * frame)
                    if len(pcm) > maximum:
                        lane.dropped_bytes += len(pcm) - maximum
                        pcm = pcm[-maximum:]
                    excess = max(0, lane.size + len(pcm) - maximum)
                    while excess:
                        timestamp, chunk = lane.chunks.popleft()
                        discard = min(excess, len(chunk))
                        lane.size -= discard
                        lane.dropped_bytes += discard
                        excess -= discard
                        if discard < len(chunk):
                            lane.chunks.appendleft((timestamp, chunk[discard:]))
                    if pcm:
                        lane.chunks.append((self.clock(), pcm))
                        lane.size += len(pcm)
                        lane.state = "PLAYING"
                elif packet.message == stop or (packet.channel == MEDIA and packet.message in (MEDIA_PAUSE, MEDIA_RESUME)):
                    if packet.payload:
                        raise ProtocolError("audio control requires empty payload")
                    if lane.state == "DISABLED":
                        return True
                    if packet.message == MEDIA_RESUME and (lane.format is None or lane.state == "STOPPED"):
                        raise ProtocolError("audio RESUME requires initialized, non-stopped lane")
                    self._discard(lane)
                    lane.generation += 1
                    lane.state = ("STOPPED" if packet.message == stop else
                                  "PAUSED" if packet.message == MEDIA_PAUSE else "READY")
            except (ProtocolError, ValueError) as error:
                self._discard(lane)
                lane.state, lane.last_error = "DISABLED", str(error)
                lane.errors += 1
                lane.generation += 1
            return True

    def read(self, channel, generation, maximum):
        with self.lock:
            lane = self.lanes[channel]
            if lane.generation != generation or lane.state not in ("READY", "PLAYING") or lane.format is None:
                return b""
            self._expire(lane)
            maximum = min(maximum, lane.size)
            maximum = maximum // lane.format.frame_bytes * lane.format.frame_bytes
            chunks = []
            while maximum > 0 and lane.chunks:
                timestamp, chunk = lane.chunks.popleft()
                count = min(maximum, len(chunk))
                chunks.append(chunk[:count])
                lane.size -= count
                maximum -= count
                lane.dequeued_bytes += count
                if count < len(chunk):
                    lane.chunks.appendleft((timestamp, chunk[count:]))
            return b"".join(chunks)

    def state(self, channel):
        with self.lock:
            lane = self.lanes[channel]
            return lane.generation, lane.state, lane.format

    def snapshot(self):
        with self.lock:
            result = {}
            for channel, lane in self.lanes.items():
                self._expire(lane)
                result[LANES[channel]] = {
                    "state": lane.state, "generation": lane.generation,
                    "format": asdict(lane.format) if lane.format else None,
                    "queued_bytes": lane.size, "partial_bytes": len(lane.partial),
                    "queued_ms": 1000 * lane.size / lane.format.bytes_per_second if lane.format else 0,
                    "received_bytes": lane.received_bytes, "dequeued_bytes": lane.dequeued_bytes,
                    "dropped_bytes": lane.dropped_bytes, "packets": lane.packets,
                    "errors": lane.errors, "last_error": lane.last_error}
            return result

    def reset(self, close=False, clear_statistics=False):
        with self.lock:
            self.closed = close
            for lane in self.lanes.values():
                self._discard(lane)
                lane.generation += 1
                lane.state = "CLOSED" if close else "IDLE"
                lane.format, lane.last_error = None, None
                if clear_statistics:
                    lane.received_bytes = lane.dequeued_bytes = lane.dropped_bytes = 0
                    lane.packets = lane.errors = 0


# Keep queue tests and CLI imports usable without the optional desktop runtime.
try:
    from PySide6.QtCore import QObject, QTimer
    from PySide6.QtMultimedia import QAudioFormat, QAudioSink, QMediaDevices
except ImportError:
    QObject, QTimer = object, None


class DesktopAudioPlayer(QObject):
    """Main-thread player; only submit() is intended for background threads.

    Device IDs are stable hex strings returned by available_outputs(). A null
    selection follows the macOS default output, including device changes.
    """
    def __init__(self, parent=None):
        if QTimer is None:
            raise RuntimeError("PySide6 QtMultimedia is required for desktop audio")
        super().__init__(parent)
        self.buffer = PCMPlaybackBuffer()
        self.volume, self.muted = .7, False
        self.device_id = None
        self.outputs = {}
        self.output_errors = {}
        self.written_bytes = {channel: 0 for channel in LANES}
        self.closed = False
        self.devices = QMediaDevices(self)
        self.devices.audioOutputsChanged.connect(self._restart_outputs)
        self.timer = QTimer(self)
        self.timer.setInterval(10)
        self.timer.timeout.connect(self._pump)
        self.timer.start()

    def submit(self, packet):
        return self.buffer.submit(packet)

    @staticmethod
    def available_outputs():
        default = bytes(QMediaDevices.defaultAudioOutput().id()).hex()
        return [{"id": bytes(device.id()).hex(), "name": device.description(),
                 "default": bytes(device.id()).hex() == default}
                for device in QMediaDevices.audioOutputs()]

    def set_output_device(self, device_id=None):
        if isinstance(device_id, bytes):
            device_id = device_id.hex()
        self.device_id = device_id or None
        self._restart_outputs()

    def set_volume(self, value):
        value = float(value)
        if not math.isfinite(value):
            raise ValueError("volume must be finite")
        self.volume = max(0., min(1., value))
        for entry in self.outputs.values():
            entry["sink"].setVolume(0. if self.muted else self.volume)

    def set_muted(self, muted):
        self.muted = bool(muted)
        self.set_volume(self.volume)

    def _stop_output(self, channel):
        entry = self.outputs.pop(channel, None)
        if entry:
            # reset() discards buffered sound immediately; stop() may drain.
            entry["sink"].reset()
            entry["sink"].deleteLater()

    def _restart_outputs(self):
        for channel in tuple(self.outputs):
            self._stop_output(channel)
        self.output_errors.clear()

    def _device(self):
        if self.device_id is None:
            return QMediaDevices.defaultAudioOutput()
        for device in QMediaDevices.audioOutputs():
            if bytes(device.id()).hex() == self.device_id:
                return device
        raise RuntimeError("Selected audio output is disconnected")

    def _start_output(self, channel, generation, pcm):
        device = self._device()
        if device.isNull():
            raise RuntimeError("No macOS audio output is available")
        fmt = QAudioFormat()
        fmt.setSampleRate(pcm.sample_rate)
        fmt.setChannelCount(pcm.channels)
        fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16 if pcm.bits == 16 else QAudioFormat.SampleFormat.UInt8)
        if not device.isFormatSupported(fmt):
            raise RuntimeError(f"Output does not support {pcm.sample_rate} Hz / {pcm.channels} ch / {pcm.bits} bit PCM")
        sink = QAudioSink(device, fmt, self)
        sink.setBufferSize(max(pcm.frame_bytes, pcm.bytes_per_second // 10))
        sink.setVolume(0. if self.muted else self.volume)
        io = sink.start()
        if io is None or sink.error().name != "NoError":
            error = sink.error().name
            sink.reset()
            sink.deleteLater()
            raise RuntimeError(f"Audio output failed: {error}")
        self.outputs[channel] = {"generation": generation, "sink": sink, "io": io,
                                 "pending": b"", "pending_at": 0.}

    def _pump(self):
        if self.closed:
            return
        for channel in LANES:
            generation, state, pcm = self.buffer.state(channel)
            entry = self.outputs.get(channel)
            if entry and (entry["generation"] != generation or state not in ("READY", "PLAYING")):
                self._stop_output(channel)
                entry = None
            if state not in ("READY", "PLAYING") or pcm is None:
                continue
            previous_error = self.output_errors.get(channel)
            if previous_error and previous_error[0] == generation:
                continue
            try:
                if entry is None:
                    self._start_output(channel, generation, pcm)
                    entry = self.outputs[channel]
                    self.output_errors.pop(channel, None)
                error = entry["sink"].error().name
                if error not in ("NoError", "UnderrunError"):
                    raise RuntimeError(f"Audio output failed: {error}")
                if entry["pending"] and time.monotonic() - entry["pending_at"] > self.buffer.max_age:
                    # Never carry half a sample across a prolonged device stall.
                    self._stop_output(channel)
                    continue
                capacity = max(0, int(entry["sink"].bytesFree()))
                if not entry["pending"]:
                    quantum = max(pcm.frame_bytes, pcm.bytes_per_second // 50)
                    entry["pending"] = self.buffer.read(channel, generation, min(capacity, quantum))
                    entry["pending_at"] = time.monotonic()
                if entry["pending"] and capacity:
                    count = entry["io"].write(entry["pending"][:capacity])
                    if count < 0:
                        raise RuntimeError("Audio device write failed")
                    entry["pending"] = entry["pending"][count:]
                    self.written_bytes[channel] += count
            except Exception as error:
                self._stop_output(channel)
                self.output_errors[channel] = (generation, str(error))

    def snapshot(self):
        lanes = self.buffer.snapshot()
        for channel, name in LANES.items():
            entry = self.outputs.get(channel)
            error = self.output_errors.get(channel)
            lanes[name].update(written_bytes=self.written_bytes[channel],
                               output_state=entry["sink"].state().name if entry else "STOPPED",
                               output_error=error[1] if error else None)
        return {"lanes": lanes, "volume": self.volume, "muted": self.muted,
                "device_id": self.device_id, "microphone": "DISABLED",
                "backend": "Qt QAudioSink / CoreAudio", "closed": self.closed}

    def test_tone(self):
        """Explicit local 150 ms, quiet 440 Hz speaker test on the VR lane."""
        rate, frames = 48000, 7200
        pcm = b"".join(struct.pack("<h", int(2600 * math.sin(2 * math.pi * 440 * i / rate)
                        * min(1., i / 240, (frames - i - 1) / 240))) for i in range(frames))
        self.submit(Packet(VR, VR_INIT, protobuf(f1=rate, f2=1, f3=16)))
        self.submit(Packet(VR, VR_DATA, pcm))

    def reset(self):
        self.buffer.reset(clear_statistics=True)
        self._restart_outputs()
        self.written_bytes = {channel: 0 for channel in LANES}

    def close(self):
        self.closed = True
        self.timer.stop()
        self.buffer.reset(close=True)
        self._restart_outputs()
