"""Desktop CarLife connections: explicit stream endpoint or selected accessory.

USB discovery reads cached descriptors. Default connection claims interface 0
of an already enumerated AOA accessory. An explicit aoa_start option enables
the existing addressed AOA transition with serial verification and no replay.
There is no driver detach, device reset, ADB, firmware, or Android configuration.
"""
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import sys
import threading
from functools import partial
import uuid

from .receiver import ACCESSORIES, MODES, ReceiverClient
from .client import HUClient
from .cluster import DesktopClusterSession, MEDIA_INFO, MEDIA_PROGRESS_BAR, MODULE_STATUS, NAVI_NEXT_TURN_INFO, SUBSCRIBE_DONE
from .honda_reference import HondaReferenceSession
from .honda_reference_client import HondaReferenceClient
from .modern import AuthError, ModernSession, VERSIONS, load_oem_registration
from .native_usb import (ACCESSORY_IDS, USBStream, _require_active, acquire_usb_port,
                         claim_accessory_when_available, wait_for_port_lock)
from .protocol import (HUSession, Packet, ProtocolError, TOUCH, HARD_KEY, protobuf)
from .touch import profile_name
from .usb import LibUSB, USBError, USBIdentity, acquisition_stage, open_for_acquisition, select_exact, select_bulk_pair
from .platform_support import bundled_libusb, open_owner_lock, usb_lock_directory


class DesktopConnectionError(RuntimeError):
    """An actionable connection problem which the desktop UI can display."""


def _desktop_usb_factory():
    return LibUSB(bundled_libusb())


def _identity(value):
    if isinstance(value, USBIdentity):
        return value
    if isinstance(value, dict):
        return USBIdentity(**{**value, "ports": tuple(value.get("ports", ()))})
    raise ValueError("Select one USB device from the refreshed device list")


def _require_physical_identity(identity):
    if not identity.ports or not all(type(p) is int and 1 <= p <= 255 for p in identity.ports):
        raise DesktopConnectionError("The USB device has no usable physical port path; reconnect it and refresh")
    if type(identity.bus) is not int or not 0 <= identity.bus <= 255:
        raise DesktopConnectionError("The USB device has an invalid bus identifier")
    if type(identity.address) is not int or not 1 <= identity.address <= 255:
        raise DesktopConnectionError("The selected USB enumeration has no device address; refresh the device list")
    if any(type(value) is not int or not 0 <= value <= 65535 for value in (identity.vid, identity.pid)):
        raise DesktopConnectionError("The selected USB device has an invalid vendor/product identifier")


def _require_accessory_identity(identity):
    _require_physical_identity(identity)
    if (identity.vid, identity.pid) not in ACCESSORY_IDS:
        raise DesktopConnectionError(
            "This device is not in Android accessory mode (18d1:2d00 or 18d1:2d01). "
            "Start its CarLife accessory service and refresh, or explicitly enable AOA start. "
            "This app does not switch USB modes automatically.")


def _validate_descriptor(descriptor, identity):
    actual = _identity(descriptor["identity"])
    _require_accessory_identity(actual)
    if (actual.bus, actual.ports, actual.vid, actual.pid, actual.address) != (
            identity.bus, identity.ports, identity.vid, identity.pid, identity.address):
        raise DesktopConnectionError("The selected USB device changed after discovery; refresh and select it again")
    if descriptor.get("configuration") != 1:
        raise DesktopConnectionError("The accessory is not in configuration 1; automatic USB reconfiguration is unsupported")
    try:
        select_bulk_pair(descriptor)
    except ValueError as error:
        raise DesktopConnectionError(str(error)) from error


def list_usb_devices(usb_factory=None):
    """Return cached USB descriptors annotated for the connection picker.

    The factory is injectable for host-only tests. Nothing is opened or claimed.
    Non-accessories are retained with an explanatory unsupported reason.
    """
    usb = (usb_factory or _desktop_usb_factory)()
    try:
        result = []
        for descriptor in usb.discover():
            item = dict(descriptor)
            identity = _identity(item["identity"])
            reason = None
            try:
                _require_accessory_identity(identity)
                _validate_descriptor(item, identity)
            except DesktopConnectionError as error:
                reason = str(error)
            item.update(desktop_supported=reason is None, desktop_reason=reason,
                        label=f"{identity.vid:04x}:{identity.pid:04x} · port {identity.physical_key} · address {identity.address}")
            result.append(item)
        return sorted(result, key=lambda item: (not item["desktop_supported"], item["label"]))
    finally:
        usb.close()


