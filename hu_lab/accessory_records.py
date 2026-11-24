"""Cancellation-aware raw bulk stream for the alternate AOA protocols."""
import asyncio


class BulkRecords:
    def __init__(self, stream):
        self.stream = stream
        self.buffer = bytearray()
        self.write_lock = asyncio.Lock()

    async def read(self, n):
        while len(self.buffer) < n:
            try:
                chunk = await asyncio.to_thread(self.stream.recv, 65536)
            except TimeoutError:
                continue
            if not chunk:
                raise EOFError("Телефон закрыл USB")
            self.buffer.extend(chunk)
        data = bytes(self.buffer[:n])
        del self.buffer[:n]
        return data

    async def write(self, header, body):
        async with self.write_lock:
            await asyncio.to_thread(self.stream.write_accessory_record, header, body)
