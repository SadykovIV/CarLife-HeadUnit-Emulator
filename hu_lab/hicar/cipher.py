"""HiSight records use the first 128 bits of the HiChain session key."""
import os

from cryptography.hazmat.primitives.ciphers import Cipher as AES, algorithms, modes
from .nearby_wire import ProtocolError


class Cipher:
    def __init__(self, key, max_record=1500):
        if len(key) not in (16, 32):
            raise ProtocolError("Invalid HiChain session key")
        self.key = bytearray(key[:16])
        self.max_record = max_record

    def transform(self, data, iv):
        if len(iv) != 16 or not self.key or not 0 < len(data) < self.max_record:
            raise ProtocolError("Invalid AES-CTR record")
        cipher = AES(algorithms.AES(self.key), modes.CTR(iv)).encryptor()
        return cipher.update(data) + cipher.finalize()

    def encrypt(self, data):
        iv = os.urandom(16)
        if len(data)+16 >= self.max_record:
            raise ProtocolError("Oversized AES-CTR record")
        return iv + self.transform(data, iv)

    def decrypt(self, record):
        if not 16 < len(record) < self.max_record:
            raise ProtocolError("Invalid encrypted record length")
        return self.transform(record[16:], record[:16])

    def close(self):
        self.key[:] = bytes(len(self.key))
        self.key.clear()