@dataclass(frozen=True)
class _AccessoryClaimAdmission:
    """Narrow claim authorization; this is not admission for an AOA switch."""
    identity: USBIdentity

    def require(self, identity):
        _require_accessory_identity(identity)
        if identity != self.identity:
            raise DesktopConnectionError("USB claim identity no longer matches the selected accessory")


class AccessoryTransportFactory:
    """Acquire only the exact selected accessory, sharing the existing HU lock."""
    def __init__(self, identity, *, usb_factory=None, lock_directory=None, on_claim_wait=None, cancel=None):
        self.identity = _identity(identity)
        _require_accessory_identity(self.identity)
        self.usb_factory = usb_factory or _desktop_usb_factory
        self.lock_directory = Path(lock_directory) if lock_directory is not None else usb_lock_directory()
        self.cancel = cancel if cancel is not None else threading.Event()
        self.on_claim_wait = on_claim_wait

    def __call__(self):
        _require_active(self.cancel)
        identity = self.identity
        key = identity.physical_key.replace(":", "-")
        owner = None
        usb = handle = None
        try:
            owner = open_owner_lock(self.lock_directory / f"carlife-usb-{key}.lock")
            with acquisition_stage("usb_port_lock", identity):
                try:
                    wait_for_port_lock(owner, self.cancel, self.on_claim_wait)
                except BlockingIOError as error:
                    raise DesktopConnectionError("Another CarLife session already owns this USB port; disconnect it first") from error
            usb = self.usb_factory()
            try:
                selected = select_exact(usb.discover(), identity)
            except ValueError as error:
                raise DesktopConnectionError("The selected accessory is unavailable; refresh the USB device list") from error
            _validate_descriptor(selected, identity)
            handle = open_for_acquisition(usb, identity, selected_identity=identity, stage="native_accessory_open")
            # Revalidate the second enumeration made by open_exact before claim.
            _validate_descriptor(handle.descriptor, identity)
            with acquisition_stage("accessory_claim", identity, attempted_identity=identity):
                claim_accessory_when_available(handle, _AccessoryClaimAdmission(identity), self.cancel, self.on_claim_wait)
                _require_active(self.cancel)
            owner.seek(0)
            owner.truncate()
            owner.write(json.dumps({"pid": os.getpid(), "physical_key": identity.physical_key,
                                    "owner": "CarLife HU", "scope": "DESKTOP_EXISTING_ACCESSORY"}))
            owner.flush()
            evidence = {"backend": "DIRECT_USB_AOA", "identity": asdict(identity),
                        "bulk_pair": asdict(handle.pair), "entry": "ALREADY_ACCESSORY",
                        "mode_switch_requested": False, "adb_data_plane": False}
            if getattr(handle, 'windows_selection', None) is not None:
                evidence['windows_selection'] = handle.windows_selection
            return USBStream(usb, handle, owner, evidence)
        except BaseException:
            try:
                if handle is not None:
                    handle.close()
            finally:
                try:
                    if usb is not None:
                        usb.close()
                finally:
                    if owner is not None:
                        owner.close()
            raise


