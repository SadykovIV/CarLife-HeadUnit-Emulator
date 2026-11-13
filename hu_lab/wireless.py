"""Native CarLife Wi-Fi: six TCP channels and UDP phone discovery.

The phone listens; the HU connects all six channels before sending HU_VERSION.
Only inner CarLife frames go on the network. The socket-compatible adapter
adds/removes the lab's multiplex header internally so USB and Wi-Fi share the
same session, decoder, audio, cluster and input implementation.
"""
from collections import deque
from dataclasses import dataclass
import ipaddress
import json
import select
import socket
import struct
import threading
import time

from .protocol import CMD, TOUCH, VIDEO, VIDEO_HEARTBEAT, MAX_INNER, FrameParser, Packet, ProtocolError

CHANNEL_PORTS = {1: 7240, 2: 8240, 3: 9240, 4: 9241, 5: 9242, 6: 9340}
CHANNEL_NAMES = {1: "CMD", 2: "VIDEO", 3: "MEDIA", 4: "TTS", 5: "VR", 6: "TOUCH"}
BEACON_PORT = 7999
PROBE_PORT = 8999
BEACON = b"CarlifeHost"
PROBE = b'{"carlifehost":"carlife"}'


class WirelessConnectionError(ConnectionError):
    def __init__(self, host, channel, port, error, connected):
        name = CHANNEL_NAMES.get(channel, "DNS")
        super().__init__(f"Wi-Fi: не удалось открыть {name} ({host}:{port}). "
                         f"Включите беспроводное ожидание в HeadSwap и подключите компьютер "
                         f"к той же сети или точке доступа телефона. Причина: {error}")
        self.connection_failure = dict(stage="wireless_connect", reason="tcp6_channel_unavailable",
            host=host, channel=name, port=port, connected_channels=list(connected),
            all_channels_required=True, cause=f"{type(error).__name__}: {error}")


class InnerChannelParser:
    """Incremental, bounded parser for one native TCP channel."""
    def __init__(self, channel):
        if channel not in CHANNEL_PORTS:
            raise ValueError("unknown CarLife channel")
        self.channel = channel
        self.buffer = bytearray()

    def feed(self, data):
        if len(data) > 65536:
            raise ProtocolError("Wi-Fi input chunk exceeds bound")
        self.buffer.extend(data)
        header = 8 if self.channel in (CMD, TOUCH) else 12
        frames = []
        while len(self.buffer) >= header:
            if header == 8:
                length, reserved, message = struct.unpack_from(">HHI", self.buffer)
                if reserved:
                    raise ProtocolError("Wi-Fi nonzero reserved field")
            else:
                length, _, message = struct.unpack_from(">III", self.buffer)
            size = header + length
            if size > MAX_INNER:
                raise ProtocolError("Wi-Fi inner length exceeds bound")
            # Tolerate the empty command-style VIDEO_HEARTBEAT accepted by
            # HeadSwap's compatibility codec; never generalize cross-channel IDs.
            command_heartbeat = self.channel == CMD and message == VIDEO_HEARTBEAT and length == 0
            if message >> 16 != self.channel and not command_heartbeat:
                raise ProtocolError("Wi-Fi message/channel mismatch")
            if len(self.buffer) < size:
                break
            if command_heartbeat:
                frames.append(Packet(VIDEO, VIDEO_HEARTBEAT).encode())
            else:
                frames.append(struct.pack(">II", self.channel, size) + self.buffer[:size])
            del self.buffer[:size]
        if len(self.buffer) > MAX_INNER:
            raise ProtocolError("Wi-Fi pending buffer exceeds bound")
        return frames

    def eof(self):
        if self.buffer:
            raise ProtocolError(f"Wi-Fi {CHANNEL_NAMES[self.channel]} truncated EOF: {len(self.buffer)} bytes")


