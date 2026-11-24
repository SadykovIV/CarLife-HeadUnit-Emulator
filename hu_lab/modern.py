"""CarLife HU negotiation used by the Baidu vehicle SDK. See docs/CARLIFE_AUTH.md."""
import base64
import hashlib
import hmac
import json
from pathlib import Path
import platform
import secrets
import time

from cryptography.hazmat.primitives import padding, serialization
from cryptography.hazmat.primitives.asymmetric import padding as rsa_padding, rsa
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from . import __version__
from .cluster import DesktopClusterSession, REQUEST_IDS, SUBSCRIBE, SUBSCRIBE_DONE
from .honda_reference import (CAR_DATA_SUBSCRIBE, CAR_DATA_DONE, MD_AUTHEN_RESULT, MD_FEATURE_REQUEST,
                              HU_FEATURE_RESPONSE, STATISTIC_INFO, list_body, repeated_entries)
from .protocol import (AUTH_IDS, CMD, VIDEO_HEARTBEAT, HU_INFO, HU_VERSION, MD_INFO, VERSION_MATCH,
                       VIDEO_INIT, VIDEO_INIT_DONE, VIDEO_RESET, HUSession, Packet, ProtocolError, fields, protobuf)

VERSIONS = {f'carlife-{version}': tuple(map(int, version.split('.'))) for version in ('2.1', '3.0', '3.1', '4.0', '4.1')}
HU_CHALLENGE, MD_PROOF, HU_VERDICT, HU_ACK = 0x18048, 0x10049, 0x1804A, 0x1804C
MD_KEY_REQUEST, HU_PUBLIC_KEY = 0x1006A, 0x1806B
MD_WRAPPED_KEY, HU_KEY_RECEIVED = 0x1006C, 0x1806D
MD_CIPHER_READY, HU_CIPHER_READY = 0x1006E, 0x1806F
MD_SUBSCRIPTIONS, HU_SUBSCRIPTIONS_ACK = 0x10043, 0x18044
SET_THEME, GET_THEME, MD_THEME = 0x18078, 0x18079, 0x10080
HU_ACTIVE_INFO, MD_ACTIVE_DONE, HU_CERTIFICATE = 0x18080, 0x10081, 0x18083
AUTH_TIMEOUT = 30


class AuthError(ProtocolError):
    def __init__(self, message, reply=None):
        super().__init__(message)
        self.reply = reply


def load_oem_registration(path, channel):
    """Only explicitly supplied values for this HU. No embedded vendor identity."""
    if not path:
        return {}
    try:
        if not Path(path).is_file():
            raise ValueError
        with open(path, 'rb') as source:
            raw = source.read(32769)
        if len(raw) > 32768:
            raise ValueError
        record = json.loads(raw.decode('utf-8'))
        if not isinstance(record, dict) or record.keys() - {'uuid', 'vehicle_id', 'bt_address', 'certificate'}:
            raise ValueError
        if any(not isinstance(v, str) or not v or '\x00' in v or
               len(v.encode('utf-8')) > (24000 if k == 'certificate' else 256) for k, v in record.items()):
            raise ValueError
        if not (record.get('certificate') or record.get('uuid')):
            raise ValueError
        if record.get('uuid') and not (channel and record.get('vehicle_id')):
            raise ValueError
        if not record.get('uuid') and ('vehicle_id' in record or 'bt_address' in record):
            raise ValueError
    except (OSError, UnicodeError, ValueError, RecursionError):
        raise ValueError('Проверьте JSON активации OEM: сертификат или UUID с каналом и vehicle_id') from None
    return record


def verification_code(identity, nonce):
    # The SDK passes a Java String through JNI GetStringUTFChars (modified UTF-8).
    utf16 = ('CARLIFE' + ''.join(identity) + nonce).encode('utf-16-be')
    java_chars = ''.join(chr(int.from_bytes(utf16[i:i+2], 'big')) for i in range(0, len(utf16), 2))
    encoded = java_chars.encode('utf-8', 'surrogatepass').replace(b'\x00', b'\xc0\x80')
    return hashlib.md5(encoded, usedforsecurity=False).hexdigest().encode('ascii')


