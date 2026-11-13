"""Normal desktop Wi-Fi entry point against an independently framed TCP peer.

DemoPeer supplies the synthetic Honda handshake, real H.264, PCM and metadata.
Only this test relay converts its development stream to native TCP channels.
The peer does not use either production wireless parser or adapter.
"""
from collections import Counter
from io import BytesIO
from pathlib import Path
import select
import socket
import struct
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from PIL import Image

from hu_lab.desktop_demo import DemoPeer
from hu_lab.desktop_session import DesktopConfig, create_client
from hu_lab.protocol import fields
from hu_lab.wireless import TCP6Stream
from tests.test_wireless import SixListeners


class NativeDemoRelay:
    def __init__(self):
        self.server = SixListeners()
        self.demo = DemoPeer(tone=True)
        self.bridge = None
        self.stopping = threading.Event()
        self.connected = threading.Event()
        self.error = None
        self.sent_channels = set()
        self.received = []
        self.thread = threading.Thread(target=self.run, daemon=True, name="test-native-phone")
        self.thread.start()

    def run(self):
        try:
            peers = self.server.accept()
            self.bridge = socket.create_connection(("127.0.0.1", self.demo.port), timeout=2)
            self.connected.set()
            buffers = {sock: bytearray() for sock in [self.bridge, *peers.values()]}
            channels = {sock: channel for channel, sock in peers.items()}
            while not self.stopping.is_set():
                readable, _, _ = select.select(list(buffers), [], [], .1)
                for sock in readable:
                    data = sock.recv(65536)
                    if not data:
                        return
                    buffers[sock].extend(data)
                    if sock is self.bridge:
                        self.forward_to_hu(buffers[sock])
                    else:
                        self.forward_to_demo(channels[sock], buffers[sock])
        except (OSError, ValueError) as error:
            if not self.stopping.is_set():
                self.error = f"{type(error).__name__}: {error}"

    def forward_to_hu(self, buffer):
        while len(buffer) >= 8:
            channel, size = struct.unpack_from(">II", buffer)
            if channel not in self.server.peers or not 8 <= size <= 4 * 1024 * 1024:
                raise ValueError("Invalid development packet from synthetic phone")
            if len(buffer) < 8 + size:
                return
            inner = bytes(buffer[8:8 + size])
            del buffer[:8 + size]
            self.sent_channels.add(channel)
            # Deliberately split writes inside the native header. TCP is free
            # to join these writes again, just as on a physical network.
            self.server.peers[channel].sendall(inner[:3])
            self.server.peers[channel].sendall(inner[3:])

    def forward_to_demo(self, channel, buffer):
        header_size = 8 if channel in (1, 6) else 12
        while len(buffer) >= header_size:
            if header_size == 8:
                length, reserved, message = struct.unpack_from(">HHI", buffer)
                if reserved != 0:
                    raise ValueError("HU sent a nonzero reserved command field")
            else:
                length, _, message = struct.unpack_from(">III", buffer)
            size = header_size + length
            if size > 4 * 1024 * 1024 or message >> 16 != channel:
                raise ValueError("HU sent an invalid native header or a multiplex header")
            if len(buffer) < size:
                return
            inner = bytes(buffer[:size])
            del buffer[:size]
            self.received.append((channel, message, inner, time.monotonic()))
            self.bridge.sendall(struct.pack(">II", channel, size) + inner)

    def close_channel(self, channel):
        # Leave the other five sockets open so the client must notice the
        # selected channel's EOF and close the whole session itself.
        self.server.peers[channel].shutdown(socket.SHUT_RDWR)

    def close(self):
        self.stopping.set()
        self.demo.begin_close()
        self.server.close()
        if self.bridge is not None:
            try:
                self.bridge.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.bridge.close()
        self.thread.join(3)
        self.demo.close()


class WirelessSessionIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.output = tempfile.TemporaryDirectory(prefix="carlife-native-wifi-")
        self.addCleanup(self.output.cleanup)
        self.clients = []

    def connect(self):
        peer = NativeDemoRelay()
        self.addCleanup(peer.close)
        audio = Mock()
        client = create_client(
            DesktopConfig(transport="wifi", profile="honda-v2", host="127.0.0.1",
                          width=640, height=360, fps=10, allow_mode_fallback=False),
            Path(self.output.name) / str(len(self.clients)), audio_sink=audio)
        self.clients.append(client)
        self.addCleanup(client.stop)
        # Exercise the real factory. Replace only the fixed port numbers with
        # ephemeral test ports; retain the original native connect method.
        native_connect = TCP6Stream.connect
        with patch.object(TCP6Stream, "connect",
                          side_effect=lambda host: native_connect(host, ports=peer.server.ports)):
            client.start()
        self.assertTrue(peer.connected.wait(2))
        self.wait_for(lambda: client.decoded_frames >= 3, client, peer)
        return client, peer, audio

    def wait_for(self, predicate, client, peer, timeout=10):
        deadline = time.monotonic() + timeout
        while not predicate():
            self.assertIsNone(peer.error)
            self.assertIsNone(peer.demo.error)
            self.assertIsNone(client.error)
            if time.monotonic() >= deadline:
                self.fail(f"Timed out in {client.session.state}, frames={client.decoded_frames}")
            time.sleep(.02)

    def test_normal_wifi_session_decodes_and_delivers_services_and_controls(self):
        client, peer, audio = self.connect()
        self.assertEqual(client.backend, "TCP6_WIFI")
        self.assertEqual(client.session.state, "STREAMING")
        self.assertEqual(client.dimensions, (640, 360))
        self.assertGreater(len(client.frame_hashes), 1)
        self.assertTrue(client.first_frame_before_eof)
        self.assertEqual(client.sock.evidence["connected_channels"], 6)
        self.assertFalse(client.sock.evidence["adb_data_plane"])
        first = peer.received[0]
        self.assertEqual(first[:2], (1, 0x18001))
        self.assertEqual(first[2], bytes.fromhex("000400000001800108021000"))

        def media_ready():
            state = client.session.cluster.snapshot()
            return (state["navigation"] is not None and state["music"] is not None and
                    state["music"]["position_ms"] is not None and
                    client.audio.summary()["lanes"]["MEDIA"]["pcm_bytes"] > 0)

        self.wait_for(media_ready, client, peer)
        state = client.session.cluster.snapshot()
        self.assertEqual(state["navigation"]["turn_id"], 3)
        self.assertEqual(state["navigation"]["road"], "Набережная")
        self.assertGreater(state["navigation"]["distance_m"], 0)
        self.assertEqual(state["music"]["title"], "Ночной рейс")
        self.assertEqual(state["music"]["artist"], "Городские огни")
        with Image.open(BytesIO(state["music"]["artwork"])) as cover:
            cover.load()
            self.assertEqual(cover.format, "JPEG")
            self.assertEqual(cover.size, (256, 256))
        sound = client.audio.summary()["lanes"]["MEDIA"]
        self.assertGreater(sound["non_silent_bytes"], 0)
        self.assertEqual(sound["errors"], 0)
        self.assertTrue(any(call.args[0].message == 0x30006 for call in audio.submit.call_args_list))
        self.assertTrue({1, 2, 3}.issubset(peer.sent_channels))

        client.tap(100, 150)
        client.home()
        client.back()

        def input_and_heartbeat_ready():
            counts = Counter(message for _, message, _, _ in peer.received)
            return counts[0x68001] >= 2 and counts[0x68008] >= 2 and counts[0x20002] >= 2

        self.wait_for(input_and_heartbeat_ready, client, peer)
        keys = [fields(inner[8:])[1] for channel, message, inner, _ in peer.received
                if channel == 6 and message == 0x68008]
        self.assertEqual(keys, [1, 14])
        touches = [fields(inner[8:]) for channel, message, inner, _ in peer.received
                   if channel == 6 and message == 0x68001]
        self.assertEqual([packet[1] for packet in touches], [0, 1])
        self.assertTrue(all(packet[2] == 100 and packet[3] == 150 for packet in touches))
        heartbeats = [row for row in peer.received if row[1] == 0x20002]
        self.assertTrue(all(row[0] == 2 and row[2] == bytes.fromhex("000000000000000000020002")
                            for row in heartbeats))
        self.assertGreaterEqual(heartbeats[1][3] - heartbeats[0][3], .8)
        self.assertLess(heartbeats[1][3] - heartbeats[0][3], 3)

        peer.demo.begin_close()
        client.stop()
        self.assertIsNone(client.error)
        self.assertEqual(client.session.state, "DISCONNECTED")
        self.assertFalse(any(thread.is_alive() for thread in client.threads))
        self.assertTrue(all(sock.fileno() == -1 for sock in client.sock.sockets.values()))
        self.assertIsNone(client.session.cluster.snapshot()["navigation"])
        self.assertIsNone(client.session.cluster.snapshot()["music"])

    def test_channel_loss_closes_session_and_fresh_connection_decodes_again(self):
        client, peer, _ = self.connect()
        peer.close_channel(3)
        self.assertTrue(client.halt.wait(3))
        self.assertIsNotNone(client.error)
        self.assertIn('MEDIA', client.error)
        self.assertEqual(client.error, client.sock.failure_reason)
        self.assertEqual(client.session.state, "ERROR")
        self.assertTrue(all(sock.fileno() == -1 for sock in client.sock.sockets.values()))
        client.stop()
        self.assertFalse(any(thread.is_alive() for thread in client.threads))
        peer.close()

        reconnected, fresh_peer, _ = self.connect()
        self.assertNotEqual(reconnected.session_id, client.session_id)
        self.assertEqual(reconnected.session.state, "STREAMING")
        self.assertIsNone(reconnected.error)
        self.assertEqual(fresh_peer.received[0][:2], (1, 0x18001))


if __name__ == "__main__":
    unittest.main()