class ExplicitAOATransportFactory:
    """Lazy, explicit operator transition using the existing no-replay adapter.

    Constructing a client does not open USB. prepare_usb binds admission to the
    chosen enumeration and actual serial, and NativeUSBTransportFactory records
    its single AOA START intent before sending any mode-changing request.
    """
    def __init__(self, identity, output_dir, *, usb_factory=None, on_claim_wait=None, cancel=None, strings=None):
        self.identity = _identity(identity)
        _require_physical_identity(self.identity)
        self.output_dir = Path(output_dir)
        self.usb_factory = usb_factory or _desktop_usb_factory
        self.prepared = None
        self.strings = strings
        self.cancel = cancel if cancel is not None else threading.Event()
        self.on_claim_wait = on_claim_wait

    def __call__(self):
        # On Windows even reading the serial can require opening WinUSB.
        # Serialize preflight, native open and streaming under the same lock.
        owner = acquire_usb_port(self.identity, self.cancel, self.on_claim_wait)
        try:
            if self.prepared is None:
                from .public import prepare_usb
                self.prepared = prepare_usb(self.identity.physical_key, self.output_dir,
                    aoa_start=True, usb_factory=self.usb_factory, expected_identity=self.identity,
                    on_claim_wait=self.on_claim_wait, cancel=self.cancel,
                    **({"strings": self.strings} if self.strings is not None else {}))
        except BaseException:
            owner.close()
            raise
        return self.prepared(acquired_port=owner)


@dataclass(frozen=True)
class DesktopConfig:
    transport: str = "tcp"
    profile: str = "v1"
    host: str = "127.0.0.1"
    port: int = 7240
    usb_identity: USBIdentity | None = None
    width: int = 1280
    height: int = 720
    fps: int = 30
    touch_profile: str = "legacy-single"
    audio_capture: bool = False
    trace_mode: str = "sampled"
    # Normal desktop connections negotiate the mode offered by the MD. Tests
    # that require exact dimensions can still opt into the strict lab policy.
    allow_mode_fallback: bool = True
    aoa_start: bool = False
    oem_channel: str = ""
    cuid: str = ""
    night_mode: bool = False
    require_encryption: bool = False
    registration_file: str = ""

    prefix_length: int = 24

    def validate(self):
        family = self.profile if self.profile in ACCESSORIES or self.profile == "hicar" else "carlife"
        if self.transport not in MODES[family] or self.transport == "demo":
            raise ValueError("Supported connections are native Wi-Fi, a TCP development stream, or USB")
        if type(self.aoa_start) is not bool or (self.aoa_start and self.transport != "usb"):
            raise ValueError("AOA start is an explicit USB-only option")
        if self.profile not in ("v1", "v2", "honda-v2", *VERSIONS, *ACCESSORIES, "hicar"):
            raise ValueError("Unknown CarLife protocol profile")
        if self.profile in VERSIONS and any(not isinstance(s, str) or len(s) > 128 or
                (s and not s.isprintable()) for s in (self.oem_channel, self.cuid)):
            raise ValueError("Канал и CUID CarLife должны содержать до 128 печатных символов")
        if any(type(v) is not int for v in (self.width, self.height, self.fps)) or not (
                64 <= self.width <= 4096 and 64 <= self.height <= 4096 and 1 <= self.fps <= 60):
            raise ValueError("Projection mode must be 64..4096 pixels and 1..60 FPS")
        if self.trace_mode not in ("sampled", "full"):
            raise ValueError("Unknown trace mode")
        profile_name(self.touch_profile)
        if family != "carlife" and self.transport in ("wifi", "ncm"):
            from ipaddress import IPv4Address
            addr = IPv4Address(self.host)
            if addr.is_multicast or addr.is_unspecified or not 1 <= self.prefix_length <= 32:
                raise ValueError("Выберите локальный IPv4-адрес сетевого интерфейса")
        if self.transport in ("tcp", "wifi", "ncm"):
            if not isinstance(self.host, str) or not self.host.strip() or any(c.isspace() for c in self.host):
                raise ValueError("Enter a TCP hostname or IP address, without a URL scheme or spaces")
            if "://" in self.host or "/" in self.host or "\x00" in self.host:
                raise ValueError("Enter a TCP hostname or IP address, not a URL")
            if type(self.port) is not int or not 1 <= self.port <= 65535:
                raise ValueError("TCP port must be 1..65535")
        else:
            validate_identity = _require_physical_identity if self.aoa_start else _require_accessory_identity
            validate_identity(_identity(self.usb_identity))
        return self


class DesktopV1Session(DesktopClusterSession, HUSession):
    pass


