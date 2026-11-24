"""EasyConn head unit over reverse TCP or app-owned accessory channels."""
import asyncio
import base64
import json
import secrets
import struct

from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.serialization import load_der_public_key

from .accessory_records import BulkRecords
from .receiver import Listener, send
from .receiver_discovery import Advertiser, CARBIT

VIDEO, MEDIA, CONTROL = 10920, 10921, 10922
PXC = struct.Struct("<4I")
REQUEST = struct.Struct("<HHI")
EC = struct.Struct("<HBBHHHBBI")
ADB = struct.Struct("<4s5I")


def media(code, data=b""):
    return REQUEST.pack(code, len(data), 0) + data


class Channel:
    def __init__(self, reader, write):
        self.reader, self.write = reader, write

    async def read(self, n):
        return await asyncio.wait_for(self.reader.readexactly(n), 10)


async def wifi(client):
    own = client.config.host
    listener = await Listener(own).open(client.resources, 10930)
    try:
        advertiser = await Advertiser(CARBIT, own, client.config.prefix_length, listener.port).start()
    except OSError:
        hint = "Укажите IP вручную в HSD"
    else:
        client.resources.callback(advertiser.close)
        hint = "В HSD выберите CarbitLink по Wi-Fi"
    client.stage("WAIT_PHONE", f"{hint} · адрес ГУ {own}")
    reader, writer = await listener.arrival
    peer = writer.get_extra_info("peername")[0]
    client.peer = peer
    async with asyncio.timeout(8):
        code, _, flag, size, _, check, _, tail, _ = EC.unpack(await reader.readexactly(16))
        if (code, flag, tail) != (16, 0x70, 0x70) or size < 16 or check != (size ^ code):
            raise ValueError("Некорректная идентификация CarbitLink")
        identity = json.loads(await reader.readexactly(size-16))
        if not isinstance(identity, dict) or identity.get("phoneType") != "Android":
            raise ValueError("Требуется отправитель CarbitLink для Android")
        channels = {}
        for port in (CONTROL, VIDEO, MEDIA):
            rx, tx = await asyncio.open_connection(peer, port, local_addr=(own, 0))
            client.resources.callback(tx.close)
            channels[port] = Channel(rx, lambda data, tx=tx: send(tx, data))
        await send(writer, EC.pack(17, 0, 0x70, 19, 0, 2, 0, 0x70, 0) + b"{}\n")
        writer.close()
    return channels


class AccessoryMux:
    """Only the three EasyConn services on the selected accessory interface."""
    def __init__(self, stream):
        self.bulk = BulkRecords(stream)
        self.ids = {}
        self.acks = {}
        self.readers = {port: asyncio.StreamReader() for port in (CONTROL, MEDIA, VIDEO)}
        self.size = 4096

    async def packet(self, command, local, remote, data=b""):
        magic = int.from_bytes(command, "little") ^ 0xffffffff
        await self.bulk.write(ADB.pack(command, local, remote, len(data), sum(data), magic), data)

    async def next(self):
        cmd, remote, local, size, checksum, magic = ADB.unpack(await self.bulk.read(24))
        if size > self.size or magic != int.from_bytes(cmd, "little") ^ 0xffffffff:
            raise ValueError("Неверный заголовок USB CarbitLink")
        data = await self.bulk.read(size)
        if sum(data) != checksum:
            raise ValueError("Неверная контрольная сумма USB CarbitLink")
        if cmd == b"AUTH":
            raise ValueError("Телефон требует авторизацию производителя CarbitLink")
        return cmd, remote, local, data

    async def open(self):
        await self.packet(b"CNXN", 0x01000000, self.size, b"host::\0")
        cmd, version, size, _ = await self.next()
        if cmd != b"CNXN" or version < 0x01000000 or not 24 <= size <= self.size:
            raise ValueError("Телефон не подтвердил USB CarbitLink")
        self.size = size
        for port in self.readers:
            await self.packet(b"OPEN", port, 0, f"tcp:{port}\0".encode())
            cmd, remote, local, _ = await self.next()
            if cmd != b"OKAY" or local != port or not remote or remote in self.ids.values():
                raise ValueError(f"Телефон не открыл канал CarbitLink {port}")
            self.ids[port] = remote

    async def pump(self):
        while True:
            cmd, remote, local, data = await self.next()
            if self.ids.get(local) != remote:
                raise ValueError("Неизвестный канал USB CarbitLink")
            if cmd == b"WRTE":
                reader = self.readers[local]
                # Each video read is solicited; unsolicited data must stay bounded.
                if len(reader._buffer) + len(data) > 8_500_000:
                    raise ValueError("Переполнение канала CarbitLink")
                reader.feed_data(data)
                await self.packet(b"OKAY", local, remote)
            elif cmd == b"OKAY" and local in self.acks and not self.acks[local].done():
                self.acks[local].set_result(None)
            elif cmd == b"CLSE":
                raise EOFError("Телефон закрыл канал CarbitLink")
            else:
                raise ValueError("Неожиданная команда USB CarbitLink")

    async def write(self, port, data):
        for at in range(0, len(data), self.size):
            ack = self.acks[port] = asyncio.get_running_loop().create_future()
            try:
                await self.packet(b"WRTE", port, self.ids[port], data[at:at+self.size])
                await asyncio.wait_for(ack, 5)
            finally:
                self.acks.pop(port, None)


