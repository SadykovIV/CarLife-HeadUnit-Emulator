"""Native TCP6 framing, cleanup and discovery over ephemeral loopback sockets."""
import socket
import struct
import threading
import time
import unittest
from unittest.mock import patch

from hu_lab.protocol import CMD, VIDEO, MEDIA, TOUCH, Packet, FrameParser, ProtocolError, VIDEO_HEARTBEAT
from hu_lab.wireless import (CHANNEL_PORTS, TCP6Stream, InnerChannelParser, WirelessConnectionError,
                             discovery_message, discover_phones)


class SixListeners:
    def __init__(self):
        self.listeners = {}
        self.peers = {}
        for channel in CHANNEL_PORTS:
            listener = socket.socket()
            listener.bind(('127.0.0.1', 0))
            listener.listen(1)
            listener.settimeout(2)
            self.listeners[channel] = listener
        self.ports = {channel: sock.getsockname()[1] for channel, sock in self.listeners.items()}

    def accept(self):
        for channel, listener in self.listeners.items():
            self.peers[channel], _ = listener.accept()
            self.peers[channel].settimeout(2)
        return self.peers

    def close(self):
        for sock in [*self.listeners.values(), *self.peers.values()]:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()


class WirelessWireTests(unittest.TestCase):
    def setUp(self):
        self.server = SixListeners()
        self.addCleanup(self.server.close)
        self.stream = TCP6Stream.connect('127.0.0.1', ports=self.server.ports)
        self.addCleanup(self.stream.close)
        self.peers = self.server.accept()
        self.stream.settimeout(.05)

    def receive_packet(self):
        parser = FrameParser()
        while True:
            for packet in parser.feed(self.stream.recv(65536)):
                return packet

    def test_six_channels_use_native_headers_and_same_source(self):
        # Explicit bytes, independently specified from the adapter's codec.
        packets = [Packet(CMD, 0x18001, b'\x08\x02'), Packet(TOUCH, 0x68008, b'\x08\x01'),
                   Packet(VIDEO, VIDEO_HEARTBEAT)]
        self.stream.sendall(b''.join(packet.encode() for packet in packets))
        self.assertEqual(self.peers[CMD].recv(1024), bytes.fromhex('00020000000180010802'))
        self.assertEqual(self.peers[TOUCH].recv(1024), bytes.fromhex('00020000000680080801'))
        self.assertEqual(self.peers[VIDEO].recv(1024), bytes.fromhex('000000000000000000020002'))
        self.assertEqual(len({sock.getpeername()[0] for sock in self.peers.values()}), 1)
        self.assertEqual(self.stream.evidence['connected_channels'], 6)

    def test_fragmented_video_does_not_block_control_or_audio(self):
        video = Packet(VIDEO, 0x20001, b'abcde', 123)
        self.peers[VIDEO].sendall(video.encode()[8:19])
        with self.assertRaises(socket.timeout):
            self.stream.recv(1024)
        command = Packet(CMD, 0x10002, b'\x08\x01')
        self.peers[CMD].sendall(command.encode()[8:])
        self.assertEqual(self.receive_packet(), command)
        sound = Packet(MEDIA, 0x30006, b'1234', 456)
        self.peers[MEDIA].sendall(sound.encode()[8:])
        self.assertEqual(self.receive_packet(), sound)
        self.peers[VIDEO].sendall(video.encode()[19:])
        self.assertEqual(self.receive_packet(), video)

    def test_partial_header_eof_fails_and_closes_all_sockets(self):
        self.peers[VIDEO].sendall(b'\x00\x00')
        with self.assertRaises(socket.timeout):
            self.stream.recv(1024)
        self.peers[VIDEO].shutdown(socket.SHUT_RDWR)
        with self.assertRaisesRegex(ProtocolError, 'VIDEO truncated EOF'):
            self.stream.recv(1024)
        self.assertTrue(self.stream.closed.is_set())
        self.assertTrue(all(sock.fileno() == -1 for sock in self.stream.sockets.values()))

    def test_shutdown_interrupts_reader_and_is_idempotent(self):
        self.stream.settimeout(2)
        done = threading.Event()
        def read():
            try:
                self.stream.recv(1024)
            except (OSError, ValueError):
                pass
            finally:
                done.set()
        reader = threading.Thread(target=read)
        reader.start()
        self.stream.shutdown()
        self.assertTrue(done.wait(.5))
        reader.join()
        self.stream.close()

    def test_channel_loss_ends_session(self):
        self.peers[MEDIA].shutdown(socket.SHUT_RDWR)
        with self.assertRaisesRegex(ConnectionError, 'MEDIA'):
            self.stream.recv(1024)
        self.assertTrue(self.stream.closed.is_set())
        cause = self.stream.failure_reason
        self.assertIn('MEDIA', cause)
        # A second worker's shutdown exception cannot overwrite the cause.
        self.assertFalse(self.stream._abort(OSError('closed socket'), CMD))
        self.assertEqual(self.stream.failure_reason, cause)
        with self.assertRaises(InterruptedError):
            self.stream.sendall(Packet(CMD, 0x18001).encode())

    def test_normal_shutdown_does_not_record_failure(self):
        self.stream.close()
        self.assertIsNone(self.stream.failure_reason)


