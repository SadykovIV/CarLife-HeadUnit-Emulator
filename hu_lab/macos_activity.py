"""Session-scoped macOS App Nap activity; idle system sleep remains allowed.

Apple recommends UserInitiatedAllowingIdleSystemSleep for asynchronous work
explicitly requested by the user. This is a process activity, not a system
preference or a guarantee against sleep, suspension, or scheduling delays.

Sources:
https://developer.apple.com/library/archive/documentation/Performance/Conceptual/power_efficiency_guidelines_osx/PrioritizeWorkAtTheAppLevel.html
https://developer.apple.com/documentation/foundation/processinfo/activityoptions/userinitiatedallowingidlesystemsleep
NSProcessInfo.h (Foundation SDK): NSActivityOptions is uint64_t; the activity
token must remain alive until endActivity:. Deallocating it also ends activity.
"""

import ctypes
import sys
import threading


# NSActivityUserInitiated & ~NSActivityIdleSystemSleepDisabled (1ULL << 20).
USER_INITIATED_ALLOWING_IDLE_SYSTEM_SLEEP = 0x00EFFFFF


class _FoundationActivityBackend:
    """Small ObjC bridge. begin() transfers one retained token to its caller."""

    def __init__(self):
        self._foundation = ctypes.CDLL(
            "/System/Library/Frameworks/Foundation.framework/Foundation")
        self._objc = ctypes.CDLL("/usr/lib/libobjc.A.dylib")
        self._objc.objc_getClass.argtypes = [ctypes.c_char_p]
        self._objc.objc_getClass.restype = ctypes.c_void_p
        self._objc.sel_registerName.argtypes = [ctypes.c_char_p]
        self._objc.sel_registerName.restype = ctypes.c_void_p
        self._object = ctypes.CFUNCTYPE(
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(
                ("objc_msgSend", self._objc))
        self._string = ctypes.CFUNCTYPE(
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_char_p)(
                ("objc_msgSend", self._objc))
        self._begin = ctypes.CFUNCTYPE(
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
            ctypes.c_uint64, ctypes.c_void_p)(("objc_msgSend", self._objc))
        self._void = ctypes.CFUNCTYPE(
            None, ctypes.c_void_p, ctypes.c_void_p)(("objc_msgSend", self._objc))
        self._void_object = ctypes.CFUNCTYPE(
            None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(
                ("objc_msgSend", self._objc))
        self._selectors = {
            name: self._objc.sel_registerName(name.encode("ascii"))
            for name in ("alloc", "init", "initWithUTF8String:", "retain",
                         "release", "drain", "processInfo",
                         "beginActivityWithOptions:reason:", "endActivity:")}
        self._pool_class = self._objc.objc_getClass(b"NSAutoreleasePool")
        self._string_class = self._objc.objc_getClass(b"NSString")
        process_class = self._objc.objc_getClass(b"NSProcessInfo")
        if not all((self._pool_class, self._string_class, process_class)):
            raise RuntimeError("Foundation activity classes unavailable")
        self._process = self._object(
            process_class, self._selectors["processInfo"])
        if not self._process:
            raise RuntimeError("NSProcessInfo unavailable")

    def begin(self, reason, options):
        sel = self._selectors
        pool = self._object(self._object(self._pool_class, sel["alloc"]), sel["init"])
        if not pool:
            raise RuntimeError("Cannot create activity autorelease pool")
        string = token = None
        retained = False
        try:
            string = self._string(
                self._object(self._string_class, sel["alloc"]),
                sel["initWithUTF8String:"], reason.encode("utf-8"))
            if not string:
                raise RuntimeError("Cannot create activity reason")
            token = self._begin(
                self._process, sel["beginActivityWithOptions:reason:"], options, string)
            if not token:
                raise RuntimeError("beginActivityWithOptions returned nil")
            # A Python pointer does not retain an ObjC object. Retain before the
            # local autorelease pool drains, independent of Qt's event loop.
            if not self._object(token, sel["retain"]):
                raise RuntimeError("Cannot retain activity token")
            retained = True
            return token
        except BaseException:
            if token:
                try:
                    self.end(token)
                finally:
                    if retained:
                        self.release(token)
            raise
        finally:
            try:
                if string:
                    self._void(string, sel["release"])
            finally:
                self._void(pool, sel["drain"])

    def end(self, token):
        self._void_object(self._process, self._selectors["endActivity:"], token)

    def release(self, token):
        self._void(token, self._selectors["release"])


class UserInitiatedActivity:
    """Optional process activity with explicit, JSON-safe lifecycle status.

    start()/close() return snapshots and tolerate native capability failures.
    Callers must inspect status; active means an owned activity token, not an
    observation of the OS's App Nap state. Reconnect may start after close.
    Context cleanup never suppresses a body exception. No native calls occur
    outside Darwin. backend_factory exists for tests without OS assertions.
    """

    def __init__(self, reason="Active CarLife USB session", *,
                 platform=None, backend_factory=None):
        if not isinstance(reason, str) or not reason.strip() or "\0" in reason:
            raise ValueError("Activity reason must be a nonempty string without NUL")
        self.reason = reason
        self.platform = sys.platform if platform is None else platform
        self._factory = backend_factory or _FoundationActivityBackend
        self._lock = threading.Lock()
        self._backend = self._token = None
        self._status = "idle"
        self._error = None

    def _snapshot(self):
        return dict(active=self._token is not None, status=self._status,
                    reason=self.reason, platform=self.platform,
                    options=USER_INITIATED_ALLOWING_IDLE_SYSTEM_SLEEP,
                    options_name="NSActivityUserInitiatedAllowingIdleSystemSleep",
                    error=self._error)

    def snapshot(self):
        with self._lock:
            return self._snapshot()

    def start(self):
        with self._lock:
            if self._token is not None:
                return self._snapshot()
            self._error = None
            if self.platform != "darwin":
                self._status = "unsupported"
                return self._snapshot()
            try:
                backend = self._factory()
                token = backend.begin(
                    self.reason, USER_INITIATED_ALLOWING_IDLE_SYSTEM_SLEEP)
                if not token:
                    raise RuntimeError("Activity backend returned no token")
                self._backend, self._token = backend, token
                self._status = "active"
            except Exception as error:
                self._status = "failed"
                self._error = f"{type(error).__name__}: {error}"
            return self._snapshot()

    def close(self):
        with self._lock:
            if self._token is None:
                return self._snapshot()
            backend, token = self._backend, self._token
            self._backend = self._token = None
            errors = []
            try:
                backend.end(token)
            except Exception as error:
                errors.append(f"end: {type(error).__name__}: {error}")
            finally:
                try:
                    backend.release(token)
                except Exception as error:
                    errors.append(f"release: {type(error).__name__}: {error}")
            self._status = "failed" if errors else "closed"
            self._error = "; ".join(errors) or None
            return self._snapshot()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
        return False
