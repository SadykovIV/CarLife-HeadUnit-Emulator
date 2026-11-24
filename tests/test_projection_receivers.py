import asyncio
from fractions import Fraction
import json
from pathlib import Path
import socket
import struct
import tempfile
import time
import unittest

import av

from hu_lab import receiver_discovery as dns
from hu_lab.desktop_session import DesktopConfig, create_client
from hu_lab.hicar import nearby_wire, receiver as hicar
from hu_lab.hicar.cipher import Cipher
from hu_lab.protocol import TTS
from hu_lab.receiver import ACCESSORIES, ReceiverClient
from hu_lab.touch import DOWN, MOVE, POINTER_DOWN, Pointer, TouchFrame
from hu_lab.usb import USBIdentity


def keyframe(width=320, height=240):
    codec = av.CodecContext.create("libx264", "w")
    codec.width, codec.height, codec.pix_fmt = width, height, "yuv420p"
    codec.time_base = Fraction(1, 30)
    codec.options = {"preset": "ultrafast", "tune": "zerolatency"}
    frame = av.VideoFrame(width, height, "yuv420p")
    for plane in frame.planes:
        plane.update(bytes([127]) * plane.buffer_size)
    return b"".join(bytes(packet) for packet in codec.encode(frame))


def until(condition, seconds=5):
    end = time.monotonic()+seconds
    while not condition():
        if time.monotonic() > end:
            raise AssertionError("Timed out waiting for the receiver")
        time.sleep(.01)


class TestDiscovery(unittest.TestCase):
    def test_hicar_legacy_question_retains_literal_dotted_instance(self):
        labels = (b"com.huawei.nearby.usb.nsd", *dns.NEARBY)
        question = dns.dns_name(labels) + struct.pack(">HH", 33, 1)
        query = struct.pack(">6H", 912, 0, 1, 0, 0, 0) + question
        data, unicast = dns.answer(query, dns.NEARBY, "192.168.4.1", 19999, True)
        self.assertTrue(unicast)
        self.assertEqual(struct.unpack_from(">6H", data), (912, 0x8400, 1, 4, 0, 0))
        self.assertIn(b"ncm.ip=192.168.4.1", data)
        self.assertIn(struct.pack(">HHH", 0, 0, 19999), data)
        self.assertIsNone(dns.answer(query, dns.CARBIT, "192.168.4.1", 10930, True))

    def test_compressed_name_cycle_rejected(self):
        with self.assertRaises(ValueError):
            dns.read_name(b"\xc0\x00", 0)

    def test_profiles_require_their_transports(self):
        for name, kind in [("welink","wifi"), ("hicar","usb"), ("carbit","tcp")]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                DesktopConfig(profile=name, transport=kind).validate()
        for name in ACCESSORIES:
            config = DesktopConfig(profile=name, transport="usb", aoa_start=True,
                                   usb_identity=USBIdentity(bus=1,ports=(2,),vid=0x04e8,pid=0x6860,address=3))
            c = create_client(config, Path("unused"))
            self.assertEqual(c.usb_factory.strings, ACCESSORIES[name])
            self.assertEqual(c.backend, name.upper()+"_USB")


class TestHiCarWire(unittest.TestCase):
    def test_new_iv_per_record_and_key_length(self):
        c = Cipher(bytes(32))
        a, b = c.encrypt(b"hello"), c.encrypt(b"hello")
        self.assertNotEqual(a, b)
        self.assertEqual(c.decrypt(a), b"hello")
        c.close()
        with self.assertRaises(ValueError):
            c.decrypt(a)
        with self.assertRaises(ValueError):
            Cipher(bytes(8))

    def test_only_cable_socket_requests_are_accepted(self):
        u = nearby_wire.pack_uint
        request = u(6) + u(19) + u(3) + b"USB" + u(1)
        self.assertEqual(hicar.business_field(request), 19)
        for invalid in (u(3) + request[1:], request+b"x", u(6)+u(19)+u(129)+u(1)):
            with self.assertRaises(ValueError):
                hicar.business_field(invalid)


