"""A verified WinUSB interface for an already-enumerated Android accessory.

The caller supplies a present interface candidate bound to the selected USB
parent. A symbolic link or an ADB-labelled child alone is never sufficient.
Only the same inspected WinUSB handle may subsequently carry CarLife traffic.
"""
from __future__ import annotations

import ctypes as C
import hashlib
import math
import sys
import threading
import time

from .usb import RecoveryBlocked, select_bulk_pair


class WinUSBCandidateMismatch(RecoveryBlocked):
    """The opened candidate demonstrably does not expose the accessory pipes."""
    def __init__(self, reason, actual=None):
        self.selection_reason = reason
        self.actual_interface = actual
        super().__init__(f'WinUSB accessory interface rejected: {reason}')


class WinUSBIdentityMismatch(RecoveryBlocked):
    """Do not try another device after the selected enumeration changes."""
    selection_reason = 'identity_mismatch'


class WinUSBError(OSError):
    native_api = 'WinUSB'

    def __init__(self, operation, code, *, issued=False):
        super().__init__(f'{operation}: Win32 error {code}')
        self.operation = operation
        self.winerror = int(code)
        self.request_may_have_run = issued


def _transfer_error(operation, code, *, issued):
    if code == 121:  # ERROR_SEM_TIMEOUT
        error = TimeoutError(f'{operation}: Win32 error {code}')
        error.operation, error.winerror = operation, code
        error.native_api, error.request_may_have_run = 'WinUSB', issued
        return error
    return WinUSBError(operation, code, issued=issued)


U8, U16, U32, BOOL, HANDLE = C.c_uint8, C.c_uint16, C.c_uint32, C.c_int32, C.c_void_p
P = C.POINTER


class _Overlapped(C.Structure):
    # Offset/OffsetHigh occupy the eight-byte offset union on Windows x64.
    _fields_ = [('internal', C.c_size_t), ('internal_high', C.c_size_t),
                ('offset', U32), ('offset_high', U32), ('event', HANDLE)]


class _Interface(C.Structure):
    _pack_ = 1
    _fields_ = [(k, U8) for k in ('length', 'type', 'number', 'alternate', 'count',
                                   'usb_class', 'subclass', 'protocol', 'string')]


class _Pipe(C.Structure):
    _fields_ = [('type', C.c_int32), ('address', U8), ('packet', U16), ('interval', U8)]


class _Setup(C.Structure):
    _pack_ = 1
    _fields_ = [('type', U8), ('request', U8), ('value', U16), ('index', U16), ('length', U16)]


