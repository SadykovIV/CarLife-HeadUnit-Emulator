"""Desktop session shared by the EasyConn, WeLink and HiCar receivers."""
import asyncio
from collections import deque
from contextlib import ExitStack
from pathlib import Path
import re
import socket
import threading
import time
from types import SimpleNamespace

import av

from .audio import MEDIA_INIT, MEDIA_DATA, TTS_INIT, TTS_DATA
from .protocol import MEDIA, TTS, Packet, protobuf
from .touch import DOWN, MOVE, UP, CANCEL, POINTER_DOWN, POINTER_UP, profile_name

FAMILIES = {"carlife": "CarLife", "carbit": "CarbitLink", "welink": "WeLink", "hicar": "Huawei HiCar"}
MODES = {"carlife": ("usb", "wifi", "tcp", "demo"), "carbit": ("wifi", "usb"),
         "welink": ("usb",), "hicar": ("ncm",)}
ACCESSORIES = {"carbit": ("Carbit", "CarbitDriverOversea", "CarbitLink", "1.0", "", "HSD"),
               "welink": ("Mapbar,Inc", "WeLink", "WeLink", "1.0", "", "HSD")}
NAL = re.compile(rb"\x00\x00(?:\x00)?\x01")


async def send(writer, data):
    writer.write(data)
    await asyncio.wait_for(writer.drain(), 5)


class Listener:
    """A one-peer listener. Close unsolicited sockets, including queued duplicates."""
    def __init__(self, address, peer=None):
        self.address, self.peer = address, peer
        self.arrival = asyncio.get_running_loop().create_future()

    async def open(self, stack, port=0):
        def accepted(reader, writer):
            ip = writer.get_extra_info("peername")[0]
            if self.arrival.done() or (self.peer is not None and self.peer != ip):
                writer.close()
                return
            stack.callback(writer.close)
            self.arrival.set_result((reader, writer))
        server = await asyncio.start_server(accepted, self.address, port, family=socket.AF_INET, backlog=2)
        stack.callback(server.close)
        self.port = server.sockets[0].getsockname()[1]
        return self


