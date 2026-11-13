"""Local synthetic MD for exercising the real HU wire, decoder and audio output.

No device or external network access. This is a diagnostic pattern, not CarLife
interoperability evidence. The server owns one loopback listener and one peer.
"""
from fractions import Fraction
import errno
import math
import socket
import struct
import threading
import time
from .protocol import (Packet, FrameParser, CMD, VIDEO, MEDIA, TOUCH, HU_VERSION,
    VERSION_MATCH, MD_INFO, VIDEO_INIT, VIDEO_INIT_DONE, VIDEO_START, VIDEO_PAUSE,
    VIDEO_RESET, VIDEO_DATA, TOUCH_ACTION, HARD_KEY, MEDIACODEC_AU_MARKER, fields, protobuf)
from .cluster import (MEDIA_INFO, MEDIA_PROGRESS_BAR, MODULE_STATUS, NAVI_NEXT_TURN_INFO,
                      SUBSCRIBE, SUBSCRIBE_DONE, SUBSCRIBE_START, SUBSCRIBE_STOP)
from .honda_reference import list_body, repeated_entries


class DemoCluster:
    """Synthetic MD. Emit route/track packets only for modules the HU started."""
    route = ((3, 'Набережная', 350), (7, 'Садовая улица', 120), (1, 'Проспект Мира', 1500))
    tracks = (('Ночной рейс', 'Городские огни'), ('За поворотом', 'Северный ветер'))

    def __init__(self):
        self.subscribed = set()
        self.started = None
        self.last_tick = self.last_song = None
        self.artwork = {}

    def cover(self, song):
        """Original diagnostic artwork, sent as a real bounded JPEG field."""
        if song not in self.artwork:
            from io import BytesIO
            from PIL import Image, ImageDraw
            image = Image.new('RGB', (256, 256), '#122737')
            draw = ImageDraw.Draw(image)
            accent = ('#67c5c0', '#f4bd78')[song]
            draw.ellipse((150, 24, 214, 88), fill=accent)
            draw.polygon(((0, 165), (75, 87), (139, 159), (202, 110), (256, 170), (256, 256), (0, 256)),
                         fill='#254c5e')
            draw.polygon(((111, 256), (143, 161), (153, 161), (210, 256)), fill=accent)
            draw.line(((150, 184), (155, 203)), fill='#122737', width=3)
            draw.line(((162, 224), (169, 250)), fill='#122737', width=4)
            output = BytesIO()
            image.save(output, 'JPEG', quality=85)
            self.artwork[song] = output.getvalue()
        return self.artwork[song]

    def command(self, packet):
        if packet.message not in (SUBSCRIBE, SUBSCRIBE_START, SUBSCRIBE_STOP):
            return None
        ids = [entry[1] for entry in repeated_entries(packet.payload)]
        available = {0, 2}.intersection(ids)
        if packet.message == SUBSCRIBE:
            support = list_body([dict(f1=n, f2=int(n in available)) for n in ids])
            modules = list_body([dict(f1=2, f2=1), dict(f1=3, f2=1)])
            return (Packet(CMD, SUBSCRIBE_DONE, support), Packet(CMD, MODULE_STATUS, modules))
        if packet.message == SUBSCRIBE_START:
            self.subscribed.update(available)
            self.last_tick = self.last_song = None
        else:
            self.subscribed.difference_update(available)
        return ()

    def updates(self, now):
        if not self.subscribed:
            return ()
        if self.started is None:
            self.started = now
        tick = int(now - self.started)
        if tick == self.last_tick:
            return ()
        self.last_tick = tick
        packets = []
        if 0 in self.subscribed:
            step = tick % 16
            if step == 15:
                turn = protobuf(f1=3, f2=24, f3='', f4=0, f5=0, f6=0)
            else:
                code, road, distance = self.route[step // 5]
                turn = protobuf(f1=1 if step % 5 == 0 else 2, f2=code, f3=road,
                                f4=distance, f5=distance * (4 - step % 5) // 4, f6=0)
            packets.append(Packet(CMD, NAVI_NEXT_TURN_INFO, turn))
        if 2 in self.subscribed:
            song = (tick // 12) % len(self.tracks)
            if song != self.last_song:
                title, artist = self.tracks[song]
                info = protobuf(f1='CarLife Demo', f2=title, f3=artist, f4='Демо-маршрут',
                                f5=self.cover(song), f6=12, f7=2, f8=str(song), f9=0)
                packets.append(Packet(CMD, MEDIA_INFO, info))
                self.last_song = song
            packets.append(Packet(CMD, MEDIA_PROGRESS_BAR, protobuf(f1=tick % 12 * 1000)))
        return packets


class DemoPeer:
    def __init__(self, *, tone=False, touch_profile='legacy-single'):
        self.listener = socket.socket()
        self.listener.bind(('127.0.0.1', 0))
        self.listener.listen(1)
        self.listener.settimeout(.2)
        self.port = self.listener.getsockname()[1]
        self.halt = threading.Event()
        self.closing = threading.Event()
        self.sock = None
        self.error = None
        self.commands = []
        self.tone = tone
        self.touch_profile = touch_profile
        self.thread = threading.Thread(target=self._run, daemon=True, name='headunit-demo')
        self.thread.start()

    def begin_close(self):
        """Mark intentional client shutdown without closing the peer socket.

        The client sends its final controls before closing TCP. On Windows,
        that close can abort a simultaneous peer read/write with WSAECONNABORTED.
        Keep serving until the client closes, but distinguish that expected
        abort from a connection failure while the demo is running.
        """
        self.closing.set()

    def close(self):
        self.begin_close()
        self.halt.set()
        self.listener.close()
        if self.sock:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.sock.close()
        self.thread.join(timeout=3)

    def _run(self):
        try:
            while not self.halt.is_set():
                try:
                    self.sock, _ = self.listener.accept()
                    break
                except socket.timeout:
                    continue
            if self.halt.is_set():
                return
            self.sock.settimeout(.004)
            self._session()
        except (OSError, EOFError) as error:
            expected_abort = self.closing.is_set() and self.sock is not None and (
                isinstance(error, ConnectionAbortedError) or
                getattr(error, 'errno', None) == errno.ECONNABORTED or
                getattr(error, 'winerror', None) == 10053)
            if (not self.halt.is_set() and not expected_abort and
                    not isinstance(error, (BrokenPipeError, ConnectionResetError))):
                self.error = str(error)
        except Exception as error:
            self.error = f'{type(error).__name__}: {error}'

    def _send(self, *packets):
        self.sock.sendall(b''.join(p.encode() for p in packets))

    def _session(self):
        import av
        from PIL import Image, ImageDraw, ImageFont
        from .touch import TouchDecoder, ACTION3
        touch_decoder = TouchDecoder(self.touch_profile)
        parser = FrameParser()
        encoder = None
        width, height, fps = 1280, 720, 30
        frame_index = 0
        streaming = False
        last_touch = None
        last_action = 'Touch the display to test input'
        next_video = next_audio = time.monotonic()
        pcm_index = 0
        cluster = DemoCluster()
        version = 1
        font = ImageFont.load_default(size=28)
        small_font = ImageFont.load_default(size=18)
        def configure(reset_pts=True):
            nonlocal encoder, frame_index
            encoder = av.CodecContext.create('libx264', 'w')
            encoder.width, encoder.height = width, height
            encoder.pix_fmt = 'yuv420p'
            encoder.time_base = Fraction(1, fps)
            encoder.framerate = Fraction(fps)
            encoder.options = {'preset':'ultrafast', 'tune':'zerolatency', 'profile':'baseline',
                               'x264-params':f'keyint={fps}:scenecut=0:repeat-headers=1'}
            encoder.open()
            if reset_pts:
                frame_index = 0
        while not self.halt.is_set():
            try:
                data = self.sock.recv(65536)
                if not data:
                    return
            except socket.timeout:
                data = b''
            for p in parser.feed(data):
                self.commands.append((p.message, p.payload))
                if len(self.commands) > 512:
                    del self.commands[:256]
                replies = cluster.command(p)
                if replies is not None:
                    if replies:
                        self._send(*replies)
                    continue
                if p.message == HU_VERSION:
                    version = fields(p.payload).get(1, 1)
                    self._send(Packet(CMD, VERSION_MATCH, protobuf(f1=1)),
                               Packet(CMD, MD_INFO, protobuf(f4='Local synthetic MD', f15=MEDIACODEC_AU_MARKER)))
                    if version == 2:
                        self._send(Packet(CMD, 0x10051))
                elif p.message == VIDEO_INIT:
                    mode = fields(p.payload)
                    width, height, fps = mode[1], mode[2], mode[3] or 30
                    configure()
                    self._send(Packet(CMD, VIDEO_INIT_DONE, protobuf(f1=width, f2=height, f3=fps)))
                elif p.message == VIDEO_START:
                    if encoder is not None:
                        configure()
                        streaming = True
                        next_video = next_audio = time.monotonic()
                        self._send(Packet(MEDIA, 0x30001, protobuf(f1=48000, f2=2, f3=16)))
                elif p.message == VIDEO_PAUSE:
                    streaming = False
                    self._send(Packet(MEDIA, 0x30003))
                elif p.message == VIDEO_RESET and version == 2:
                    configure(reset_pts=False)
                elif p.channel == TOUCH:
                    if p.message in (TOUCH_ACTION, ACTION3):
                        touch = touch_decoder.decode(p)
                        point = touch.pointers[0]
                        last_touch = (point.x, point.y)
                        last_action = f'Touch action {touch.action} at {last_touch}'
                    elif p.message == HARD_KEY:
                        f = fields(p.payload)
                        last_action = f'CarLife key received: {f.get(1)}'
            now = time.monotonic()
            updates = cluster.updates(now)
            if updates:
                self._send(*updates)
            if streaming and now >= next_video:
                image = Image.new('RGB', (width, height), '#101b29')
                draw = ImageDraw.Draw(image)
                unit = min(width / 1280, height / 720)
                pad = max(16, int(56 * unit))
                draw.rounded_rectangle((pad, pad, width-pad, height-pad), radius=24, fill='#152a3a', outline='#315265', width=2)
                draw.text((pad*1.5, pad*1.6), 'LOCAL TEST SIGNAL', font=font, fill='#67e8c5')
                draw.text((pad*1.5, pad*1.6+48), f'{width} x {height} / {fps} fps / H.264 + PCM', font=small_font, fill='#d7e6ef')
                draw.text((pad*1.5, height-pad*2-25), last_action, font=small_font, fill='#d7e6ef')
                colors = ['#6de6c2','#62b7eb','#9e8bdf','#f4ce78','#eb927c']
                for i, color in enumerate(colors):
                    x = int(pad*1.5 + i*(width-pad*3)/5)
                    draw.rounded_rectangle((x, int(height*.42), x+int((width-pad*4)/6), int(height*.65)), radius=10, fill=color)
                x = int(pad*2 + (width-pad*4)*(math.sin(frame_index/max(1,fps))*.5+.5))
                draw.ellipse((x-12,height*.74-12,x+12,height*.74+12), fill='white')
                if last_touch:
                    x,y = last_touch
                    draw.ellipse((x-20,y-20,x+20,y+20), outline='#ffffff', width=4)
                frame = av.VideoFrame.from_image(image)
                frame.pts = frame_index
                for packet in encoder.encode(frame):
                    self._send(Packet(VIDEO, VIDEO_DATA, bytes(packet), round(frame_index * 1000 / fps)))
                frame_index += 1
                next_video = now + 1 / fps
            if streaming and now >= next_audio:
                if self.tone:
                    # Low-level 440 Hz test tone, only after explicit user opt-in.
                    pcm = b''.join(struct.pack('<hh', v, v) for v in
                        (int(1500*math.sin(2*math.pi*440*(pcm_index+i)/48000)) for i in range(960)))
                else:
                    pcm = b'\0' * 3840
                self._send(Packet(MEDIA, 0x30006, pcm, pcm_index // 48))
                pcm_index += 960
                next_audio = now + .02
