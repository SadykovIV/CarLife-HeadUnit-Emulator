import base64
from collections import deque
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import socket
import tempfile
import time
import unittest
from unittest.mock import patch

import av
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from hu_lab import modern as m
from hu_lab.desktop_session import DesktopConfig, create_client
from hu_lab.honda_reference import MD_FEATURE_REQUEST, list_body, repeated_entries
from hu_lab.protocol import (CMD, VIDEO, HARD_KEY, HU_VERSION, VERSION_MATCH, MD_INFO, VIDEO_INIT, VIDEO_INIT_DONE,
                             VIDEO_DATA, VIDEO_START, MEDIACODEC_AU_MARKER, FrameParser, Packet, fields, protobuf)


def command(kind, body=b''):
    return Packet(CMD, kind, body)


INFO = protobuf(f4='qti', f14='TBox Ultra 1', f20='35', f15=MEDIACODEC_AU_MARKER)


def phone_proof(challenge):
    nonce = fields(challenge.payload)[1].split(b';')[1]
    digest = hashlib.md5(b'CARLIFEqtiTBox Ultra 135' + nonce, usedforsecurity=False)
    return command(m.MD_PROOF, protobuf(f1=digest.hexdigest()))


class NegotiationTest(unittest.TestCase):
    def session(self, version=(2, 1), **options):
        return m.ModernSession(320, 240, 15, version=version, channel='own-channel', cuid='own-cuid',
                               multitouch=True, theme=1, **options)

    def begin(self, s):
        s.start()
        return s.receive(command(VERSION_MATCH, protobuf(f1=1, f2=4)))

    def test_independent_md_vectors(self):
        self.assertEqual(m.verification_code(('qti', 'TBox Ultra 1', '35'), '2026-10-14-12-01-33'),
                         b'e330041700aabf8c288c329b01bd6beb')
        self.assertEqual(m.verification_code(('品牌', '盒子😀', '35'), 'n\0x'), b'2c14a1c1c1f182d7dcd61333667bdb22')

    def test_each_version_announces_exact_numbers_and_waits_for_both_verdicts(self):
        for version in m.VERSIONS.values():
            for phone_first in (True, False):
                with self.subTest(version=version, phone_first=phone_first):
                    s = self.session(version)
                    self.assertEqual(fields(s.start()[0].payload), {1:version[0], 2:version[1]})
                    stats, = s.receive(command(VERSION_MATCH, protobuf(f1=1, f2=4)))
                    self.assertEqual(fields(stats.payload)[4], b'own-channel')
                    challenge = s.receive(command(MD_INFO, INFO))[-1]
                    self.assertEqual(s.receive(command(MD_INFO, INFO)), [])
                    messages = [phone_proof(challenge), command(m.MD_AUTHEN_RESULT, protobuf(f1=1))]
                    if not phone_first:
                        messages.reverse()
                    s.receive(messages[0])
                    self.assertEqual(s.state, 'WAIT_AUTH')
                    replies = s.receive(messages[1])
                    self.assertEqual([p.message for p in replies][-1], VIDEO_INIT)
                    self.assertEqual(s.state, 'WAIT_INIT')
                    self.assertNotIn(VIDEO_INIT, [p.message for p in s.receive(messages[1])])

    def test_registration_uses_supplied_values_and_waits_for_activation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'registration.json'
            path.write_text(json.dumps(dict(uuid='assigned', vehicle_id='vehicle', certificate='issued text')))
            reg = m.load_oem_registration(path, 'own-channel')
            with self.assertRaises(ValueError):
                m.load_oem_registration(path, '')
        s = self.session(registration=reg)
        cert, active, _ = self.begin(s)
        self.assertEqual((cert.message, fields(cert.payload)[1]), (m.HU_CERTIFICATE, b'issued text'))
        self.assertEqual((active.message, fields(active.payload)[1]), (m.HU_ACTIVE_INFO, b'assigned'))
        self.assertEqual(fields(active.payload)[4], 0)
        s.receive(phone_proof(s.receive(command(MD_INFO, INFO))[-1]))
        s.receive(command(m.MD_AUTHEN_RESULT, protobuf(f1=1)))
        self.assertEqual(s.state, 'WAIT_AUTH')
        self.assertEqual(s.receive(command(m.MD_ACTIVE_DONE, protobuf(f1=1)))[-1].message, VIDEO_INIT)
        self.assertNotIn('issued text', json.dumps(s.status()))
        with self.assertRaises(m.AuthError):
            s.receive(command(m.MD_ACTIVE_DONE, protobuf(f1=0)))

    def test_no_replay_implicit_accept_or_version_downgrade(self):
        a, b = self.session(), self.session()
        for s in (a, b):
            self.begin(s)
        previous = phone_proof(a.receive(command(MD_INFO, INFO))[-1])
        b.receive(command(MD_INFO, INFO))
        with self.assertRaises(m.AuthError) as err:
            b.receive(previous)
        self.assertEqual(fields(err.exception.reply.payload), {1:0})
        self.assertEqual(b.state, 'BLOCKED_AUTH')
        with self.assertRaises(m.AuthError):
            b.receive(command(m.MD_AUTHEN_RESULT, protobuf(f1=1)))
        with self.assertRaises(m.AuthError):
            a.receive(command(MD_INFO, protobuf(f4='different')))
        c = self.session()
        c.start()
        with self.assertRaisesRegex(ValueError, 'отклонил версию'):
            c.receive(command(VERSION_MATCH, protobuf(f1=2)))
        self.assertEqual(c.state, 'VERSION_REJECTED')

    def test_absolute_timeout_and_premature_media(self):
        for premature in (command(VIDEO_INIT_DONE, protobuf(f1=320, f2=240, f3=15)), Packet(VIDEO, VIDEO_DATA, b'x')):
            s = self.session()
            self.begin(s)
            with self.assertRaises(m.AuthError):
                s.receive(premature)
        s = self.session()
        self.begin(s)
        with patch('hu_lab.modern.time.monotonic', return_value=s.deadline + 1):
            with self.assertRaisesRegex(m.AuthError, 'MD_INFO'):
                s.check_deadline()

    def test_key_order_and_optional_feature(self):
        s = self.session()
        self.begin(s)
        features = s.receive(command(MD_FEATURE_REQUEST))[0]
        self.assertEqual({e[1]:e[2] for e in repeated_entries(features.payload, bytes)},
                         {b'CONTENT_ENCRYPTION':0, b'MULTI_TOUCH':1})
        with self.assertRaises(m.AuthError):
            s.receive(command(m.MD_CIPHER_READY))
        s = self.session()
        self.begin(s)
        s.receive(command(m.MD_KEY_REQUEST))
        with self.assertRaises(m.AuthError):
            s.receive(command(m.MD_WRAPPED_KEY, protobuf(f1='invalid')))

    def test_required_encryption_cannot_be_downgraded_to_plain_video(self):
        s = self.session(require_encryption=True)
        self.begin(s)
        s.receive(command(MD_FEATURE_REQUEST, list_body([dict(f1='CONTENT_ENCRYPTION', f2=0)])))
        s.receive(phone_proof(s.receive(command(MD_INFO, INFO))[-1]))
        replies = s.receive(command(m.MD_AUTHEN_RESULT, protobuf(f1=1)))
        self.assertNotIn(VIDEO_INIT, [p.message for p in replies])
        self.assertEqual(s.state, 'WAIT_AUTH')
        with patch('hu_lab.modern.time.monotonic', return_value=s.deadline + 1):
            with self.assertRaisesRegex(m.AuthError, 'обмена ключами'):
                s.check_deadline()