class ContentEncryption:
    """Per-connection RSA wrapping and AES payload encoding used by the SDK."""
    def __init__(self):
        self.state = 'idle'
        self._private = self._cipher = None

    def public_key(self):
        if self.state not in ('idle', 'offered'):
            raise AuthError('CarLife запросил повторный обмен ключами в открытом сеансе')
        if self._private is None:
            self._private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        der = self._private.public_key().public_bytes(serialization.Encoding.DER,
                                                       serialization.PublicFormat.SubjectPublicKeyInfo)
        self.state = 'offered'
        return base64.b64encode(der)

    def accept_key(self, body):
        wrapped = fields(body).get(1)
        if self.state != 'offered' or not isinstance(wrapped, bytes) or len(wrapped) > 512:
            raise AuthError('CarLife прислал ключ AES вне согласованного обмена')
        try:
            key = self._private.decrypt(base64.b64decode(wrapped, validate=True), rsa_padding.PKCS1v15())
            if len(key) not in (16, 24, 32):
                raise ValueError('invalid key length')
            self._cipher = Cipher(algorithms.AES(key), modes.ECB())
        except ValueError:
            raise AuthError('CarLife не удалось согласовать ключ AES') from None
        self._private = None
        self.state = 'key_received'

    def activate(self):
        if self.state != 'key_received':
            raise AuthError('CarLife запросил шифрование до получения ключа AES')
        self.state = 'active'

    def transform(self, packet, *, outbound):
        if self.state != 'active' or packet.message == HU_VERSION or not packet.payload:
            return packet
        if outbound:
            pad = padding.PKCS7(128).padder()
            codec = self._cipher.encryptor()
            data = codec.update(pad.update(packet.payload) + pad.finalize()) + codec.finalize()
        else:
            try:
                codec = self._cipher.decryptor()
                unpad = padding.PKCS7(128).unpadder()
                data = unpad.update(codec.update(packet.payload) + codec.finalize()) + unpad.finalize()
            except ValueError:
                raise AuthError('CarLife прислал некорректный зашифрованный пакет') from None
        return Packet(packet.channel, packet.message, data, packet.timestamp)