class InnerParserTests(unittest.TestCase):
    def test_bytewise_and_coalesced_frames_all_channels(self):
        for channel in CHANNEL_PORTS:
            first = Packet(channel, channel << 16 | 1, b'payload', 100 if channel not in (CMD, TOUCH) else 0)
            second = Packet(channel, channel << 16 | 2)
            parser = InnerChannelParser(channel)
            frames = []
            for byte in first.encode()[8:]:
                frames.extend(parser.feed(bytes([byte])))
            frames.extend(parser.feed(second.encode()[8:] * 2))
            self.assertEqual(frames, [first.encode(), second.encode(), second.encode()])
            parser.eof()

    def test_rejects_lengths_ids_and_reserved_before_payload_allocation(self):
        for channel, wire in [(VIDEO, struct.pack('>III', 0xffffffff, 0, 0x20001)),
                              (CMD, struct.pack('>HHI', 0, 1, 0x10002)),
                              (CMD, struct.pack('>HHI', 1, 0, 0x20002)),
                              (TOUCH, struct.pack('>HHI', 0, 0, 0x10002))]:
            with self.subTest(channel=channel, wire=wire), self.assertRaises(ProtocolError):
                InnerChannelParser(channel).feed(wire)

    def test_empty_command_heartbeat_compatibility_is_narrow(self):
        wire = bytes.fromhex('0000000000020002')
        self.assertEqual(InnerChannelParser(CMD).feed(wire), [Packet(VIDEO, VIDEO_HEARTBEAT).encode()])

    def test_partial_connect_rolls_back_and_reports_failed_channel(self):
        server = SixListeners()
        self.addCleanup(server.close)
        server.listeners[MEDIA].close()
        opened = []
        class AttemptedPeerSocket(socket.socket):
            """Winsock can remember a peer even when connect reports refusal."""
            def connect(self, address):
                self.attempted_peer = address
                return super().connect(address)

            def getpeername(self):
                try:
                    return super().getpeername()
                except OSError:
                    return self.attempted_peer
        def create(*args, **kwargs):
            sock = AttemptedPeerSocket(*args, **kwargs)
            opened.append(sock)
            return sock
        with patch('hu_lab.wireless.socket.socket', side_effect=create):
            with self.assertRaises(WirelessConnectionError) as caught:
                TCP6Stream.connect('127.0.0.1', ports=server.ports)
        failure = caught.exception.connection_failure
        self.assertEqual(failure['channel'], 'MEDIA')
        self.assertEqual(failure['connected_channels'], ['CMD', 'VIDEO'])
        self.assertTrue(all(sock.fileno() == -1 for sock in opened))


class DiscoveryTests(unittest.TestCase):
    def test_only_known_beacons_or_ready_json_are_accepted(self):
        self.assertEqual(discovery_message(b'CarlifeHost\x00'), 'CarLife beacon')
        self.assertEqual(discovery_message(b'{"status":"ready","carlifehost":"carlife"}'), 'HeadSwap ready')
        for data in (b'carlifehost', b'{"carlifehost":"carlife"}', b'[]', b'null', b'\xff', b'{}'*1024):
            self.assertIsNone(discovery_message(data))

    def test_probe_response_uses_sender_address_and_deduplicates(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as server:
            server.bind(('127.0.0.1', 0))
            server.settimeout(2)
            errors = []
            def reply():
                try:
                    data, sender = server.recvfrom(1024)
                    self.assertIn(b'carlifehost', data)
                    response = b'{"carlifehost":"carlife","status":"ready","ip":"8.8.8.8"}'
                    server.sendto(response, sender)
                    server.sendto(response, sender)
                except Exception as error:
                    errors.append(error)
            worker = threading.Thread(target=reply)
            worker.start()
            phones = discover_phones(.15, port=0, probe_port=server.getsockname()[1], targets=['127.0.0.1'])
            worker.join(2)
            self.assertFalse(errors)
            self.assertEqual([phone.host for phone in phones], ['127.0.0.1'])

    def test_busy_beacon_port_has_manual_address_guidance(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as listener:
            listener.bind(('', 0))
            with self.assertRaisesRegex(OSError, 'введите IP'):
                discover_phones(.1, port=listener.getsockname()[1])
