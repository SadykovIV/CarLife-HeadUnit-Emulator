"""Portable operator-selected USB entry; no Android commands or recovery claims."""
from dataclasses import asdict, dataclass
from pathlib import Path
import hashlib
import json
import time

from .native_usb import NativeUSBTransportFactory
from .usb import LibUSB, RecoveryBlocked, USBIdentity


@dataclass(frozen=True)
class OperatorUSBAdmission:
    identity: USBIdentity
    transition_evidence: dict
    scope: str = "PUBLIC_HU_EXPLICIT_DEVICE_SELECTION"
    recovery_kind: str = "OPERATOR_MANAGED_RECOVERY_NOT_VERIFIED"
    current_session_owner: str = "CarLife HU"

    def require(self, identity):
        if (not identity.ports or identity.physical_key != self.identity.physical_key or
                not identity.serial_sha256 or identity.serial_sha256 != self.identity.serial_sha256):
            raise RecoveryBlocked("USB identity differs from the explicitly selected device")
        path = Path(self.transition_evidence["path"])
        if hashlib.sha256(path.read_bytes()).hexdigest() != self.transition_evidence["sha256"]:
            raise RecoveryBlocked("USB selection record changed during this session")


def select_port(devices, physical_key):
    matches = [item for item in devices if item["physical_key"] == physical_key]
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one device at {physical_key}; found {len(matches)}")
    return matches[0]


def prepare_usb(physical_key, output, *, aoa_start=False, usb_factory=LibUSB, expected_identity=None):
    """Read the selected serial, then create a factory; no AOA request/claim here."""
    output = Path(output)
    with usb_factory() as usb:
        selected = select_port(usb.discover(), physical_key)
        identity = USBIdentity(**{**selected["identity"], "ports": tuple(selected["identity"]["ports"])})
        if expected_identity is not None:
            _require_selected_enumeration(identity, expected_identity)
        with usb.open_exact(identity) as handle:
            if expected_identity is not None:
                opened = USBIdentity(**{**handle.descriptor["identity"],
                                       "ports": tuple(handle.descriptor["identity"]["ports"])})
                _require_selected_enumeration(opened, expected_identity)
            serial_hash = handle.serial_sha256()
            if expected_identity is not None and expected_identity.serial_sha256 and serial_hash != expected_identity.serial_sha256:
                raise RecoveryBlocked("Selected USB device serial changed; refresh and select it again")
    identity = USBIdentity(**{**asdict(identity), "serial_sha256": serial_hash})
    output.mkdir(parents=True, exist_ok=True)
    path = output / "usb-selection.json"
    record = {"identity": asdict(identity), "selected_unix": time.time(),
              "operator_requested_aoa_start": aoa_start, "automatic_recovery_verified": False,
              "adb_data_plane": False}
    # A reused output must never silently grant another initial AOA request.
    with path.open("x") as stream:
        json.dump(record, stream, indent=2)
        stream.write("\n")
    admission = OperatorUSBAdmission(identity, {"path": str(path.resolve()),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    return NativeUSBTransportFactory(identity, admission, output,
                                     allow_aoa_start=aoa_start, usb_factory=usb_factory)


def _require_selected_enumeration(actual, expected):
    """Reject a stale desktop picker selection before reading serial or AOA."""
    names = ("bus", "ports", "vid", "pid", "address")
    if any(getattr(actual, name) != getattr(expected, name) for name in names):
        raise RecoveryBlocked("Selected USB device changed since discovery; refresh and select it again")
