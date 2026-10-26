"""Optional AOAv2 HID keyboard, independent of the CarLife bulk stream.

Only the already owned USB accessory is used. No ADB, firmware changes, remote
clipboard reads, or invented CarLife text messages. Clipboard paste is typing
host text into the focused Android field, not clipboard synchronization.

Wire specification: https://source.android.com/docs/core/interaction/accessories/aoa2
Layout assumptions: Android Generic.kcm and InputDevices' keyboard_layout_russian.kcm.
Select the corresponding physical-keyboard layout on Android and turn Caps Lock
off. US supports ASCII; Russian supports standard ЙЦУКЕН characters/digits and
punctuation. Layout switching and arbitrary Unicode are deliberately not guessed.
"""
from collections import deque
from dataclasses import dataclass
import threading
import time

from .usb import USBError


MAX_TEXT_LENGTH = 4096
MAX_PENDING_KEYS = 4096
MAX_PENDING_BATCHES = 32
SHIFT = 0x02
CTRL = 0x01
ALT = 0x04
RELEASE_REPORT = bytes(8)
# HID Generic Desktop / Keyboard: modifiers, reserved byte, six usage slots.
# Input only: no LED output endpoint is requested from the Android accessory.
KEYBOARD_REPORT_DESCRIPTOR = bytes.fromhex(
    '05010906a101050719e029e715002501750195088102'
    '95017508810195067508150025650507190029658100c0')


class KeyboardInputError(RuntimeError):
    pass


def key_report(usage, modifiers=0):
    if type(usage) is not int or not 4 <= usage <= 0x65:
        raise KeyboardInputError('Unsupported HID keyboard usage')
    if type(modifiers) is not int or not 0 <= modifiers <= 0xff:
        raise KeyboardInputError('Invalid HID keyboard modifiers')
    return bytes((modifiers, 0, usage, 0, 0, 0, 0, 0))


def _layout(layout):
    if layout not in ('us', 'ru'):
        raise KeyboardInputError('Choose the Android physical keyboard layout: us or ru')
    result = {' ': (0x2c, 0), '\n': (0x28, 0), '\t': (0x2b, 0)}
    for digit, usage in zip('1234567890', range(0x1e, 0x28)):
        result[digit] = usage, 0
    if layout == 'us':
        for n, char in enumerate('abcdefghijklmnopqrstuvwxyz', 4):
            result[char] = n, 0
            result[char.upper()] = n, SHIFT
        pairs = [(0x1e + n, c) for n, c in enumerate('!@#$%^&*()')]
        for usage, plain, shifted in (
                (0x2d, '-', '_'), (0x2e, '=', '+'), (0x2f, '[', '{'),
                (0x30, ']', '}'), (0x31, '\\', '|'), (0x33, ';', ':'),
                (0x34, "'", '"'), (0x35, '`', '~'), (0x36, ',', '<'),
                (0x37, '.', '>'), (0x38, '/', '?')):
            result[plain] = usage, 0
            pairs.append((usage, shifted))
    else:
        latin = '`qwertyuiop[]asdfghjkl;\'zxcvbnm,.'
        russian = 'ёйцукенгшщзхъфывапролджэячсмитьбю'
        us = _layout('us')
        for char, physical in zip(russian, latin):
            usage = us[physical][0]
            result[char] = usage, 0
            result[char.upper()] = usage, SHIFT
        pairs = [(0x1e + n, c) for n, c in enumerate('!"№;%:?*()')]
        for usage, plain, shifted in ((0x2d, '-', '_'), (0x2e, '=', '+'),
                                      (0x31, '\\', '/'), (0x38, '.', ',')):
            result[plain] = usage, 0
            pairs.append((usage, shifted))
    for usage, char in pairs:
        result[char] = usage, SHIFT
    return result


