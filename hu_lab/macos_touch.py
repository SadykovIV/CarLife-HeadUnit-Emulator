"""Read real trackpad contacts during Qt's synchronous Cocoa touch callback.

Qt 6's QEventPoint.normalizedPosition is relative to a desktop geometry, not
the touchpad. AppKit NSTouch.normalizedPosition provides the actual hardware
coordinates used by Honda Connect's PixelView. No monitor, event interception,
permission, private API or native helper is installed here.

Qt Cocoa qnsview_touch.mm delivers QTouchEvent synchronously. Check its event
timestamp before reading NSApplication.currentEvent; synthetic/queued Qt events
must never borrow unrelated native fingers.
"""
import ctypes
import math
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class TrackpadSnapshot:
    contacts: tuple
    released: tuple = ()
    cancelled: bool = False


class _Point(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


class _CocoaTouchAPI:
    def __init__(self):
        self.objc = ctypes.CDLL("/usr/lib/libobjc.A.dylib")
        self.objc.objc_getClass.argtypes = [ctypes.c_char_p]
        self.objc.objc_getClass.restype = ctypes.c_void_p
        self.objc.sel_registerName.argtypes = [ctypes.c_char_p]
        self.objc.sel_registerName.restype = ctypes.c_void_p
        self.selectors = {}
        self.calls = {}
        self.app_class = self.objc.objc_getClass(b"NSApplication")

    def send(self, result, obj, name, args=(), types=()):
        selector = self.selectors.setdefault(name, self.objc.sel_registerName(name.encode("ascii")))
        key = result, types
        if key not in self.calls:
            self.calls[key] = ctypes.CFUNCTYPE(result, ctypes.c_void_p, ctypes.c_void_p, *types)(
                ("objc_msgSend", self.objc))
        return self.calls[key](obj, selector, *args)

    def snapshot(self):
        obj = ctypes.c_void_p
        integer = ctypes.c_ulong
        app = self.send(obj, self.app_class, "sharedApplication")
        event = self.send(obj, app, "currentEvent") if app else None
        if not event:
            return None
        stamp = self.send(ctypes.c_double, event, "timestamp")
        touch_set = self.send(obj, event, "touchesMatchingPhase:inView:", (31, None), (integer, obj))
        array = self.send(obj, touch_set, "allObjects")
        count = self.send(integer, array, "count")
        if count > 32:
            return stamp, (), True
        rows = []
        for index in range(count):
            touch = self.send(obj, array, "objectAtIndex:", (index,), (integer,))
            if self.send(ctypes.c_bool, touch, "isResting"):
                continue
            point = self.send(_Point, touch, "normalizedPosition")
            identity = self.send(obj, touch, "identity")
            phase = self.send(integer, touch, "phase")
            rows.append((identity, point.x, point.y, phase))
        return stamp, rows, False


class MacOSTrackpad:
    def __init__(self, api=None):
        self.api = api
        self.failed = False

    def read(self, timestamp):
        """Return top-left normalized contacts, or None without a matching NSEvent."""
        if not timestamp or self.failed or (self.api is None and sys.platform != "darwin"):
            return None
        try:
            if self.api is None:
                self.api = _CocoaTouchAPI()
            snapshot = self.api.snapshot()
            if snapshot is None:
                return None
            stamp, rows, cancelled = snapshot
            if not math.isfinite(stamp) or abs(int(stamp * 1000) - timestamp) > 1:
                return None
            active, released, seen = [], [], set()
            for identity, x, y, phase in rows:
                if (type(identity) is not int or identity in seen or
                        not math.isfinite(x) or not math.isfinite(y) or
                        not 0 <= x <= 1 or not 0 <= y <= 1):
                    return TrackpadSnapshot((), cancelled=True)
                seen.add(identity)
                if phase & 16:
                    cancelled = True
                target = released if phase & (8 | 16) else active
                target.append((identity, x, 1 - y))
            return TrackpadSnapshot(tuple(sorted(active)), tuple(sorted(released)), cancelled)
        except (OSError, AttributeError, TypeError, ValueError):
            self.failed = True
            return None