class DesktopV2Session(DesktopClusterSession, HondaReferenceSession):
    """V2 test profile with current metadata and requested video frame rate.

    This extends the historical Honda fixture, rather than claiming exact Honda
    hardware behavior or cryptographic authentication. No session is patched.
    """
    profile = "DESKTOP_V2_CURRENT_SERVICES"

    def __init__(self, width=1280, height=720, fps=30, **kwargs):
        if type(fps) is not int or not 1 <= fps <= 60:
            raise ProtocolError("desktop V2 FPS must be 1..60")
        super().__init__(width, height, fps, subscribe_ids=(0, 1, 2), **kwargs)
        self.wire_fps = fps
        self.requested.update(wire_fps=fps, provenance="explicit desktop V2 test request")

# CarLife key enum from the pinned fgrcwp/apollo ServiceTypes.kt, not Android
# KeyEvent codes. MEDIA_STOP is pause for HeadSwap's AA bridge; other MDs may stop.
HARD_KEYS = {"home": 0x01, "settings": 0x08, "media": 0x09, "navigation": 0x0B,
             "back": 0x0E, "previous": 0x0F, "next": 0x10, "volume_down": 0x11,
             "volume_up": 0x12, "mute": 0x13, "ok": 0x14, "left": 0x15,
             "right": 0x16, "up": 0x17, "down": 0x18, "main": 0x1E,
             "play": 0x1F, "stop": 0x20, "voice": 0x21}


class _DesktopControls:
    def hard_key(self, name):
        if name not in HARD_KEYS:
            raise ProtocolError(f"Unsupported CarLife hard key: {name}")
        with self.state_lock:
            if self.session.state != "STREAMING" or self.stopped or self.halt.is_set():
                raise ProtocolError("CarLife controls require a live streaming session")
            self.cancel_touch("hard_key_" + name)
            self.send(Packet(TOUCH, HARD_KEY, protobuf(f1=HARD_KEYS[name])))
            self.input_packets += 1
            self.trace.event("hard_key", key=name, carlife_code=HARD_KEYS[name])

    def home(self):
        self.hard_key("home")

    def back(self):
        self.hard_key("back")


class DesktopClient(_DesktopControls, HUClient):
    pass


class DesktopV2Client(_DesktopControls, HondaReferenceClient):
    pass


class AuthenticatedClient(DesktopV2Client):
    pre_version_status_packets = 0

    def _heartbeat_due(self):
        return self.session.state in ('WAIT_AUTH', 'WAIT_INIT', 'STREAMING', 'PAUSED')

    def _record_wire(self, direction, packet, wire=None):
        # MD_INFO may contain an OEM token. Keep packet boundaries in the event
        # log but omit the raw byte streams for the whole authenticated session.
        self.trace.packet(direction, packet)

    def _check_native_handshake_deadline(self):
        self.session.check_deadline()
        try:
            super()._check_native_handshake_deadline()
        except ProtocolError as error:
            if self.session.state == 'WAIT_VERSION' and self.pre_version_status_packets:
                raise ProtocolError('Телефон присылает статус прежнего USB-сеанса. Для смены версии в HSD '
                                    'остановите подключение на телефоне, нажмите «Подключить» и повторите на ГУ.') from error
            raise

    def send(self, packet):
        with self.state_lock:
            super().send(self.session.crypto.transform(packet, outbound=True))

    def _receive_packet(self, packet, video, received_ns=None):
        with self.state_lock:
            try:
                if (self.backend == 'DIRECT_USB_AOA' and self.session.state == 'WAIT_VERSION' and
                        packet.message in (MODULE_STATUS, NAVI_NEXT_TURN_INFO, MEDIA_INFO, MEDIA_PROGRESS_BAR, SUBSCRIBE_DONE)):
                    # HSD can leave the last session's module/cluster notifications
                    # beside its final audio packets on a still attached accessory.
                    self._check_native_handshake_deadline()
                    self.pre_version_status_packets += 1
                    if self.pre_version_status_packets > 64:
                        raise ProtocolError('USB: слишком много сообщений до согласования версии CarLife')
                    return
                packet = self.session.crypto.transform(packet, outbound=False)
                return super()._receive_packet(packet, video, received_ns)
            except AuthError as error:
                self.session.state = 'BLOCKED_AUTH'
                self.session.failure_reason = f'{type(error).__name__}: {error}'
                if error.reply is not None:
                    # The reader closes the transport on failure. Write the
                    # negative verdict before handing the exception to it.
                    try:
                        self._write_packet(self.session.crypto.transform(error.reply, outbound=True))
                    except (OSError, USBError, ProtocolError) as delivery_error:
                        # Some phones close immediately after their verdict.
                        self.trace.event('auth_reply_failed', message=error.reply.message,
                                         error_type=type(delivery_error).__name__)
                raise

    def set_night_mode(self, night):
        with self.state_lock:
            self.session.theme = int(night)
            if self.session.state in ('STREAMING', 'PAUSED'):
                for command in self.session.theme_commands():
                    self.send(command)

    def summary(self):
        result = super().summary()
        result['carlife_negotiation'] = self.session.status()
        result['carlife_negotiation']['previous_usb_status_packets'] = self.pre_version_status_packets
        return result


