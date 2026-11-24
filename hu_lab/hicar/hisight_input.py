"""Encrypted HiSight UIBC input records."""
import struct
import time
from .cipher import Cipher
from .nearby_wire import ProtocolError


KEYS = {'up': 19, 'down': 20, 'left': 21, 'right': 22, 'enter': 23,
        'back': 4, 'home': 3, 'play_pause': 85}


class Input:
    def __init__(self, key, width, height):
        self.width, self.height = width, height
        self.cipher = Cipher(key)
        self.start = time.monotonic()

    def pack(self, body):
        # UIBC has a four-byte clear header. The six reserved bytes and
        # event body are encrypted with a new IV, as in HiSight CTR records.
        data = self.cipher.encrypt(bytes([255]) * 6 + body)
        return b'\0\0' + (len(data) + 4).to_bytes(2, 'big') + data

    def key(self, name, down):
        if not isinstance(name, str) or name not in KEYS or type(down) is not bool:
            raise ProtocolError('unsupported input key')
        return self.pack(struct.pack('>BHBHHI', 3 if down else 4, 9, 0,
                                     KEYS[name], 0, 0))

    def touch(self, x, y, down, move=False):
        if type(x) is not int or type(y) is not int or not 0 <= x < self.width or not 0 <= y < self.height:
            raise ProtocolError('input coordinate outside negotiated display')
        if type(down) is not bool or type(move) is not bool or move and not down:
            raise ProtocolError('invalid touch action')
        elapsed = int((time.monotonic() - self.start) * 1000) & 0xffffffff
        return self.pack(struct.pack('>BHBBHHIB', 2 if move else 0 if down else 1, 11, 1,
                                     0, x, y, elapsed, 0))