class Accessory:
    """Emulate Android bulk reads, including split headers and coalesced records."""
    def __init__(self):
        self.host, self.phone = socket.socketpair()
        self.host.settimeout(.2)
        self.phone.settimeout(4)
        self.stream = self.phone.makefile("rb")
        self.evidence = {"entry": "ALREADY_ACCESSORY"}

    def recv(self, n):
        return self.host.recv(n)

    def write_accessory_record(self, head, body):
        self.host.sendall(head)
        self.host.sendall(body)

    def shutdown(self):
        self.host.shutdown(socket.SHUT_RDWR)

    def close(self):
        self.stream.close()
        self.phone.close()
        self.host.close()

    def record(self, kind, data):
        return struct.pack(">2sBBI", b"WL", kind, 0, len(data)) + data

    def get(self):
        magic, kind, ext, n = struct.unpack(">2sBBI", self.stream.read(8))
        assert magic == b"WL" and ext == 0
        return kind, self.stream.read(n)


class TestWeLink(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.usb = Accessory()
        self.audio = []
        self.config = DesktopConfig(profile="welink", transport="usb", width=320, height=240, touch_profile="legacy-dual")
        self.client = ReceiverClient(self.config, Path(root.name), self, None, None, lambda: self.usb)
        self.client.start()
        self.addCleanup(self.client.stop)
        methods = [json.loads(self.usb.get()[1])["command"] for _ in range(3)]
        self.assertEqual([m["method"] for m in methods], ["onHuReady", "onHuReady", "requestWeLink"])
        self.assertEqual(methods[0]["extData"], {"width":320,"height":240})
        self.assertEqual(methods[1]["extData"], {"codecMode":2})

    def submit(self, packet):
        self.audio.append(packet)

    def test_welink_h264_pcm_and_two_touches(self):
        answer = dict(moduleName="WeLink_SDK", command=dict(method="responseWeLink",
                extData=dict(state=1, errCode=0, responseCode=0, codecMode=2)))
        reply = self.usb.record(9, json.dumps(answer).encode())
        self.usb.phone.sendall(reply[:5])
        self.usb.phone.sendall(reply[5:] + self.usb.record(6, keyframe()))
        self.assertEqual(self.usb.get(), (8, b"config 30"))
        self.assertEqual(json.loads(self.usb.get()[1])["command"]["method"], "onHuSourceChange")
        until(lambda:self.client.decoded_frames == 1)
        samples = b"\x00\x10"*100
        body = struct.pack("<BBBBI", 0, 1, 1, 16, 48000) + samples
        self.usb.phone.sendall(self.usb.record(17, struct.pack("<HHBBHII", 0x55aa,10,0,0,0,0,len(body))+body))
        ack = self.usb.get()
        self.assertEqual(ack[0], 18)
        self.assertEqual(struct.unpack_from("<H", ack[1], 2)[0], 40010)
        self.assertEqual((self.audio[-1].channel,self.audio[-1].payload), (TTS,samples))
        a, b = Pointer(0,40,60), Pointer(1,150,70)
        self.client.send_touch_frame(TouchFrame(DOWN,(a,)))
        self.client.send_touch_frame(TouchFrame(POINTER_DOWN,(a,b),1))
        self.client.send_touch_frame(TouchFrame(MOVE,(a,b)))
        frames = [self.usb.get()[1] for _ in range(7)]
        self.assertEqual(frames, [b"motionevent3 40 60 0 down",b"motionevent3 comm",b"motionevent3 150 70 1 down",
                         b"motionevent3 comm",b"motionevent3 40 60 0 move",b"motionevent3 150 70 1 move",b"motionevent3 comm"])

    def test_tls_is_not_acknowledged_as_streaming(self):
        self.usb.phone.sendall(self.usb.record(107,b""))
        until(self.client.halt.is_set)
        self.assertIn("TLS", self.client.error)
        self.assertEqual(self.client.decoded_frames, 0)


class TestEasyConn(unittest.TestCase):
    def test_closed_socket_unblocks_wifi_listener(self):
        from hu_lab.receiver import Listener
        import contextlib
        async def check():
            with contextlib.ExitStack() as stack:
                listener = await Listener("127.0.0.1", "127.0.0.1").open(stack)
                rx, tx = await asyncio.open_connection("127.0.0.1", listener.port)
                first = await asyncio.wait_for(listener.arrival, 1)
                second_rx, second_tx = await asyncio.open_connection("127.0.0.1", listener.port)
                self.assertEqual(await asyncio.wait_for(second_rx.read(1),1), b"")
                second_tx.close()
                tx.close()
                self.assertEqual(await first[0].read(1), b"")
        asyncio.run(check())
