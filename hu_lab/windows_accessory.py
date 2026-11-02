"""Choose an AOA data handle for the selected Windows USB enumeration.

libusb provides the device descriptors and physical selection. On Windows its
composite backend may assign an ADB path with no MI_XX suffix to slot zero.
Never use that unverified slot for accessory traffic. Bind bulk and optional
HID to the same native handle whose live interface and pipes passed inspection.
"""
import time

from .usb import RecoveryBlocked, _enumeration_record, select_bulk_pair


class WindowsSelectionFailure(RecoveryBlocked):
    def __init__(self, reason, message, evidence, *, attempted=None, winerror=None):
        super().__init__(message)
        self.selection_reason = reason
        self.windows_selection = evidence
        self.attempted_identity = attempted
        self.operation = 'select verified WinUSB accessory interface'
        self.winerror = winerror


def open_accessory(identity, descriptor, *, find=None, open_candidate=None, readiness_seconds=1.5):
    from .windows_usb_interfaces import find_winusb_candidates
    from .windows_winusb import (WinUSBCandidateMismatch, WinUSBError, WinUSBIdentityMismatch,
                                 open_verified_winusb)
    find = find or find_winusb_candidates
    open_candidate = open_candidate or open_verified_winusb
    evidence = {'api': 'WinUSB', 'target': _enumeration_record(identity),
                'expected_interface': 0, 'examined': []}
    if descriptor.get('configuration') != 1:
        raise WindowsSelectionFailure('invalid_descriptor', 'AOA configuration 1 is not active', evidence)
    try:
        pair = select_bulk_pair(descriptor)
    except ValueError as error:
        raise WindowsSelectionFailure('invalid_descriptor', str(error), evidence) from error
    evidence.update(expected_in=pair.endpoint_in, expected_out=pair.endpoint_out)

    # Windows can list the USB parent before its function interfaces appear.
    # Only poll PnP inventory; no mode switch or failed transfer is retried.
    deadline = time.monotonic() + readiness_seconds
    while True:
        try:
            candidates = find(identity, descriptor)
            if not candidates:
                raise WindowsSelectionFailure('no_winusb', 'No present WinUSB accessory function', evidence)
            break
        except Exception as error:
            why = getattr(error, 'selection_reason', None)
            if why is None:
                raise
            left = deadline - time.monotonic()
            if why not in ('no_winusb', 'adb_only') or left <= 0:
                error.windows_selection = evidence
                raise
            time.sleep(min(.15, left))

    evidence['candidates'] = len(candidates)
    last_os_error = None
    for candidate in candidates:
        item = {'role': candidate.role}
        evidence['examined'].append(item)
        if candidate.role == 'adb' or (candidate.role.startswith('mi_') and candidate.role != 'mi_00'):
            item['result'] = 'excluded_non_accessory'
            continue
        try:
            handle = open_candidate(candidate, identity, descriptor)
        except WinUSBCandidateMismatch as error:
            item['result'] = error.selection_reason
            observed = error.actual_interface
            if observed is not None:
                item['observed'] = observed
            continue
        except WinUSBIdentityMismatch as error:
            item['result'] = 'identity_mismatch'
            raise WindowsSelectionFailure('device_changed', 'USB identity changed during WinUSB verification',
                                          evidence, attempted=identity) from error
        except WinUSBError as error:
            item.update(result='winusb_error', operation=error.operation, winerror=error.winerror)
            last_os_error = error
            continue
        except Exception as error:
            # A candidate that changed while opening is not interchangeable
            # with another physical device. Keep the enumeration error intact.
            if getattr(error, 'selection_reason', None):
                error.windows_selection = evidence
            raise
        item['result'] = 'selected'
        evidence['verified'] = handle.verified_interface
        handle.windows_selection = evidence
        return handle

    if last_os_error is not None:
        raise WindowsSelectionFailure('cannot_open', str(last_os_error), evidence, attempted=identity,
                                      winerror=last_os_error.winerror) from last_os_error
    reasons = {item['result'] for item in evidence['examined']}
    if reasons <= {'excluded_non_accessory', 'adb_interface'}:
        raise WindowsSelectionFailure('adb_only', 'Windows exposes ADB instead of the CarLife interface', evidence,
                                      attempted=identity if 'adb_interface' in reasons else None)
    raise WindowsSelectionFailure('no_verified_accessory', 'No WinUSB function matched the selected AOA data pipes',
                                  evidence, attempted=identity)