class ReceiverClient:
    def __init__(self, config, directory, audio_sink, on_frame_available, on_decoded_frame, usb_factory=None):
        self.config = config
        self.output_dir = Path(directory)
        self.session_id = self.output_dir.name
        self.backend = config.profile.upper() + "_" + config.transport.upper()
        self.session = SimpleNamespace(state="CONNECTING", width=config.width, height=config.height, fps=config.fps,
            requested=dict(width=config.width, height=config.height, fps=config.fps), accepted=None)
        dual = config.profile != "hicar" and profile_name(config.touch_profile) != "LEGACY_SINGLE"
        self.touch_profile = "LEGACY_DUAL" if dual else "LEGACY_SINGLE"
        self.usb_factory, self.sock = usb_factory, None
        self.audio_sink, self.on_frame_available, self.on_decoded_frame = audio_sink, on_frame_available, on_decoded_frame
        self.frame_lock = threading.Lock()
        self.metrics_lock = threading.Lock()
        self.frame_records, self.latest_frame = {}, None
        self.frame_times = deque(maxlen=180)
        self.decoder_generation = 0
        self.decoded_frames = self.input_packets = self.audio_bytes = 0
        self.pending_touch = {}
        self.error, self.detail = None, "Подготовка"
        self.halt, self.started = threading.Event(), threading.Event()
        self.stopped = False
        self.loop = self.task = self.thread = self.input_handler = None
        self.decoder = av.CodecContext.create("h264", "r")
        self.decoder.thread_count = 1
        self.parameter_sets = {}
        self.audio_formats = {}
        self.state_since = time.monotonic()
        self.peer = None
        self.hard_keys = {"home", "back"} if config.profile == "hicar" else set()
        if config.profile == "welink":
            self.hard_keys = {"home", "back", "previous", "next", "play", "stop"}

    def start(self):
        self.output_dir.mkdir(parents=True, exist_ok=True)
        if self.usb_factory:
            self.sock = self.usb_factory()
        self.thread = threading.Thread(target=self._worker, daemon=True, name=self.backend)
        self.thread.start()
        self.started.wait(5)

    @staticmethod
    def _causes(exc):
        if isinstance(exc, BaseExceptionGroup):
            return [cause for item in exc.exceptions for cause in ReceiverClient._causes(item)]
        return [exc]

    def _worker(self):
        try:
            asyncio.run(self._run())
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            if not self.halt.is_set():
                causes = self._causes(exc)
                failures = [item for item in causes if not isinstance(item, (EOFError, ConnectionResetError, BrokenPipeError))]
                if self.decoded_frames and not failures:
                    self.stage("CLOSED", "Телефон завершил проекцию")
                else:
                    cause = (failures or causes)[0]
                    self.error = f"{self.detail} — {str(cause) or type(cause).__name__}"
                    self.session.state = "ERROR"
        finally:
            self.halt.set()

    async def _run(self):
        from . import carbit_receiver, welink_receiver
        from .hicar import receiver as hicar_receiver
        self.loop = asyncio.get_running_loop()
        self.task = asyncio.current_task()
        self.inputs = asyncio.Queue(64)
        self.started.set()
        with ExitStack() as stack:
            self.resources = stack
            sender = asyncio.create_task(self._send_inputs())
            receiver = asyncio.create_task({"carbit": carbit_receiver.run, "welink": welink_receiver.run,
                                             "hicar": hicar_receiver.run}[self.config.profile](self))
            watchdog = asyncio.create_task(self._watch_video())
            tasks = (sender, receiver, watchdog)
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for item in done:
                    item.result()
            finally:
                for item in tasks:
                    item.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    async def _watch_video(self):
        while True:
            await asyncio.sleep(1)
            if self.session.state == "WAIT_VIDEO" and time.monotonic() - self.state_since > 45:
                raise TimeoutError("Телефон не передал первый декодируемый кадр")

    def stage(self, state, detail):
        self.session.state, self.detail = state, detail
        self.state_since = time.monotonic()

    async def _send_inputs(self):
        while True:
            events = await self.inputs.get()
            if self.input_handler:
                await self.input_handler(events)
                self.input_packets += 1

    def _enqueue(self, events):
        if self.session.state != "STREAMING" or self.halt.is_set():
            raise ValueError("Проекция пока не готова к вводу")
        def offer():
            if self.inputs.full():
                self.error = "Очередь ввода переполнена"
                self.session.state = "ERROR"
                self.task.cancel()
            else:
                self.inputs.put_nowait(events)
        self.loop.call_soon_threadsafe(offer)

    def video(self, data):
        if not data or len(data) > 8_500_000:
            raise ValueError("Недопустимый размер кадра H.264")
        parts = NAL.split(data)
        if len(parts) < 2 or parts[0]:
            raise ValueError("Требуется видео H.264 Annex B")
        units = [p for p in parts[1:] if p]
        kinds = {p[0] & 31 for p in units}
        for unit in units:
            typ = unit[0] & 31
            if typ in (7, 8):
                if len(unit) > 65536:
                    raise ValueError("Слишком большой набор параметров H.264")
                if typ == 7:
                    from .h264 import parse_sps
                    dims = parse_sps(unit[1:])
                    if max(dims["coded_width"], dims["coded_height"]) > 4096:
                        raise ValueError("Размер видео больше 4096 пикселей")
                self.parameter_sets[typ] = b"\0\0\0\1" + unit
        if not kinds.intersection((1, 5)) or not self.decoded_frames and 5 not in kinds:
            return
        if 5 in kinds:
            data = b"".join(self.parameter_sets[k] for k in (7, 8) if k not in kinds and k in self.parameter_sets) + data
        for frame in self.decoder.decode(av.Packet(data)):
            rgb = frame.reformat(format="rgb24")
            plane = rgb.planes[0]
            self.decoded_frames += 1
            index = self.decoded_frames
            self.session.width, self.session.height = rgb.width, rgb.height
            with self.metrics_lock:
                self.frame_records = {index: {"generation": self.decoder_generation}}
            with self.frame_lock:
                self.latest_frame = (bytes(plane), rgb.width, rgb.height, plane.line_size, index)
            self.frame_times.append(time.monotonic())
            if self.session.state != "STREAMING":
                self.stage("STREAMING", "Проекция активна")
            if self.on_frame_available:
                self.on_frame_available()
            if self.on_decoded_frame:
                self.on_decoded_frame({"generation": self.decoder_generation, "index": index}, self.latest_frame[0], rgb.width, rgb.height, plane.line_size)

    def pcm(self, data, rate, channels, lane=MEDIA):
        if self.audio_sink and data:
            if self.audio_formats.get(lane) != (rate, channels):
                self.audio_formats[lane] = rate, channels
                self.audio_sink.submit(Packet(lane, TTS_INIT if lane == TTS else MEDIA_INIT, protobuf(f1=rate, f2=channels, f3=16)))
            step = 32768
            for at in range(0, len(data), step):
                self.audio_sink.submit(Packet(lane, TTS_DATA if lane == TTS else MEDIA_DATA, data[at:at + step]))
        self.audio_bytes += len(data)

    def mark_presented(self, frame_index, host_paint_ns=None):
        self.presented = frame_index

    def input_content_contains(self, x, y):
        return 0 <= x < self.session.width and 0 <= y < self.session.height

    def touch(self, action, x, y):
        from .touch import Pointer, TouchFrame
        self.send_touch_frame(TouchFrame(action, (Pointer(0, x, y),), generation=self.decoder_generation))

    def send_touch_frame(self, frame):
        frame.validate(1 if self.touch_profile == "LEGACY_SINGLE" else 2)
        if frame.generation != self.decoder_generation:
            raise ValueError("Касание относится к предыдущему кадру")
        if frame.action == CANCEL:
            return self.cancel_touch()
        points = frame.pointers if frame.action == MOVE else (frame.pointers[frame.action_index],)
        action = {DOWN: "down", POINTER_DOWN: "down", UP: "up", POINTER_UP: "up", MOVE: "move"}[frame.action]
        events = []
        for point in points:
            x, y = int(point.x), int(point.y)
            if not self.input_content_contains(x, y):
                raise ValueError("Точка за пределами видео")
            events.append((action, point.id, x, y))
        self._enqueue(events)
        for _, slot, x, y in events:
            if action == "up":
                self.pending_touch.pop(slot, None)
            else:
                self.pending_touch[slot] = (x, y)

    def cancel_touch(self, reason="cancel"):
        if self.pending_touch:
            events = [("up", slot, *xy) for slot, xy in self.pending_touch.items()]
            self.pending_touch.clear()
            if not self.halt.is_set():
                self._enqueue(events)

    def hard_key(self, name):
        if name not in self.hard_keys:
            raise ValueError("Команда недоступна для этого протокола")
        self._enqueue([("key", name)])

    def stop(self):
        self.halt.set()
        if self.sock:
            self.sock.shutdown()
        if self.loop and not self.loop.is_closed():
            try:
                self.loop.call_soon_threadsafe(self.task.cancel)
            except RuntimeError:
                pass
        if self.thread:
            self.thread.join(8)
            if self.thread.is_alive():
                raise RuntimeError("Приёмник ещё завершает соединение")
        if self.sock:
            self.sock.close()
        self.stopped = True
        self.session.state = "ERROR" if self.error else "CLOSED"

    def summary(self):
        return dict(session_id=self.session_id, profile=self.config.profile, backend=self.backend,
                    state=self.session.state, stage=self.detail, error=self.error, peer=self.peer,
                    transport=getattr(self.sock, "evidence", None) or self.config.transport,
                    width=self.session.width, height=self.session.height, decoded_frames=self.decoded_frames,
                    input_packets=self.input_packets, audio_bytes=self.audio_bytes)