class ModernSession(DesktopClusterSession, HUSession):
    def __init__(self, *args, version, channel, cuid, multitouch, theme, require_encryption=False,
                 registration=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.protocol_major, self.minor = version
        self.profile = f'CarLife {self.protocol_major}.{self.minor}'
        self.channel, self.cuid = channel, cuid
        self.multitouch, self.theme = multitouch, theme
        self.peer_version = self.peer_theme = None
        self.identity = self.nonce = None
        self.phone_verified = self.hu_accepted = None
        self.failure_reason = None
        self.crypto = ContentEncryption()
        self.require_encryption = require_encryption
        self.registration = registration or {}
        self.activation = 'pending' if 'uuid' in self.registration else 'unused'
        self.deadline = None

    def start(self):
        if self.state != 'NEW':
            raise ProtocolError('duplicate local start')
        self.state = 'WAIT_VERSION'
        return [Packet(CMD, HU_VERSION, protobuf(f1=self.protocol_major, f2=self.minor))]

    def check_deadline(self):
        if self.deadline is not None and time.monotonic() >= self.deadline:
            self.state = 'BLOCKED_AUTH'
            if self.identity is None:
                stage = 'данные телефона MD_INFO'
            elif not self.phone_verified:
                stage = 'ответ телефона на запрос ГУ'
            elif not self.hu_accepted:
                stage = 'решение телефона о допуске ГУ'
            elif self.activation == 'pending':
                stage = 'решение телефона об активации OEM'
            else:
                stage = 'завершение обмена ключами'
            raise AuthError(f'Истекло время ожидания CarLife: {stage}')

    def status(self):
        return dict(requested_version=f'{self.protocol_major}.{self.minor}', md_protocol_version=self.peer_version,
                    phone_verified=self.phone_verified, hu_accepted_by_phone=self.hu_accepted,
                    content_encryption=self.crypto.state, encryption_required=self.require_encryption,
                    oem_activation=self.activation, certificate_supplied='certificate' in self.registration,
                    phone_theme=self.peer_theme)

    def theme_commands(self):
        return [Packet(CMD, SET_THEME, protobuf(f1=self.theme)), Packet(CMD, GET_THEME)]

    def _finish(self, replies):
        ready = self.crypto.state == 'active' or (self.crypto.state == 'idle' and not self.require_encryption)
        if self.state == 'WAIT_AUTH' and self.phone_verified and self.hu_accepted and ready and self.activation != 'pending':
            self.state = 'WAIT_INIT'
            self.deadline = None
            replies.append(Packet(CMD, VIDEO_INIT, protobuf(f1=self.width, f2=self.height, f3=self.fps)))
        return replies

    def _challenge(self, packet):
        info = fields(packet.payload)
        parts = [info.get(k, b'') for k in (4, 14, 20)]
        if any(not isinstance(p, bytes) or len(p) > 4096 for p in parts) or not any(parts):
            raise AuthError('В MD_INFO нет данных для проверки телефона CarLife')
        try:
            identity = tuple(p.decode('utf-8') for p in parts)
        except UnicodeError:
            raise AuthError('В MD_INFO некорректная кодировка идентификатора телефона') from None
        super().receive(packet)
        if self.identity is not None:
            if self.identity != identity:
                raise AuthError('Идентификатор телефона изменился во время сеанса CarLife')
            return []
        self.identity, self.nonce = identity, secrets.token_hex(16)
        return [Packet(CMD, HU_INFO, protobuf(f1=platform.system(), f4='HeadUnit Emulator',
                                             f14='HSD HeadUnit Emulator', f24=__version__)),
                Packet(CMD, HU_CHALLENGE, protobuf(f1='2.0;' + self.nonce))]

    def receive(self, p):
        try:
            return self._receive(p)
        except AuthError:
            self.state = 'BLOCKED_AUTH'
            raise

    def _receive(self, p):
        if self.state == 'BLOCKED_AUTH':
            raise AuthError('Сеанс CarLife завершён при авторизации')
        if p.message == VERSION_MATCH:
            match = fields(p.payload)
            if match.get(1) != 1:
                self.state = 'VERSION_REJECTED'
                raise ProtocolError(f'Телефон отклонил версию протокола {self.profile}')
            if self.state != 'WAIT_VERSION':
                self.duplicates += 1
                return []
            self.peer_version = match.get(2) if isinstance(match.get(2), int) else None
            self.state = 'WAIT_AUTH'
            self.deadline = time.monotonic() + AUTH_TIMEOUT
            replies = []
            if 'certificate' in self.registration:
                replies.append(Packet(CMD, HU_CERTIFICATE, protobuf(f1=self.registration['certificate'])))
            if self.activation == 'pending':
                replies.append(Packet(CMD, HU_ACTIVE_INFO, protobuf(f1=self.registration['uuid'], f2=self.channel,
                    f3=self.registration['vehicle_id'], f4=0, f5=self.registration.get('bt_address', ''))))
            replies.append(Packet(CMD, STATISTIC_INFO, protobuf(f1=self.cuid, f2=__version__, f3=1, f4=self.channel,
                                                               f5=0, f6=0, f7=0)))
            return replies
        if self.state in ('NEW', 'WAIT_VERSION'):
            if p.message != VIDEO_HEARTBEAT:
                raise ProtocolError('CarLife сообщение до согласования версии')
            return []
        if p.message == MD_INFO:
            return self._challenge(p)
        if p.message == MD_PROOF:
            response = fields(p.payload).get(1)
            valid = bool(self.nonce and isinstance(response, bytes) and
                         hmac.compare_digest(response, verification_code(self.identity, self.nonce)))
            self.phone_verified = valid
            verdict = Packet(CMD, HU_VERDICT, protobuf(f1=int(valid)))
            if not valid:
                raise AuthError('Ответ телефона на запрос авторизации CarLife не прошёл проверку', verdict)
            return self._finish([verdict])
        if p.message == MD_AUTHEN_RESULT:
            self.hu_accepted = fields(p.payload).get(1) == 1
            ack = Packet(CMD, HU_ACK)
            if not self.hu_accepted:
                raise AuthError('Телефон отклонил авторизацию ГУ. Проверьте канал и CUID, выданные для вашего ГУ.', ack)
            return self._finish([ack])
        if p.message == MD_FEATURE_REQUEST:
            entries = repeated_entries(p.payload, bytes) if p.payload else []
            if any(e[1] == b'CONTENT_ENCRYPTION' and e.get(2) == 1 for e in entries):
                if self.state in ('WAIT_INIT', 'STREAMING', 'PAUSED') and self.crypto.state != 'active':
                    raise AuthError('Телефон потребовал шифрование после начала согласования видео')
                self.require_encryption = True
            features = [dict(f1='CONTENT_ENCRYPTION', f2=int(self.require_encryption)),
                        dict(f1='MULTI_TOUCH', f2=int(self.multitouch))]
            return [Packet(CMD, HU_FEATURE_RESPONSE, list_body(features))]
        if p.message == MD_ACTIVE_DONE:
            if fields(p.payload).get(1) != 1:
                self.activation = 'rejected'
                raise AuthError('Телефон отклонил активацию OEM для этого ГУ')
            if self.activation == 'pending':
                self.activation = 'accepted'
            return self._finish([])
        if p.message == MD_KEY_REQUEST:
            public = self.crypto.public_key()
            if self.deadline is None:
                self.deadline = time.monotonic() + AUTH_TIMEOUT
            return [Packet(CMD, HU_PUBLIC_KEY, protobuf(f1=public))]
        if p.message == MD_WRAPPED_KEY:
            self.crypto.accept_key(p.payload)
            return [Packet(CMD, HU_KEY_RECEIVED)]
        if p.message == MD_CIPHER_READY:
            self.crypto.activate()
            if self.state != 'WAIT_AUTH':
                self.deadline = None
            return self._finish([Packet(CMD, HU_CIPHER_READY)])
        if p.message in AUTH_IDS or p.message in (0x16001, 0x16002):
            raise AuthError(f'Требуется другой способ авторизации или активации CarLife (0x{p.message:05x})')
        if p.message == CAR_DATA_SUBSCRIBE:
            if not p.payload:
                return []
            return [Packet(CMD, CAR_DATA_DONE, list_body([dict(f1=e[1], f2=0) for e in repeated_entries(p.payload)]))]
        if self.state == 'WAIT_AUTH':
            if (p.channel != CMD and p.message != VIDEO_HEARTBEAT) or p.message == VIDEO_INIT_DONE:
                raise AuthError('Телефон начал проекцию до завершения авторизации CarLife')
            return []
        if p.message == MD_THEME:
            mode = fields(p.payload).get(1)
            if mode in (0, 1, 2):
                self.peer_theme = mode
            return []
        if p.message == MD_SUBSCRIPTIONS:
            self.cluster.requested_once()
            return [Packet(CMD, SUBSCRIBE, list_body([dict(f1=i) for i in REQUEST_IDS]))]
        out = super().receive(p)
        if p.message == SUBSCRIBE_DONE:
            out.insert(0, Packet(CMD, HU_SUBSCRIPTIONS_ACK))
        if p.message == VIDEO_INIT_DONE and out:
            out.extend(self.theme_commands())
        return out

    def reset(self):
        if self.state not in ('STREAMING', 'PAUSED'):
            raise ProtocolError('reset outside established session')
        return [Packet(CMD, VIDEO_RESET)]