def create_client(config, output_dir, audio_sink=None, on_decoded_frame=None, on_frame_available=None, *,
                  on_claim_wait=None, cancel=None):
    """Construct (do not connect) a normal HUClient for the desktop worker.

    audio_sink.submit(packet) must be bounded/nonblocking; playback itself runs
    on its owning Qt thread. Changing resolution/profile requires reconnection.
    Wi-Fi uses native six-socket CarLife; TCP retains the development stream.
    """
    config.validate()
    usb = config.transport == "usb"
    if config.profile in (*ACCESSORIES, "hicar"):
        factory = None
        if usb:
            factory = (ExplicitAOATransportFactory(config.usb_identity, output_dir, strings=ACCESSORIES[config.profile],
                        on_claim_wait=on_claim_wait, cancel=cancel) if config.aoa_start else
                       AccessoryTransportFactory(config.usb_identity, on_claim_wait=on_claim_wait, cancel=cancel))
        return ReceiverClient(config, output_dir, audio_sink, on_frame_available, on_decoded_frame, factory)
    v2 = config.profile in ("v2", "honda-v2")
    client_type = DesktopV2Client if v2 else DesktopClient
    session_factory = DesktopV2Session if v2 else DesktopV1Session
    modern = config.profile in VERSIONS
    if modern:
        client_type = AuthenticatedClient
        session_factory = partial(ModernSession, version=VERSIONS[config.profile], channel=config.oem_channel,
                                  cuid=config.cuid or uuid.uuid4().hex, multitouch=profile_name(config.touch_profile) != 'LEGACY_SINGLE',
                                  theme=int(config.night_mode), require_encryption=config.require_encryption,
                                  registration=load_oem_registration(config.registration_file, config.oem_channel))
    transport_factory = None
    if usb:
        if config.aoa_start:
            transport_factory = ExplicitAOATransportFactory(config.usb_identity, output_dir,
                                                            on_claim_wait=on_claim_wait, cancel=cancel)
        else:
            transport_factory = AccessoryTransportFactory(config.usb_identity, on_claim_wait=on_claim_wait,
                                                          cancel=cancel)
    elif config.transport == "wifi":
        from .wireless import TCP6Stream
        transport_factory = (lambda: TCP6Stream.connect(config.host, try_update=True)) if modern else (
            lambda: TCP6Stream.connect(config.host))
    return client_type(config.host, config.port, output_dir, config.width, config.height, config.fps,
                       backend="DIRECT_USB_AOA" if usb else "TCP6_WIFI" if config.transport == "wifi" else "TCP_STREAM",
                       transport_factory=transport_factory,
                       session_factory=session_factory,
                       touch_profile=config.touch_profile, trace_mode=config.trace_mode,
                       allow_mode_fallback=config.allow_mode_fallback,
                       audio_capture=config.audio_capture, on_decoded_frame=on_decoded_frame,
                       on_frame_available=on_frame_available,
                       on_audio_packet=audio_sink.submit if audio_sink is not None else None)
