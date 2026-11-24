"""Bounded TCP/RTP/PES receive path for HiSight's non-VTP transport."""
import time
from .cipher import Cipher
from .nearby_wire import ProtocolError

MAX_FRAME = 2 * 1024 * 1024


class Depacketizer:
    def __init__(self, key):
        self.cipher = Cipher(key, MAX_FRAME)
        self.parts = bytearray()
        self.sequence = None
        self.timestamp = None

    def receive(self, packet):
        if len(packet) < 12 or packet[0] >> 6 != 2 or packet[0] & 0x20:
            raise ProtocolError('invalid HiSight RTP header')
        kind = packet[1] & 0x7f
        if kind not in (96, 97, 98):
            raise ProtocolError('unsupported HiSight RTP type')
        sequence = int.from_bytes(packet[2:4], 'big')
        timestamp = int.from_bytes(packet[4:8], 'big')
        if (self.sequence is not None and sequence != (self.sequence + 1) % 65536
                or self.timestamp is not None and timestamp != self.timestamp):
            self.parts.clear()
        self.sequence, self.timestamp = sequence, timestamp
        offset = 12 + (packet[0] & 15) * 4
        if packet[0] & 0x10:
            if kind == 96:
                if offset + 4 > len(packet):
                    raise ProtocolError('truncated HiSight RTP extension')
                offset += 4 + int.from_bytes(packet[offset + 2:offset + 4], 'big')
            else:
                offset += 1
        if offset >= len(packet) or len(self.parts) + len(packet) - offset > MAX_FRAME:
            raise ProtocolError('invalid HiSight RTP payload size')
        self.parts.extend(packet[offset:])
        if not packet[1] & 0x80:
            return None
        pes = bytes(self.parts)
        self.parts.clear()
        if len(pes) < 14 or pes[:3] != b'\0\0\1' or pes[6] != 0x84:
            raise ProtocolError('invalid HiSight PES header')
        size = int.from_bytes(pes[4:6], 'big')
        if size and size + 6 != len(pes):
            raise ProtocolError('invalid HiSight PES size')
        offset = 14
        if pes[7] & 1 and pes[8]:
            offset += pes[8] + 1
        if offset + 16 >= len(pes):
            raise ProtocolError('truncated HiSight encrypted ES')
        return kind, timestamp, self.cipher.decrypt(pes[offset:])