class WinUSBAPI:
    """Bind only system DLLs; constructing this object does not access hardware."""
    def __init__(self):
        if sys.platform != 'win32':
            raise RuntimeError('WinUSB is available only on Windows')
        self.kernel = C.WinDLL('kernel32.dll', winmode=0x800, use_last_error=True)
        self.usb = C.WinDLL('winusb.dll', winmode=0x800, use_last_error=True)
        def bind(dll, name, result, *args):
            fn = getattr(dll, name)
            fn.argtypes, fn.restype = list(args), result
        bind(self.kernel, 'CreateFileW', HANDLE, C.c_wchar_p, U32, U32, HANDLE, U32, U32, HANDLE)
        bind(self.kernel, 'CreateEventW', HANDLE, HANDLE, BOOL, BOOL, C.c_wchar_p)
        bind(self.kernel, 'CloseHandle', BOOL, HANDLE)
        bind(self.kernel, 'WaitForSingleObject', U32, HANDLE, U32)
        bind(self.kernel, 'CancelIoEx', BOOL, HANDLE, P(_Overlapped))
        bind(self.usb, 'WinUsb_Initialize', BOOL, HANDLE, P(HANDLE))
        bind(self.usb, 'WinUsb_Free', BOOL, HANDLE)
        bind(self.usb, 'WinUsb_GetDescriptor', BOOL, HANDLE, U8, U8, U16, P(U8), U32, P(U32))
        bind(self.usb, 'WinUsb_GetCurrentAlternateSetting', BOOL, HANDLE, P(U8))
        bind(self.usb, 'WinUsb_QueryInterfaceSettings', BOOL, HANDLE, U8, P(_Interface))
        bind(self.usb, 'WinUsb_QueryPipe', BOOL, HANDLE, U8, U8, P(_Pipe))
        for fn in ('WinUsb_ReadPipe', 'WinUsb_WritePipe'):
            bind(self.usb, fn, BOOL, HANDLE, U8, P(U8), U32, P(U32), P(_Overlapped))
        bind(self.usb, 'WinUsb_ControlTransfer', BOOL, HANDLE, _Setup, P(U8), U32, P(U32), P(_Overlapped))
        bind(self.usb, 'WinUsb_GetOverlappedResult', BOOL, HANDLE, P(_Overlapped), P(U32), BOOL)

    def _check(self, ok, operation):
        if not ok:
            raise WinUSBError(operation, C.get_last_error())

    def open_file(self, path):
        file = self.kernel.CreateFileW(path, 0xc0000000, 3, None, 3, 0x40000000, None)
        if file is None or file == C.c_void_p(-1).value:
            raise WinUSBError('CreateFileW(selected interface)', C.get_last_error())
        return file

    def initialize(self, file):
        interface = HANDLE()
        self._check(self.usb.WinUsb_Initialize(file, C.byref(interface)), 'WinUsb_Initialize')
        return interface

    def close_file(self, file):
        self._check(self.kernel.CloseHandle(file), 'CloseHandle')

    def free(self, interface):
        self._check(self.usb.WinUsb_Free(interface), 'WinUsb_Free')

    def descriptor(self, interface, kind, index, language, capacity):
        buffer, length = (U8 * capacity)(), U32()
        self._check(self.usb.WinUsb_GetDescriptor(interface, kind, index, language, buffer,
                                                  capacity, C.byref(length)), 'WinUsb_GetDescriptor')
        if length.value > capacity:
            raise WinUSBIdentityMismatch('Invalid USB descriptor byte count')
        return bytes(buffer[:length.value])

    def interface_info(self, interface):
        alt = U8()
        self._check(self.usb.WinUsb_GetCurrentAlternateSetting(interface, C.byref(alt)),
                    'WinUsb_GetCurrentAlternateSetting')
        desc = _Interface()
        self._check(self.usb.WinUsb_QueryInterfaceSettings(interface, alt.value, C.byref(desc)),
                    'WinUsb_QueryInterfaceSettings')
        if desc.length != 9 or desc.type != 4 or desc.alternate != alt.value or desc.count > 30:
            raise WinUSBIdentityMismatch('Invalid live WinUSB interface descriptor')
        pipes = []
        for index in range(desc.count):
            pipe = _Pipe()
            self._check(self.usb.WinUsb_QueryPipe(interface, alt.value, index, C.byref(pipe)), 'WinUsb_QueryPipe')
            pipes.append(dict(type=pipe.type, address=pipe.address, max_packet=pipe.packet))
        return dict(number=desc.number, alternate=desc.alternate, usb_class=desc.usb_class,
                    subclass=desc.subclass, protocol=desc.protocol, pipes=pipes)

    def transfer(self, file, interface, op, pipe, buffer, length, deadline, cancel, setup=None):
        """One submission; keep the event, OVERLAPPED and buffer alive through final completion."""
        if cancel.is_set():
            raise InterruptedError('WinUSB transfer cancelled before submission')
        if time.monotonic() >= deadline:
            raise TimeoutError('WinUSB deadline before submission')
        event = self.kernel.CreateEventW(None, 1, 0, None)  # private, manual-reset event
        if not event:
            raise WinUSBError('CreateEventW', C.get_last_error())
        overlapped, count = _Overlapped(), U32()
        overlapped.event = event
        target = C.byref(overlapped)
        try:
            if op == 'control':
                ok = self.usb.WinUsb_ControlTransfer(interface, setup, buffer, length, C.byref(count), target)
            else:
                method = self.usb.WinUsb_ReadPipe if op == 'read' else self.usb.WinUsb_WritePipe
                ok = method(interface, pipe, buffer, length, C.byref(count), target)
            if ok:
                # This is still an OVERLAPPED submission, even when it
                # completed immediately. Use the completion's byte count.
                if not self.usb.WinUsb_GetOverlappedResult(interface, target, C.byref(count), 1):
                    raise _transfer_error(f'WinUSB {op} completion', C.get_last_error(), issued=True)
            else:
                code = C.get_last_error()
                if code != 997:  # ERROR_IO_PENDING
                    raise _transfer_error(f'WinUSB {op}', code, issued=code not in (5, 6, 50, 87))
                # CancelIoEx does not wait. Always drain the completion before
                # releasing the OVERLAPPED, file, event or caller-owned buffer.
                reason, signalled = None, False
                try:
                    while True:
                        remaining = deadline - time.monotonic()
                        if cancel.is_set() or remaining <= 0:
                            reason = 'cancelled' if cancel.is_set() else 'timeout'
                            break
                        wait = self.kernel.WaitForSingleObject(event, max(1, min(50, math.ceil(remaining * 1000))))
                        if wait == 0:
                            signalled = True
                            break
                        if wait != 258:  # WAIT_TIMEOUT
                            raise WinUSBError('WaitForSingleObject', C.get_last_error(), issued=True)
                finally:
                    # Also drain if Python itself interrupted the wait.
                    if not signalled:
                        self.kernel.CancelIoEx(file, target)  # NOT_FOUND can mean completion won.
                    completed = self.usb.WinUsb_GetOverlappedResult(interface, target, C.byref(count), 1)
                    completion_error = C.get_last_error() if not completed else 0
                if not completed:
                    if completion_error == 995 and reason in ('cancelled', 'timeout'):
                        error = (InterruptedError if reason == 'cancelled' else TimeoutError)(
                            f'WinUSB {op} {reason}: Win32 error {completion_error}; transfer not replayed')
                        error.request_may_have_run = True
                        error.winerror = completion_error
                        error.native_api, error.operation = 'WinUSB', f'WinUSB {op} completion'
                        raise error
                    raise _transfer_error(f'WinUSB {op} completion', completion_error, issued=True)
            if count.value > length:
                raise RuntimeError(f'WinUSB {op} returned an invalid byte count')
            return count.value
        finally:
            self.kernel.CloseHandle(event)


