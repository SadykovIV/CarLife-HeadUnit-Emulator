"""Huawei Nearby USB discovery, HiChain pairing, DMSDP, HiSight and UIBC."""
import asyncio
import json
import os
from pathlib import Path
import re
import secrets
import socket
import struct
import sys

import av

from ..receiver import Listener, send
from ..platform_support import lock_owner, open_owner_lock, unlock_owner
from ..receiver_discovery import Advertiser, NEARBY
from . import auth_wire, control, control_wire, dmsdp_wire, hichain, hisight, nearby_wire as nearby
from .audio_media import Audio
from .cipher import Cipher
from .hisight_input import Input
from .hisight_media import Depacketizer


def library_path():
    win = sys.platform == "win32"
    name = "hsd_hicar_auth.dll" if win else "libhsd_hicar_auth.dylib"
    here = Path(__file__).resolve().parent
    candidates = (here / name, here.parents[1] / "build/hicar" / ("windows-x64" if win else "macos-arm64") / name)
    for path in candidates:
        if path.is_file():
            return path
    raise FileNotFoundError("Отсутствует библиотека HiChain из комплекта HSD HeadUnit Emulator")


def identity(client):
    root = client.output_dir.parent.parent / "hicar-pairing"
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    owner = open_owner_lock(root / "session.lock")
    try:
        lock_owner(owner)
    except OSError as exc:
        owner.close()
        raise RuntimeError("В другом окне уже открыт сеанс HiCar") from exc
    client.resources.callback(owner.close)
    client.resources.callback(unlock_owner, owner)
    path = root / "identity"
    if not path.exists():
        temporary = root / ("identity-" + secrets.token_hex(8))
        with os.fdopen(os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w") as out:
            out.write(secrets.token_hex(32).upper())
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass
        finally:
            temporary.unlink()
    own = path.read_text(encoding="ascii")
    if not re.fullmatch(r"[A-F0-9]{64}", own):
        raise ValueError("Повреждён идентификатор сопряжения HiCar")
    runtime = hichain.Runtime._active
    if runtime is None:
        runtime = hichain.Runtime(library_path(), root, own)
    if runtime.root != root:
        raise ValueError("Хранилище HiCar уже открыто из другого каталога")
    return own, runtime


async def chunk(reader):
    data = await reader.read(8192)
    if not data:
        raise EOFError("Телефон закрыл соединение HiCar")
    return data


async def authenticate(reader, writer, own, runtime):
    pin = f"{secrets.randbelow(1_000_000):06d}"
    decoder = auth_wire.JsonStream()
    session = None
    async def reply(op, data):
        outer = dict(version="L_1.0", discover_type=1, oper_type=op,
                     data=data if op == 1 else json.dumps(data, separators=(",", ":")))
        if op == 1:
            outer.update(usb_connection=True, pin_code=pin)
        await send(writer, json.dumps(outer, separators=(",", ":")).encode())
    try:
        async with asyncio.timeout(45):
            while True:
                for msg in decoder.feed(await chunk(reader)):
                    if not isinstance(msg, dict) or msg.get("usb_connection", True) not in (True, 1):
                        raise ValueError("Недействительный проводной сеанс HiCar")
                    op = msg.get("oper_type")
                    data = msg.get("data")
                    if isinstance(data, str):
                        data = auth_wire.parse_json(data)
                    if not isinstance(data, dict):
                        raise ValueError("Неверный формат HiChain")
                    if op == 1 and session is None:
                        peer = data["deviceId"]
                        if not isinstance(peer, str) or not 1 <= len(peer.encode()) <= 64 or data.get("usb_connection") not in (True, 1):
                            raise ValueError("Неверный идентификатор телефона HiCar")
                        session = hichain.Session(runtime, own, peer, pin, package="com.huawei.hicar", service="CarDevice")
                        await reply(1, dict(deviceId=own, devicename="HSD HeadUnit Emulator", has_pin=True,
                                           usb_connection=True, CONNECT_ABILITY_KEY="1", CAR_MODE_ID="12345678"))
                    elif op == 3 and session:
                        code, packets = session.receive(json.dumps(data, separators=(",", ":")).encode())
                        for packet in packets:
                            await reply(3, json.loads(packet))
                        if code or 1 in session.results:
                            raise ValueError("HiChain отклонил сопряжение телефона")
                        if 0 in session.results:
                            if session.session_key is None or len(session.session_key) not in (16, 32):
                                raise ValueError("HiChain не подтвердил ключ сеанса")
                            return session.session_key
                    else:
                        raise ValueError("Нарушен порядок сопряжения HiCar")
    finally:
        if session:
            session.close()


def business_field(value):
    chan, at = nearby.read_uint(value)
    business, at = nearby.read_uint(value, at)
    length, at = nearby.read_uint(value, at)
    if length > 128 or at + length >= len(value):
        raise ValueError("Неверный запрос ресурса Nearby")
    proto, at = nearby.read_uint(value, at+length)
    if chan != 6 or proto != 1 or at != len(value):
        raise ValueError("Неизвестный транспорт Nearby")
    return business


async def run(client):
    own, runtime = identity(client)
    host = client.config.host
    stack = client.resources
    entry = await Listener(host).open(stack)
    advertiser = await Advertiser(NEARBY, host, client.config.prefix_length, entry.port).start()
    stack.callback(advertiser.close)
    client.stage("WAIT_PHONE", f"HiCar · ожидание телефона на USB-сети {host}")
    reader, writer = await entry.arrival
    client.peer = writer.get_extra_info("peername")[0]
    advertiser.close()
    auth = await Listener(host, client.peer).open(stack)
    data = await Listener(host, client.peer).open(stack)
    control_port = await Listener(host, client.peer).open(stack)
    negotiation = control.Negotiator(own.encode())
    decoder = nearby.Decoder()
    key_task = None
    async def pair():
        rx, tx = await auth.arrival
        return await authenticate(rx, tx, own, runtime)
    client.stage("WAIT_AUTH", "HiCar · обнаружение и сопряжение по USB")
    try:
        async with asyncio.timeout(55):
            while True:
                for frame in decoder.feed(await chunk(reader)):
                    if frame.kind == 0 and not frame.encrypted:
                        response = negotiation.respond(frame)
                        if response:
                            await send(writer, nearby.pack_frame(0, response))
                    elif frame.kind == 4 and negotiation.complete and not frame.encrypted:
                        for tag, value in nearby.read_tlvs(frame.payload):
                            biz = business_field(value)
                            if tag != 3 or biz not in (19, 20):
                                raise ValueError("Неизвестный ресурс HiCar")
                            if biz == 19 and key_task is None:
                                key_task = asyncio.create_task(pair())
                                port = auth.port
                            elif biz == 20 and key_task is not None:
                                key = await key_task
                                port = data.port
                            else:
                                raise ValueError("Нарушен порядок Nearby и HiChain")
                            response = nearby.pack_uint(0) + nearby.pack_uint(port) + value
                            await send(writer, nearby.pack_frame(4, nearby.pack_tlv(4, response)))
                            if biz == 20:
                                break
                        else:
                            continue
                        break
                    else:
                        raise ValueError("Неверное сообщение Nearby")
                else:
                    continue
                break
    finally:
        if key_task:
            key_task.cancel()
            await asyncio.gather(key_task, return_exceptions=True)
    client.stage("WAIT_VIDEO", "HiCar · согласование дисплея и звука")
    async with asyncio.timeout(15):
        entry_rx, entry_tx = await data.arrival
        parser = dmsdp_wire.Decoder()
        handshake = dmsdp_wire.Negotiation(control_port.port)
        while not handshake.complete:
            for packet in parser.feed(await chunk(entry_rx)):
                await send(entry_tx, handshake.respond(packet))
        ctl, tx = await control_port.arrival
    await media_session(client, own, key, ctl, tx)


async def media_session(client, own, key, rx, tx):
    cfg = client.config
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    client.resources.callback(sock.close)
    sock.bind((cfg.host, 0))
    sock.setblocking(False)
    info = dict(deviceId=own, deviceName="HSD HeadUnit Emulator", deviceType=4, btName="HSD", localIp=cfg.host,
                remoteIp=client.peer, port=0, properties={"2003":"12345678"})
    session = control_wire.Session(key, info, True, (cfg.width, cfg.height))
    client.resources.callback(session.close)
    session.speaker.ports = (sock.getsockname()[1], -1)
    await send(tx, session.initial())
    async def heartbeat():
        while True:
            await asyncio.sleep(1)
            if session.negotiated:
                await send(tx, session.heartbeat())
    async with asyncio.TaskGroup() as group:
        group.create_task(heartbeat())
        group.create_task(audio_stream(client, key, sock, session.speaker))
        opened = False
        while True:
            events = session.receive(await asyncio.wait_for(chunk(rx), 20))
            for reply in session.take_pending():
                await send(tx, reply)
            if any(event.get("hisight_endpoint") for event in events):
                if opened:
                    raise ValueError("Повторное открытие HiSight")
                opened = True
                group.create_task(video_control(client, key, session.media_port))


async def open_channel(client, port):
    rx, tx = await asyncio.wait_for(asyncio.open_connection(client.peer, port, local_addr=(client.config.host, 0)), 8)
    client.resources.callback(tx.close)
    return rx, tx


async def video_control(client, key, port):
    cfg = client.config
    session = hisight.Session(key, cfg.width, cfg.height, cfg.fps)
    client.resources.callback(session.close)
    rx, tx = await open_channel(client, port)
    await send(tx, session.initial())
    opened = False
    async with asyncio.TaskGroup() as group:
        while True:
            session.receive(await asyncio.wait_for(chunk(rx), 20))
            if session.media_endpoint and not opened:
                opened = True
                media_rx, _ = await open_channel(client, session.media_endpoint)
                if session.input_endpoint:
                    _, input_tx = await open_channel(client, session.input_endpoint)
                    encoder = Input(key, cfg.width, cfg.height)
                    client.resources.callback(encoder.cipher.close)
                    async def input_events(events):
                        for event in events:
                            if event[0] == "key":
                                await send(input_tx, encoder.key(event[1], True) + encoder.key(event[1], False))
                            else:
                                action, _, x, y = event
                                await send(input_tx, encoder.touch(min(cfg.width-1, x*cfg.width//client.session.width),
                                    min(cfg.height-1, y*cfg.height//client.session.height), action!="up", action=="move"))
                    client.input_handler = input_events
                group.create_task(video_stream(client, key, media_rx))
                session.request("PLAY", {"Transport": "RTP/AVP/UDP;unicast;client_port=0"})
            for reply in session.pending:
                await send(tx, reply)
            session.pending.clear()
            if session.peer_teardown:
                raise EOFError("Телефон завершил HiSight")


async def video_stream(client, key, rx):
    decoder = Depacketizer(key)
    client.resources.callback(decoder.cipher.close)
    while True:
        size, = struct.unpack(">I", await rx.readexactly(4))
        if not 12 <= size <= 2*1024*1024:
            raise ValueError("Недопустимый размер пакета HiSight")
        unit = decoder.receive(await asyncio.wait_for(rx.readexactly(size), 8))
        if unit:
            client.video(unit[2])


async def audio_stream(client, key, sock, speaker):
    cipher = Cipher(key, 65536)
    client.resources.callback(cipher.close)
    parser = Audio()
    codec = av.CodecContext.create("aac", "r")
    resampler = None
    parameters = None
    while True:
        data, peer = await asyncio.get_running_loop().sock_recvfrom(sock, 65536)
        if peer != (client.peer, speaker.remote) or not speaker.ready:
            continue
        if len(data) < 20 or data[0] != 171 or int.from_bytes(data[1:3], "big") != len(data)-3:
            raise ValueError("Неверный пакет звука HiCar")
        for record in parser.receive(cipher.decrypt(data[3:])):
            for frame in codec.decode(av.Packet(record["data"])):
                fmt = (frame.sample_rate, len(frame.layout.channels))
                if parameters != fmt:
                    resampler = av.AudioResampler(format="s16", layout=frame.layout.name, rate=frame.sample_rate)
                    parameters = fmt
                for pcm in resampler.resample(frame):
                    client.pcm(bytes(pcm.planes[0])[:pcm.samples*fmt[1]*2], *fmt)