def text_reports(text, layout='us'):
    """Validate the complete batch before producing any USB input.

    Windows CRLF is one Enter; lone CR is also a line break. Unsupported code
    points are errors, never omitted/replaced. Error messages do not expose text.
    """
    if not isinstance(text, str):
        raise KeyboardInputError('Text must be a string')
    if len(text) > MAX_TEXT_LENGTH:
        raise KeyboardInputError(f'Text is limited to {MAX_TEXT_LENGTH} characters')
    mapping = _layout(layout)
    reports = []
    for index, char in enumerate(text.replace('\r\n', '\n').replace('\r', '\n')):
        if char not in mapping:
            raise KeyboardInputError(
                f'Character at position {index + 1} is unavailable in layout {layout}; nothing was queued')
        reports.append(key_report(*mapping[char]))
    return tuple(reports)


@dataclass(frozen=True)
class KeyboardCommand:
    kind: str
    usage: int = 0
    modifiers: int = 0
    text: str = ''
    hard_key: str | None = None


def qt_keyboard_command(event, layout='us'):
    """Translate a Qt key press without reading clipboard or doing USB IO.

    Return kinds: paste (caller explicitly reads clipboard), key, text, ignore,
    unsupported. hard_key provides a CarLife fallback for navigation/media only.
    Each key command is an atomic press/release; Qt auto-repeat repeats taps.
    """
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QKeySequence
    if event.matches(QKeySequence.Paste):
        return KeyboardCommand('paste')
    key, mods = event.key(), event.modifiers()
    if key in (Qt.Key_Shift, Qt.Key_Control, Qt.Key_Alt, Qt.Key_Meta, Qt.Key_AltGr):
        return KeyboardCommand('ignore')
    modifier = ((SHIFT if mods & Qt.ShiftModifier else 0) |
                (CTRL if mods & (Qt.ControlModifier | Qt.MetaModifier) else 0) |
                (ALT if mods & Qt.AltModifier else 0))
    special = {
        Qt.Key_Return: (0x28, 'ok'), Qt.Key_Enter: (0x28, 'ok'),
        Qt.Key_Escape: (0x29, 'back'), Qt.Key_Backspace: (0x2a, None),
        Qt.Key_Tab: (0x2b, None), Qt.Key_Backtab: (0x2b, None),
        Qt.Key_Insert: (0x49, None), Qt.Key_Home: (0x4a, 'home'),
        Qt.Key_PageUp: (0x4b, None), Qt.Key_Delete: (0x4c, None),
        Qt.Key_End: (0x4d, None), Qt.Key_PageDown: (0x4e, None),
        Qt.Key_Right: (0x4f, 'right'), Qt.Key_Left: (0x50, 'left'),
        Qt.Key_Down: (0x51, 'down'), Qt.Key_Up: (0x52, 'up'),
    }
    media = {Qt.Key_MediaPrevious: 'previous', Qt.Key_MediaNext: 'next',
             Qt.Key_MediaPlay: 'play', Qt.Key_MediaPause: 'stop',
             Qt.Key_MediaStop: 'stop', Qt.Key_Back: 'back'}
    if key in media:
        return KeyboardCommand('unsupported', hard_key=media[key] if not modifier else None)
    if key in special:
        usage, fallback = special[key]
        if key == Qt.Key_Backtab:
            modifier |= SHIFT
        return KeyboardCommand('key', usage, modifier, hard_key=fallback if not modifier else None)
    if modifier & (CTRL | ALT):
        if Qt.Key_A <= key <= Qt.Key_Z:
            return KeyboardCommand('key', int(key - Qt.Key_A) + 4, modifier)
        if Qt.Key_1 <= key <= Qt.Key_9:
            return KeyboardCommand('key', int(key - Qt.Key_1) + 0x1e, modifier)
        if key == Qt.Key_0:
            return KeyboardCommand('key', 0x27, modifier)
        if key == Qt.Key_Space:
            return KeyboardCommand('key', 0x2c, modifier)
        return KeyboardCommand('unsupported')
    if event.text():
        # Validation here keeps UI input errors immediate. submit_text repeats
        # this before queueing, so direct/IME callers have the same contract.
        text_reports(event.text(), layout)
        return KeyboardCommand('text', text=event.text())
    return KeyboardCommand('unsupported')