def _serial_hash(api, interface, index):
    if not index:
        raise WinUSBIdentityMismatch('Selected device has no USB serial descriptor')
    lang = api.descriptor(interface, 3, 0, 0, 4)
    if len(lang) != 4 or lang[0] < 4 or lang[0] % 2 or lang[1] != 3:
        raise WinUSBIdentityMismatch('Invalid USB language descriptor')
    raw = api.descriptor(interface, 3, index, int.from_bytes(lang[2:4], 'little'), 255)
    if len(raw) < 4 or raw[0] > len(raw) or raw[0] < 4 or raw[0] % 2 or raw[1] != 3:
        raise WinUSBIdentityMismatch('Invalid USB serial descriptor')
    # libusb_get_string_descriptor_ascii uses the first LANGID and maps each
    # UTF-16 code unit >= 0x80 to '?', including each half of a surrogate pair.
    units = (int.from_bytes(raw[i:i+2], 'little') for i in range(2, raw[0], 2))
    return hashlib.sha256(bytes(x if x < 128 else 63 for x in units)).hexdigest()


def _check_interface(actual, expected):
    if (actual['usb_class'], actual['subclass'], actual['protocol']) == (255, 66, 1):
        raise WinUSBCandidateMismatch('adb_interface', actual)
    if (actual['number'], actual['alternate'], actual['usb_class']) != (0, 0, 255):
        raise WinUSBCandidateMismatch('interface_mismatch', actual)
    if (actual['usb_class'], actual['subclass'], actual['protocol']) != (
            expected['class'], expected['subclass'], expected['protocol']):
        raise WinUSBCandidateMismatch('interface_class_mismatch', actual)
    pipes = sorted((p['address'], p['type'], p['max_packet']) for p in actual['pipes'])
    advertised = sorted((p['address'], p['attributes'] & 3, p['max_packet']) for p in expected['endpoints'])
    if pipes != advertised or len({p[0] for p in pipes}) != len(pipes):
        raise WinUSBCandidateMismatch('pipe_mismatch', actual)