class SocketPeer:
    """Independent MD side: raw socket packets and the JCE AES padding convention."""
    def __init__(self, sock):
        self.sock = sock
        self.parser = FrameParser()
        self.pending = deque()
        self.cipher = None

    def take(self, kind):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            if not self.pending:
                data = self.sock.recv(65536)
                if not data:
                    raise EOFError(kind)
                self.pending.extend(self.parser.feed(data))
                continue
            p = self.pending.popleft()
            if p.message == kind:
                if self.cipher and p.payload:
                    ctx = self.cipher.decryptor()
                    data = ctx.update(p.payload) + ctx.finalize()
                    n = data[-1]
                    assert 1 <= n <= 16 and data[-n:] == bytes([n]) * n
                    return Packet(p.channel, p.message, data[:-n], p.timestamp)
                return p
        raise TimeoutError(kind)

    def send(self, *packets):
        for p in packets:
            if self.cipher and p.payload:
                n = 16 - len(p.payload) % 16
                ctx = self.cipher.encryptor()
                data = ctx.update(p.payload + bytes([n]) * n) + ctx.finalize()
                p = Packet(p.channel, p.message, data, p.timestamp)
            self.sock.sendall(p.encode())


class WireTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        a, b = socket.socketpair()
        self.addCleanup(b.close)
        b.settimeout(4)
        self.peer = SocketPeer(b)
        self.client = create_client(DesktopConfig(profile='carlife-4.1', width=320, height=240, fps=15), tmp.name)
        self.client.transport_factory = lambda: a
        self.addCleanup(self.client.stop)
        self.client.start()
        self.peer.take(HU_VERSION)
        self.peer.send(command(VERSION_MATCH, protobuf(f1=1)), command(MD_INFO, INFO))
        self.challenge = self.peer.take(m.HU_CHALLENGE)

    def test_phone_rejection_acknowledged_on_wire_before_close(self):
        self.peer.send(command(m.MD_AUTHEN_RESULT, protobuf(f1=0)))
        self.peer.take(m.HU_ACK)
        self.assertTrue(self.client.halt.wait(2))
        self.assertEqual(self.client.session.state, 'BLOCKED_AUTH')
        self.assertFalse(self.client.summary()['carlife_negotiation']['hu_accepted_by_phone'])

    def test_bad_response_sends_negative_verdict(self):
        self.peer.send(command(m.MD_PROOF, protobuf(f1='bad')))
        self.assertEqual(fields(self.peer.take(m.HU_VERDICT).payload), {1:0})
        self.assertTrue(self.client.halt.wait(2))
        self.assertIsNone(self.client.session.accepted)

    def test_phone_rejection_survives_ack_write_failure(self):
        with patch.object(self.client, '_write_packet', side_effect=BrokenPipeError('peer closed')):
            self.peer.send(command(m.MD_AUTHEN_RESULT, protobuf(f1=0)))
            self.assertTrue(self.client.halt.wait(2))
        self.assertIn('Телефон отклонил', self.client.error)
        self.assertEqual(self.client.session.state, 'BLOCKED_AUTH')
        self.assertFalse(self.client.summary()['carlife_negotiation']['hu_accepted_by_phone'])

    def test_encrypted_projection_input_and_theme(self):
        peer = self.peer
        request = list_body([dict(f1='CONTENT_ENCRYPTION', f2=1)])
        peer.send(command(MD_FEATURE_REQUEST, request), phone_proof(self.challenge),
                  command(m.MD_AUTHEN_RESULT, protobuf(f1=1)), command(m.MD_KEY_REQUEST))
        key = serialization.load_der_public_key(base64.b64decode(fields(peer.take(m.HU_PUBLIC_KEY).payload)[1]))
        self.assertEqual(key.key_size, 2048)
        self.assertEqual(self.client.session.state, 'WAIT_AUTH')
        aes = b'0123456789abcdef'
        peer.send(command(m.MD_WRAPPED_KEY, protobuf(f1=base64.b64encode(key.encrypt(aes, padding.PKCS1v15())))))
        peer.take(m.HU_KEY_RECEIVED)
        peer.send(command(m.MD_CIPHER_READY))
        peer.take(m.HU_CIPHER_READY)
        peer.cipher = Cipher(algorithms.AES(aes), modes.ECB())
        mode = peer.take(VIDEO_INIT).payload
        self.assertEqual(fields(mode), {1:320, 2:240, 3:15})
        peer.send(command(VIDEO_INIT_DONE, mode))
        peer.take(VIDEO_START)
        encoder = av.CodecContext.create('libx264', 'w')
        encoder.width, encoder.height, encoder.pix_fmt = 320, 240, 'yuv420p'
        encoder.time_base, encoder.framerate = Fraction(1,15), Fraction(15,1)
        encoder.options = {'preset':'ultrafast', 'tune':'zerolatency'}
        for pts in range(4):
            frame = av.VideoFrame(320, 240, 'yuv420p')
            frame.pts = pts
            for out in encoder.encode(frame):
                peer.send(Packet(VIDEO, VIDEO_DATA, bytes(out), pts * 67))
        deadline = time.monotonic() + 3
        while self.client.decoded_frames < 2 and not self.client.error and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertGreaterEqual(self.client.decoded_frames, 2, self.client.error)
        self.client.hard_key('back')
        self.assertEqual(fields(peer.take(HARD_KEY).payload), {1:14})
        self.client.set_night_mode(True)
        self.assertEqual(fields(peer.take(m.SET_THEME).payload), {1:1})
        peer.take(m.GET_THEME)
        peer.send(command(m.MD_THEME, protobuf(f1=1)))
        # A malformed encrypted payload must not be reinterpreted as plaintext.
        peer.sock.sendall(command(m.MD_THEME, b'plaintext').encode())
        self.assertTrue(self.client.halt.wait(2))
        report = self.client.summary()['carlife_negotiation']
        self.assertEqual((report['content_encryption'], report['phone_theme']), ('active', 1))
        self.assertIn('зашифрованный', self.client.error)