async def project(client, channels):
    client.stage("WAIT_VERSION", "CarbitLink · согласование телефона и ГУ")
    ctl, screen, video = (channels[port] for port in (CONTROL, MEDIA, VIDEO))
    async def pxc(code, data=b""):
        size = 16 + len(data)
        await ctl.write(PXC.pack(code, size, size ^ code, 0) + data)
        reply, length, check, _ = PXC.unpack(await ctl.read(16))
        if reply != code+1 or not 16 <= length <= 65536 or check != length ^ reply:
            raise ValueError("Неверный ответ канала управления CarbitLink")
        return await ctl.read(length-16)

    await pxc(0x10000)
    huid = secrets.token_hex(16)
    phone = json.loads(await pxc(0x10010, json.dumps({"HUID": huid}).encode()))
    key = load_der_public_key(base64.b64decode(phone["pubkey"], validate=True))
    if not isinstance(key, rsa.RSAPublicKey) or key.recover_data_from_signature(
            base64.b64decode(phone["encryptedHUID"], validate=True), padding.PKCS1v15(), None) != huid.encode():
        raise ValueError("Недействительная подпись идентификатора ГУ CarbitLink")
    await pxc(0x70000000)
    async def request(code, body=b""):
        await screen.write(media(code, body))
        reply, size, _ = REQUEST.unpack(await screen.read(8))
        if reply != code+1:
            raise ValueError("Неожиданный ответ медиаканала CarbitLink")
        return await screen.read(size)

    cfg = client.config
    result = await request(16, struct.pack("<HHII", cfg.width, cfg.height, cfg.fps, 2))
    codec, width, height = struct.unpack_from("<IHH", result)
    if codec != 2 or not 16 <= width <= 4096 or not 16 <= height <= 4096:
        raise ValueError("Неподдерживаемый формат CarbitLink")
    await request(112)
    async def touch(events):
        for action, slot, x, y in events:
            await screen.write(media(32, struct.pack("<4H10x", {"down": 2, "up": 1, "move": 3}[action],
                        min(width-1, x*width//client.session.width), min(height-1, y*height//client.session.height), slot)))

    client.input_handler = touch
    client.stage("WAIT_VIDEO", "CarbitLink · ожидание первого кадра")
    while True:
        await video.write(media(114))
        size, = struct.unpack("<I", await video.read(4))
        if size > 8_500_000:
            raise ValueError("Слишком большой кадр CarbitLink")
        if size:
            client.video(await video.read(size))
        else:
            await asyncio.sleep(0.008)


async def run(client):
    if not client.sock:
        return await project(client, await wifi(client))
    mux = AccessoryMux(client.sock)
    client.stage("WAIT_VERSION", "CarbitLink · открытие трёх каналов USB")
    await asyncio.wait_for(mux.open(), 10)
    channels = {port: Channel(rx, lambda data, p=port: mux.write(p, data)) for port, rx in mux.readers.items()}
    async with asyncio.TaskGroup() as group:
        group.create_task(mux.pump())
        group.create_task(project(client, channels))