class VerifiedWinUSBHandle:
    def __init__(self, api, file, interface, identity, descriptor, candidate, verified, pair, digest):
        self.api, self._file, self._interface = api, file, interface
        self.identity, self.descriptor, self._candidate = identity, descriptor, candidate
        self.verified_interface, self._verified_pair, self._serial_hash = verified, pair, digest
        self.pair = None
        self._write_failed = False
        self.cancel, self._retiring = threading.Event(), threading.Event()
        self._read, self._write, self._control = threading.Lock(), threading.Lock(), threading.RLock()
        self._close_lock = threading.Lock()

    def __enter__(self):
        return self

    def __exit__(self, _kind, primary, _traceback):
        try:
            self.close()
        except Exception as cleanup:
            if primary is None:
                raise
            if hasattr(primary, 'add_note'):
                primary.add_note(f'WinUSB cleanup: {type(cleanup).__name__}')

    def serial_sha256(self):
        if self._retiring.is_set():
            raise RecoveryBlocked('WinUSB interface is closed')
        return self._serial_hash

    def claim_accessory(self, gate):
        with self._control:
            if self._retiring.is_set():
                raise RecoveryBlocked('WinUSB interface is closed')
            gate.require(self.identity)
            if self._candidate.verify_present() is not True:
                raise WinUSBIdentityMismatch('Selected WinUSB interface is no longer present')
            self.pair = self._verified_pair

    def _require_claim(self):
        if self._retiring.is_set() or self.pair is None:
            raise RecoveryBlocked('WinUSB accessory interface is not claimed')

    def read(self, max_bytes=1024*1024, timeout=2):
        with self._read:
            self._require_claim()
            if not 16384 <= max_bytes <= 4*1024*1024 or max_bytes % self.pair.max_packet_in:
                raise ValueError('Invalid WinUSB read capacity')
            buffer = (U8 * max_bytes)()
            deadline = time.monotonic() + timeout
            while not self.cancel.is_set():
                if time.monotonic() >= deadline:
                    raise TimeoutError('WinUSB read deadline')
                n = self.api.transfer(self._file, self._interface, 'read', self.pair.endpoint_in,
                                      buffer, max_bytes, deadline, self.cancel)
                if n:
                    return bytes(buffer[:n])
            raise InterruptedError('WinUSB read cancelled')

    def write(self, data, timeout=2):
        if len(data) > 4*1024*1024:
            raise ValueError('Write exceeds 4MiB bounded buffer')
        with self._write:
            self._require_claim()
            if self._write_failed:
                raise OSError('Previous WinUSB write failed; replay on this handle is prohibited')
            deadline, offset = time.monotonic() + timeout, 0
            try:
                while offset < len(data):
                    if self.cancel.is_set():
                        raise InterruptedError('WinUSB write cancelled')
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f'WinUSB write deadline after {offset}/{len(data)} bytes')
                    part = data[offset:offset+16384]
                    buffer = (U8 * len(part)).from_buffer_copy(part)
                    # Continue only confirmed progress. A timeout or failure
                    # can leave a prefix on the wire, so retire further writes.
                    n = self.api.transfer(self._file, self._interface, 'write', self.pair.endpoint_out,
                                          buffer, len(part), deadline, self.cancel)
                    if not 0 < n <= len(part):
                        raise OSError('WinUSB write made no valid progress')
                    offset += n
            except Exception:
                self._write_failed = True
                raise
            return offset

    def hid_control(self, request, hid_id, *, index=0, data=b''):
        if request not in (54, 55, 56, 57) or type(hid_id) is not int or not 1 <= hid_id <= 32:
            raise ValueError('Invalid AOA HID request or ID')
        if type(index) is not int or not 0 <= index <= 65535 or len(data) > 4096:
            raise ValueError('Invalid AOA HID index or data length')
        with self._control:
            self._require_claim()
            setup = _Setup(0x40, request, hid_id, index, len(data))
            buffer = (U8 * max(1, len(data)))()
            buffer[:len(data)] = data
            n = self.api.transfer(self._file, self._interface, 'control', 0, buffer, len(data),
                                  time.monotonic()+.2, self._retiring, setup)
            if n != len(data):
                error = OSError(f'Short WinUSB HID transfer: {n}/{len(data)} bytes')
                error.request_may_have_run = True
                raise error
            return bytes(buffer[:n])

    def close(self):
        with self._close_lock:
            if self._file is None:
                return
            self.cancel.set()
            self._retiring.set()
            with self._read, self._write, self._control:
                try:
                    self.api.free(self._interface)
                finally:
                    try:
                        self.api.close_file(self._file)
                    finally:
                        self._interface = self._file = self.pair = None


