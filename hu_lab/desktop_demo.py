"""Local synthetic MD for exercising the real HU wire, decoder and audio output.

No device or external network access. This is a diagnostic pattern, not CarLife
interoperability evidence. The server owns one loopback listener and one peer.
"""
from fractions import Fraction
import math
import socket
import struct
import threading
import time
from .protocol import (Packet, FrameParser, CMD, VIDEO, MEDIA, TOUCH, HU_VERSION,
    VERSION_MATCH, MD_INFO, VIDEO_INIT, VIDEO_INIT_DONE, VIDEO_START, VIDEO_PAUSE,
    VIDEO_RESET, VIDEO_DATA, TOUCH_ACTION, HARD_KEY, MEDIACODEC_AU_MARKER, fields, protobuf)


class DemoPeer:
    def __init__(self, *, tone=False, touch_profile='legacy-single'):
        self.listener = socket.socket()
        self.listener.bind(('127.0.0.1', 0))
        self.listener.listen(1)
        self.listener.settimeout(.2)
        self.port = self.listener.getsockname()[1]
        self.halt = threading.Event()
        self.sock = None
        self.error = None
        self.commands = []
        self.tone = tone
        self.touch_profile = touch_profile
        self.thread = threading.Thread(target=self._run, daemon=True, name='headunit-demo')
        self.thread.start()

    def close(self):
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
            if not self.halt.is_set() and not isinstance(error, (BrokenPipeError, ConnectionResetError)):
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
                if p.message == HU_VERSION:
                    version = fields(p.payload).get(1, 1)
                    self._send(Packet(CMD, VERSION_MATCH, protobuf(f1=1)),
                               Packet(CMD, MD_INFO, protobuf(f4='Local synthetic MD', f15=MEDIACODEC_AU_MARKER)))
                    if version == 2:
                        self._send(Packet(CMD, 0x10051))
                elif p.message == 0x18043:
                    from .honda_reference import repeated_entries, list_body
                    entries = repeated_entries(p.payload)
                    self._send(Packet(CMD, 0x10044, list_body([dict(f1=e[1], f2=0) for e in entries])))
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
