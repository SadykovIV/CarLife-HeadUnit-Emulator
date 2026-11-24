"""WeDrive WeLink A1 accessory receiver."""
import asyncio
import json
import struct

from . import __version__
from .accessory_records import BulkRecords
from .protocol import MEDIA, TTS

HEADER = struct.Struct(">2sBBI")
AUDIO = struct.Struct("<HHBBHII")
KEYS = dict(home=3, back=4, previous=88, next=87, play=126, stop=86)


async def run(client):
    bulk = BulkRecords(client.sock)
    async def write(kind, body):
        await bulk.write(HEADER.pack(b"WL", kind, 0, len(body)), body)

    async def command(method, data, module="WeLink_SDK"):
        msg = dict(moduleName=module, version=0, platform="linux", command=dict(method=method, extData=data))
        await write(9, json.dumps(msg, separators=(",", ":")).encode())

    async def handshake():
        await command("onHuReady", dict(width=client.config.width, height=client.config.height))
        await command("onHuReady", dict(codecMode=2))
        await command("requestWeLink", dict(vers=dict(muVersion="", huVersion=__version__), isBtAutoConnect=False,
                                           isSupportRecord=False, isHuSupportCall=False, recordDefault=0, linkMethod=3))

    async def input_events(events):
        touch = False
        for event in events:
            if event[0] == "key":
                await write(8, f"keyevent {KEYS[event[1]]}".encode())
            else:
                action, slot, x, y = event
                x = min(client.config.width-1, x * client.config.width // client.session.width)
                y = min(client.config.height-1, y * client.config.height // client.session.height)
                await write(8, f"motionevent3 {x} {y} {slot} {action}".encode())
                touch = True
        if touch:
            await write(8, b"motionevent3 comm")

    client.stage("WAIT_VERSION", "WeLink · согласование с телефоном")
    client.input_handler = input_events
    ready = False
    async def heartbeat():
        attempts = 0
        while True:
            if ready:
                await command("reqHeartbeat", dict(heartbeatType=1))
            else:
                if attempts == 5:
                    raise TimeoutError("Телефон не подтвердил WeLink")
                await handshake()
                attempts += 1
            await asyncio.sleep(2)

    async def receive():
        nonlocal ready
        while True:
            async with asyncio.timeout(15):
                magic, kind, ext, size = HEADER.unpack(await bulk.read(8))
                if magic != b"WL" or ext > size or size > 2*1024*1024:
                    raise ValueError("Неверный заголовок WeLink")
                body = (await bulk.read(size))[ext:]
            if kind == 107:
                raise ValueError("Телефон запросил профиль WeLink TLS. Поддерживается открытый профиль A1.")
            if kind == 9:
                msg = json.loads(body)
                cmd = msg["command"]
                method, data = cmd["method"], cmd.get("extData", {})
                if msg.get("moduleName") == "WeLink_SDK" and method == "responseWeLink" and not ready:
                    if (data.get("state"), data.get("errCode"), data.get("responseCode")) != (1, 0, 0) or data.get("codecMode") not in (2, 3):
                        raise ValueError("Телефон отклонил параметры WeLink AVC")
                    await write(8, f"config {client.config.fps}".encode())
                    await command("onHuSourceChange", {"status": "1"})
                    ready = True
                    client.stage("WAIT_VIDEO", "WeLink · ожидание первого кадра")
                elif ready and msg.get("moduleName") == "WeLink" and method == "onHuSendPcm":
                    state = data.get("pcmState")
                    if type(state) is int and 1 <= state <= 6:
                        await command("onMuSendPcm", dict(pcmState=state, isSuccess=1), "WeLink")
            elif kind == 6 and ready:
                client.video(body)
            elif kind == 17 and ready:
                magic, cmd, src, _, _, _, size = AUDIO.unpack_from(body)
                play, series, channels, bits, rate = struct.unpack_from("<BBBBI", body, 16)
                pcm = body[24:]
                if (magic != 0x55aa or cmd != 10 or src > 5 or size != len(body)-16 or play or series > 2
                        or channels not in (1, 2) or bits != 16 or not 8000 <= rate <= 96000 or len(pcm) % (channels*2)):
                    raise ValueError("Неверные параметры WeLink PCM")
                client.pcm(pcm, rate, channels, TTS if src == 0 else MEDIA)
                if series in (1, 2):
                    await write(18, AUDIO.pack(0x55aa, 40010 if series == 1 else 40020, src, 0, 0, 0, 0))

    async with asyncio.TaskGroup() as group:
        group.create_task(heartbeat())
        group.create_task(receive())