class HIDKeyboard:
    """One optional keyboard per USB session, with a bounded independent worker.

    start()/close() belong on the connection worker. Submission and cancellation
    never perform USB IO on the GUI thread. A failure disables only keyboard IO;
    the shared USB handle and CarLife session are never closed by this object.
    """
    def __init__(self, handle=None, *, unavailable_reason=None, hid_id=0x434c,
                 key_interval=.008):
        if type(hid_id) is not int or not 0 < hid_id <= 65535:
            raise ValueError('Invalid HID ID')
        if not 0 <= key_interval <= .1:
            raise ValueError('Invalid keyboard interval')
        self.handle, self.hid_id = handle, hid_id
        self.key_interval = key_interval
        self.reason = unavailable_reason or ('USB accessory connection required' if handle is None else None)
        self.condition = threading.Condition(threading.RLock())
        self.lifecycle = threading.Lock()
        self.stop_event = threading.Event()
        self.jobs = deque()
        self.epoch = 0
        self.pending_keys = self.sent_keys = self.cancelled_keys = 0
        self.release_requested = False
        self.status, self.error = 'idle', None
        self.protocol = None
        self.registered = False
        self.worker = None

    @classmethod
    def for_client(cls, client):
        stream = getattr(client, 'sock', None)
        handle = getattr(stream, 'handle', None)
        if (getattr(client, 'backend', None) != 'DIRECT_USB_AOA' or handle is None
                or not callable(getattr(handle, 'hid_control', None))):
            return cls(unavailable_reason='AOA HID keyboard is available only over native USB; CarLife TCP has no text channel')
        return cls(handle)

    def snapshot(self):
        with self.condition:
            return {'status': self.status, 'available': self.status == 'ready',
                    'aoa_protocol': self.protocol, 'registered': self.registered,
                    'error': self.error, 'reason': self.reason,
                    'pending_keys': self.pending_keys, 'sent_keys': self.sent_keys,
                    'cancelled_keys': self.cancelled_keys,
                    'worker_alive': bool(self.worker and self.worker.is_alive()),
                    'max_text_length': MAX_TEXT_LENGTH, 'layouts': ['us', 'ru'],
                    'clipboard_direction': 'host_to_device_typing_only',
                    'device_text_delivery_verified': False}

    def _event(self, report):
        self.handle.hid_control(57, self.hid_id, data=report)

    def _cleanup(self):
        errors = []
        if self.registered:
            try:
                self._event(RELEASE_REPORT)
            except Exception as error:
                errors.append(f'release: {type(error).__name__}: {error}')
            try:
                self.handle.hid_control(55, self.hid_id)
            except Exception as error:
                errors.append(f'unregister: {type(error).__name__}: {error}')
            finally:
                self.registered = False
        if errors:
            with self.condition:
                self.error = '; '.join(filter(None, [self.error, *errors]))
                self.status = 'failed'

    def start(self):
        with self.lifecycle:
            if self.status != 'idle':
                return self.snapshot()
            if self.handle is None:
                self.status = 'unavailable'
                return self.snapshot()
            try:
                self.protocol = self.handle.get_protocol()
                if self.protocol != 2:
                    self.status = 'unavailable'
                    self.reason = 'The selected accessory does not advertise AOA 2.0 HID'
                    return self.snapshot()
                # An unknown REGISTER outcome is cleaned up once, never retried.
                self.registered = True
                self.handle.hid_control(54, self.hid_id, index=len(KEYBOARD_REPORT_DESCRIPTOR))
                maximum = getattr(self.handle, 'descriptor', {}).get('endpoint0_max_packet', 64)
                chunk = maximum if maximum in (8, 16, 32, 64, 512) else 64
                for offset in range(0, len(KEYBOARD_REPORT_DESCRIPTOR), chunk):
                    self.handle.hid_control(56, self.hid_id, index=offset,
                                            data=KEYBOARD_REPORT_DESCRIPTOR[offset:offset + chunk])
                # Android constructs HID on a worker after the descriptor. Only
                # an all-keys-up report is safe to retry while that worker starts.
                deadline = time.monotonic() + 1
                while True:
                    try:
                        self._event(RELEASE_REPORT)
                        break
                    except USBError as error:
                        if error.code != -9 or time.monotonic() >= deadline:
                            raise
                        self.stop_event.wait(.02)
                self.status = 'ready'
                self.worker = threading.Thread(target=self._run, name='carlife-hid-keyboard', daemon=True)
                try:
                    self.worker.start()
                except Exception:
                    # Thread.start can fail before it owns a native thread;
                    # that object cannot be joined during later UI cleanup.
                    self.worker = None
                    raise
            except Exception as error:
                self.status = 'failed'
                self.error = f'{type(error).__name__}: {error}'
                self._cleanup()
            return self.snapshot()

    def _submit(self, reports):
        with self.condition:
            if self.status != 'ready' or self.stop_event.is_set():
                raise KeyboardInputError(self.reason or self.error or 'USB HID keyboard is not ready')
            if (len(self.jobs) >= MAX_PENDING_BATCHES or
                    self.pending_keys + len(reports) > MAX_PENDING_KEYS):
                raise KeyboardInputError('Keyboard queue is full; wait or stop the current paste')
            if reports:
                self.jobs.append((self.epoch, reports))
                self.pending_keys += len(reports)
                self.condition.notify()
            return len(reports)

    def submit_text(self, text, layout='us'):
        return self._submit(text_reports(text, layout))

    def submit_key(self, usage, modifiers=0):
        return self._submit((key_report(usage, modifiers),))

    def cancel_pending(self):
        with self.condition:
            self.epoch += 1
            removed = sum(len(reports) for _, reports in self.jobs)
            self.cancelled_keys += removed
            self.pending_keys -= removed
            self.jobs.clear()
            self.release_requested = True
            self.condition.notify_all()

    def _run(self):
        try:
            while not self.stop_event.is_set():
                with self.condition:
                    self.condition.wait_for(lambda: self.jobs or self.release_requested or self.stop_event.is_set())
                    if self.stop_event.is_set():
                        break
                    release = self.release_requested
                    self.release_requested = False
                    job = self.jobs.popleft() if self.jobs else None
                if release:
                    self._event(RELEASE_REPORT)
                if job is None:
                    continue
                epoch, reports = job
                for index, report in enumerate(reports):
                    with self.condition:
                        if self.stop_event.is_set() or epoch != self.epoch:
                            remaining = len(reports) - index
                            self.pending_keys -= remaining
                            self.cancelled_keys += remaining
                            break
                    try:
                        self._event(report)
                    finally:
                        # Even an ambiguous press failure gets one release; a
                        # nonzero report is never retried after a USB timeout.
                        self._event(RELEASE_REPORT)
                    with self.condition:
                        self.pending_keys -= 1
                        self.sent_keys += 1
                    self.stop_event.wait(self.key_interval)
        except Exception as error:
            with self.condition:
                self.status = 'failed'
                self.error = f'{type(error).__name__}: {error}'
                self.cancelled_keys += self.pending_keys
                self.pending_keys = 0
                self.jobs.clear()
        finally:
            self._cleanup()
            with self.condition:
                if self.status != 'failed':
                    self.status = 'closed'

    def close(self):
        with self.lifecycle:
            self.stop_event.set()
            self.cancel_pending()
            if self.worker:
                self.worker.join(timeout=2)
                if self.worker.is_alive():
                    with self.condition:
                        self.status = 'failed'
                        self.error = 'Keyboard worker did not stop within its bounded timeout'
            else:
                self._cleanup()
                if self.status not in ('failed', 'unavailable'):
                    self.status = 'closed'
            return self.snapshot()
