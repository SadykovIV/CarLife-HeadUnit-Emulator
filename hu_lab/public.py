"""Portable operator-selected USB entry; no Android commands or recovery claims."""
from dataclasses import asdict, dataclass
from pathlib import Path
import hashlib
import json
import time

from .native_usb import ACCESSORY_IDS, NativeUSBTransportFactory, _require_active
from .usb import (LibUSB, RecoveryBlocked, USBEnumerationChanged, USBIdentity,
                  acquisition_stage, open_for_acquisition)


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


def prepare_usb(physical_key, output, *, aoa_start=False, usb_factory=LibUSB, expected_identity=None,
                on_claim_wait=None, cancel=None):
    """Read the selected serial, then create a factory; no AOA request/claim here."""
    output = Path(output)
    with acquisition_stage('preflight_discovery', expected_identity):
        _require_active(cancel)
        context = usb_factory()
    with context as usb:
        with acquisition_stage('preflight_discovery', expected_identity):
            devices = usb.discover()
        with acquisition_stage('preflight_selection', expected_identity):
            selected = select_port(devices, physical_key)
            identity = USBIdentity(**{**selected["identity"], "ports": tuple(selected["identity"]["ports"])})
            picker = expected_identity or identity
            # A previous connection verified this accessory's serial. Its
            # address may have changed since the picker enumerated the port.
            # Only read the serial at the new address until it matches.
            known_accessory = bool(picker.serial_sha256 and (picker.vid, picker.pid) in ACCESSORY_IDS)
            _require_selected_enumeration(identity, picker, allow_new_address=known_accessory)
        stage = ("preflight_accessory_open" if (picker.vid, picker.pid) in ACCESSORY_IDS
                 else "preflight_normal_open")
        with open_for_acquisition(usb, identity, selected_identity=picker, stage=stage) as handle:
            opened = USBIdentity(**{**handle.descriptor["identity"],
                                   "ports": tuple(handle.descriptor["identity"]["ports"])})
            with acquisition_stage(stage, picker, attempted_identity=opened):
                _require_selected_enumeration(opened, identity)
                serial_hash = handle.serial_sha256()
                if picker.serial_sha256 and serial_hash != picker.serial_sha256:
                    raise USBEnumerationChanged("Selected USB device serial changed; refresh and select it again", opened)
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
                                     allow_aoa_start=aoa_start, usb_factory=usb_factory,
                                     on_claim_wait=on_claim_wait, cancel=cancel)


def _require_selected_enumeration(actual, expected, *, allow_new_address=False):
    names = ("bus", "ports", "vid", "pid")
    if (any(getattr(actual, name) != getattr(expected, name) for name in names) or
            (actual.address != expected.address and not allow_new_address)):
        raise USBEnumerationChanged("Selected USB device changed since discovery; refresh and select it again", actual)
