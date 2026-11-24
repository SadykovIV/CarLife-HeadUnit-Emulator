"""DNS-SD for one explicitly selected local IPv4 link."""
import asyncio
import ipaddress
import socket
import struct

GROUP = "224.0.0.251"
CARBIT = (b"_EasyConn", b"_tcp", b"local")
NEARBY = (b"_nearby", b"_tcp", b"local")


def interfaces():
    from PySide6.QtNetwork import QNetworkInterface, QAbstractSocket
    rows = []
    for nic in QNetworkInterface.allInterfaces():
        if not nic.flags() & QNetworkInterface.IsUp:
            continue
        for entry in nic.addressEntries():
            if entry.ip().protocol() == QAbstractSocket.IPv4Protocol:
                rows.append((entry.ip().toString(), entry.prefixLength(), nic.humanReadableName()))
    return rows


def dns_name(labels):
    return b"".join(bytes([len(word)]) + word for word in labels) + b"\0"


def read_name(data, start):
    labels, seen = [], set()
    at, end = start, None
    while at < len(data) and at not in seen and len(seen) < 128:
        seen.add(at)
        size = data[at]
        at += 1
        if not size:
            return tuple(word.lower() for word in labels), end or at
        if size & 192 == 192 and at < len(data):
            if end is None:
                end = at + 1
            at = ((size & 63) << 8) | data[at]
        elif size <= 63 and at + size <= len(data):
            labels.append(data[at:at + size])
            at += size
        else:
            break
    raise ValueError("Malformed DNS name")


def answer(data, service, address, port, legacy):
    if not 12 <= len(data) <= 4096:
        return None
    ident, flags, count, *_ = struct.unpack_from("!6H", data)
    if flags & 0xf800 or not 1 <= count <= 8:
        return None
    prefix = b"com.huawei.nearby.usb.nsd" if service == NEARBY else b"HSD HeadUnit Emulator"
    instance, host = (prefix, *service), (b"hsd-headunit-" + socket.inet_aton(address).hex().encode(), b"local")
    records = [(service, 12, dns_name(instance)), (instance, 33, struct.pack("!HHH", 0, 0, port) + dns_name(host)),
               (instance, 16, dns_name([b"ncm.ip=" + address.encode()])[:-1] if service == NEARBY else b"\0"),
               (host, 1, socket.inet_aton(address))]
    offset, questions, match, unicast = 12, [], False, legacy
    for _ in range(count):
        name, offset = read_name(data, offset)
        kind, cls = struct.unpack_from("!HH", data, offset)
        offset += 4
        questions.append(dns_name(name) + struct.pack("!HH", kind, cls))
        if cls & 0x7fff == 1 and any(name == tuple(x.lower() for x in key) and kind in (typ, 255)
                                     for key, typ, _ in records):
            match = True
            unicast |= bool(cls & 0x8000)
    if not match:
        return None
    reply = struct.pack("!6H", ident if legacy else 0, 0x8400, count if legacy else 0, len(records), 0, 0)
    if legacy:
        reply += b"".join(questions)
    for labels, kind, payload in records:
        reply += dns_name(labels) + struct.pack("!HHIH", kind, 1, 10 if legacy else 120, len(payload)) + payload
    return reply, unicast


class Advertiser(asyncio.DatagramProtocol):
    def __init__(self, service, address, prefix, port):
        self.service, self.address, self.port = service, address, port
        self.subnet = ipaddress.IPv4Network((address, prefix), strict=False)

    async def start(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, "SO_REUSEPORT"):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            sock.bind(("", 5353))
            iface = socket.inet_aton(self.address)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, socket.inet_aton(GROUP) + iface)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, iface)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
            transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(lambda: self, sock=sock)
            return transport
        except BaseException:
            sock.close()
            raise

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, peer):
        if ipaddress.ip_address(peer[0]) not in self.subnet:
            return
        try:
            result = answer(data, self.service, self.address, self.port, peer[1] != 5353)
        except (ValueError, struct.error):
            return
        if result:
            reply, unicast = result
            self.transport.sendto(reply, peer if unicast else (GROUP, 5353))