def open_verified_winusb(candidate, identity, descriptor, *, api=None):
    """Open and verify one exact child; do not perform bulk/HID or change USB configuration."""
    if candidate.role not in ('mi_00', 'unlabelled', 'device'):
        raise WinUSBCandidateMismatch('ineligible_child_role')
    if (identity.vid, identity.pid) not in ((0x18d1, 0x2d00), (0x18d1, 0x2d01)):
        raise WinUSBIdentityMismatch('Selected USB device is not in accessory mode')
    cached = descriptor['identity']
    if (cached['bus'], tuple(cached['ports']), cached['address'], cached['vid'], cached['pid']) != (
            identity.bus, identity.ports, identity.address, identity.vid, identity.pid):
        raise WinUSBIdentityMismatch('Selected USB enumeration changed before WinUSB open')
    if not identity.ports or descriptor.get('configuration') != 1:
        raise WinUSBIdentityMismatch('Expected a physical USB port and active accessory configuration 1')
    pair = select_bulk_pair(descriptor)
    expected_interfaces = [item for item in descriptor['interfaces'] if (item['number'], item['alternate']) == (0, 0)]
    if len(expected_interfaces) != 1:
        raise WinUSBIdentityMismatch('No unique cached accessory interface zero')
    expected = expected_interfaces[0]
    if not pair.max_packet_in or not pair.max_packet_out:
        raise WinUSBIdentityMismatch('Invalid bulk endpoint packet size')
    api = api or WinUSBAPI()
    if candidate.verify_present() is not True:
        raise WinUSBIdentityMismatch('Selected WinUSB interface is no longer present')
    file = api.open_file(candidate.path)
    interface = None
    try:
        interface = api.initialize(file)
        actual = api.interface_info(interface)
        _check_interface(actual, expected)
        device = api.descriptor(interface, 1, 0, 0, 18)
        if (len(device) != 18 or device[:2] != bytes((18, 1)) or
                int.from_bytes(device[8:10], 'little') != identity.vid or
                int.from_bytes(device[10:12], 'little') != identity.pid):
            raise WinUSBIdentityMismatch('Opened WinUSB device descriptor differs from the selection')
        digest = _serial_hash(api, interface, device[16])
        if identity.serial_sha256 and digest != identity.serial_sha256:
            raise WinUSBIdentityMismatch('Opened WinUSB serial does not match the selected device')
        if candidate.verify_present() is not True:
            raise WinUSBIdentityMismatch('Selected WinUSB interface changed during verification')
        return VerifiedWinUSBHandle(api, file, interface, identity, descriptor, candidate, actual, pair, digest)
    except BaseException as error:
        for operation, handle in ((api.free, interface), (api.close_file, file)):
            if handle is not None:
                try:
                    operation(handle)
                except Exception as cleanup:
                    if hasattr(error, 'add_note'):
                        error.add_note(f'WinUSB cleanup: {type(cleanup).__name__}')
        raise