class TCP6Stream:
    """One reader, one writer, six sockets; loss of any channel ends the session."""
    def __init__(self, sockets, host):
        if set(sockets) != set(CHANNEL_PORTS):
            raise ValueError("all six CarLife sockets are required")
        self.sockets = dict(sockets)
        self.channels = {sock: channel for channel, sock in sockets.items()}
        self.parsers = {channel: InnerChannelParser(channel) for channel in sockets}
        self.outbound = FrameParser()
        self.ready = deque()
        self.pending = bytearray()
        self.closed = threading.Event()
        self.close_lock = threading.Lock()
        self.failure_reason = None
        self.timeout = .5
        self.rx_bytes = self.tx_bytes = 0
        self.evidence = dict(backend="TCP6_WIFI", host=host, wire="native_inner_per_channel",
            channels={CHANNEL_NAMES[c]: s.getpeername()[1] for c, s in sockets.items()},
            peer=sockets[CMD].getpeername()[0], local_ip=sockets[CMD].getsockname()[0],
            connected_channels=6, adb_data_plane=False)

    @classmethod
    def connect(cls, host, timeout=6.0, ports=None):
        ports = dict(CHANNEL_PORTS if ports is None else ports)
        if set(ports) != set(CHANNEL_PORTS):
            raise ValueError("all six CarLife ports are required")
        sockets = {}
        connected = []
        channel, port = CMD, ports[CMD]
        deadline = time.monotonic() + timeout
        try:
            # Resolve once; every channel must reach the same MD and use the
            # same local interface even with several active network adapters.
            addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            # HeadSwap's LAN listener/beacon is IPv4. Prefer that route for
            # dual-stack hostnames, while still supporting explicit IPv6 peers.
            family, socktype, proto, _, target = next(
                (entry for entry in addresses if entry[0] == socket.AF_INET), addresses[0])
            local = None
            for channel, port in ports.items():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("six-channel connection deadline")
                sock = socket.socket(family, socktype, proto)
                sockets[channel] = sock
                sock.settimeout(min(remaining, 2.0))
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                if local is not None:
                    sock.bind((local[0], 0, *local[2:]))
                sock.connect((target[0], port, *target[2:]))
                connected.append(CHANNEL_NAMES[channel])
                local = sock.getsockname()
            result = cls(sockets, host)
            result.settimeout(.5)
            return result
        except Exception as error:
            # Winsock may return the attempted peer even after connect failed.
            # Only successful connect returns count toward six-channel readiness.
            for sock in sockets.values():
                sock.close()
            raise WirelessConnectionError(host, channel, port, error, connected) from error

    def settimeout(self, timeout):
        if timeout is None or timeout <= 0:
            raise ValueError("Wi-Fi requires a finite positive IO timeout")
        self.timeout = timeout
        for sock in self.sockets.values():
            sock.settimeout(timeout)

    def sendall(self, data):
        if self.closed.is_set():
            raise InterruptedError("Wi-Fi connection closed")
        channel = None
        try:
            # Existing writer submits complete multiplex packets. Retain an
            # incremental parser for socket-like fragmented callers as well.
            for packet in self.outbound.feed(data):
                channel = packet.channel
                inner = packet.encode()[8:]
                self.sockets[packet.channel].sendall(inner)
                self.tx_bytes += len(inner)
        except Exception as error:
            if not self._abort(error, channel):
                raise InterruptedError(self.failure_reason or "Wi-Fi connection closed") from error
            raise

    def recv(self, size):
        if size <= 0:
            return b""
        deadline = time.monotonic() + self.timeout
        channel = None
        try:
            while not self.pending:
                if self.closed.is_set():
                    raise InterruptedError("Wi-Fi connection closed")
                if self.ready:
                    self.pending.extend(self.ready.popleft())
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise socket.timeout("Wi-Fi receive timeout")
                readable, _, _ = select.select(list(self.channels), [], [], remaining)
                if not readable:
                    raise socket.timeout("Wi-Fi receive timeout")
                # Read each ready channel once so video cannot starve control,
                # audio or cluster metadata. Partial frames stay per-channel.
                for sock in readable:
                    channel = self.channels[sock]
                    data = sock.recv(65536)
                    if not data:
                        self.parsers[channel].eof()
                        raise ConnectionError(f"Wi-Fi: канал {CHANNEL_NAMES[channel]} закрыт телефоном")
                    self.rx_bytes += len(data)
                    self.ready.extend(self.parsers[channel].feed(data))
            data = bytes(self.pending[:size])
            del self.pending[:size]
            return data
        except socket.timeout:
            raise
        except Exception as error:
            if not self._abort(error, channel):
                raise InterruptedError(self.failure_reason or "Wi-Fi connection closed") from error
            raise

    def _abort(self, error, channel):
        # Record the cause before shutdown wakes the other IO worker. Its
        # cancellation must not replace the original channel/error in reports.
        with self.close_lock:
            first = self.failure_reason is None and not self.closed.is_set()
            if first:
                name = CHANNEL_NAMES.get(channel, "transport")
                self.failure_reason = f"Wi-Fi {name}: {type(error).__name__}: {error}"
        self.close()
        return first

    def shutdown(self, _how=socket.SHUT_RDWR):
        self.close()

    def close(self):
        with self.close_lock:
            if self.closed.is_set():
                return
            self.closed.set()
            for sock in self.sockets.values():
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                sock.close()


@dataclass(frozen=True)
class WirelessPhone:
    host: str
    discovery: str


def discovery_message(data):
    """Recognize exact native beacons or the optional HeadSwap ready response."""
    if data.strip(b"\x00\r\n ") == BEACON:
        return "CarLife beacon"
    if len(data) > 1024:
        return None
    try:
        message = json.loads(data)
    except (ValueError, UnicodeDecodeError):
        return None
    if isinstance(message, dict) and message.get("carlifehost") == "carlife" and message.get("status") == "ready":
        return "HeadSwap ready"
    return None


def discover_phones(timeout=4.5, *, port=BEACON_PORT, probe_port=PROBE_PORT, targets=None):
    """Listen for native phone beacons; also probe HeadSwap without opening TCP.

    Network-provided hostnames/URLs are never followed. Only the sender address
    is offered for explicit selection. This does not change Wi-Fi or Bluetooth.
    """
    found = {}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as listener:
        try:
            listener.bind(("", port))
        except OSError as error:
            raise OSError(f"Wi-Fi: UDP {port} занят или недоступен. Закройте другой поиск CarLife "
                          "или введите IP телефона вручную.") from error
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.bind(("", 0))
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            for target in targets or ("255.255.255.255",):
                try:
                    probe.sendto(PROBE, (target, probe_port))
                except OSError:
                    pass  # Native phone beacons remain usable without probes.
            deadline = time.monotonic() + timeout
            while (remaining := deadline - time.monotonic()) > 0:
                readable, _, _ = select.select([listener, probe], [], [], remaining)
                for sock in readable:
                    data, sender = sock.recvfrom(2048)
                    source = discovery_message(data)
                    address = ipaddress.ip_address(sender[0])
                    if source and not address.is_unspecified and not address.is_multicast:
                        found[sender[0]] = WirelessPhone(sender[0], source)
    return sorted(found.values(), key=lambda phone: ipaddress.ip_address(phone.host))
