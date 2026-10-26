import unittest

from hu_lab.macos_activity import (
    USER_INITIATED_ALLOWING_IDLE_SYSTEM_SLEEP,
    UserInitiatedActivity,
    _FoundationActivityBackend,
)


class FakeBackend:
    def __init__(self, begin_error=None, end_error=None, release_error=None):
        self.calls = []
        self.begin_error, self.end_error = begin_error, end_error
        self.release_error = release_error
        self.token = object()

    def begin(self, reason, options):
        self.calls.append(("begin", reason, options))
        if self.begin_error:
            raise self.begin_error
        return self.token

    def end(self, token):
        self.calls.append(("end", token))
        if self.end_error:
            raise self.end_error

    def release(self, token):
        self.calls.append(("release", token))
        if self.release_error:
            raise self.release_error


class ActivityTests(unittest.TestCase):
    def activity(self, backend, platform="darwin"):
        return UserInitiatedActivity("test stream", platform=platform,
                                     backend_factory=lambda: backend)

    def test_balanced_idempotent_lifecycle_and_reconnect(self):
        backend = FakeBackend()
        activity = self.activity(backend)
        self.assertEqual(activity.snapshot()["status"], "idle")
        self.assertTrue(activity.start()["active"])
        self.assertTrue(activity.start()["active"])
        self.assertEqual(activity.close()["status"], "closed")
        self.assertFalse(activity.close()["active"])
        self.assertEqual([c[0] for c in backend.calls], ["begin", "end", "release"])
        self.assertTrue(activity.start()["active"])
        activity.close()
        self.assertEqual([c[0] for c in backend.calls],
                         ["begin", "end", "release"] * 2)
        self.assertEqual(backend.calls[0][2], 0xEFFFFF)
        self.assertEqual(USER_INITIATED_ALLOWING_IDLE_SYSTEM_SLEEP & (1 << 20), 0)

    def test_context_cleans_up_without_swallowing_startup_exception(self):
        backend = FakeBackend()
        activity = self.activity(backend)
        with self.assertRaisesRegex(RuntimeError, "client startup"):
            with activity as entered:
                self.assertIs(entered, activity)
                raise RuntimeError("client startup")
        self.assertFalse(activity.snapshot()["active"])
        self.assertEqual([c[0] for c in backend.calls], ["begin", "end", "release"])

    def test_acquisition_failure_is_explicit_and_retryable(self):
        backend = FakeBackend(begin_error=OSError("unavailable"))
        activity = self.activity(backend)
        result = activity.start()
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["active"])
        self.assertIn("unavailable", result["error"])
        self.assertEqual(activity.close(), result)
        self.assertEqual([c[0] for c in backend.calls], ["begin"])
        backend.begin_error = None
        self.assertTrue(activity.start()["active"])
        self.assertIsNone(activity.snapshot()["error"])
        activity.close()

    def test_nil_or_missing_backend_never_claims_active(self):
        backend = FakeBackend()
        backend.token = None
        self.assertEqual(self.activity(backend).start()["status"], "failed")
        def unavailable():
            raise OSError("Foundation missing")
        activity = UserInitiatedActivity(platform="darwin", backend_factory=unavailable)
        self.assertEqual(activity.start()["status"], "failed")
        self.assertFalse(activity.close()["active"])

    def test_cleanup_failure_still_releases_and_preserves_body_exception(self):
        backend = FakeBackend(end_error=RuntimeError("end failed"),
                              release_error=RuntimeError("release failed"))
        activity = self.activity(backend)
        with self.assertRaisesRegex(ValueError, "body"):
            with activity:
                raise ValueError("body")
        result = activity.snapshot()
        self.assertEqual(result["status"], "failed")
        self.assertIn("end failed", result["error"])
        self.assertIn("release failed", result["error"])
        self.assertFalse(result["active"])
        activity.close()
        self.assertEqual([c[0] for c in backend.calls], ["begin", "end", "release"])

    def test_non_darwin_is_noop_without_loading_backend(self):
        def forbidden():
            self.fail("must not load native backend")
        activity = UserInitiatedActivity(platform="linux", backend_factory=forbidden)
        with activity:
            self.assertEqual(activity.snapshot()["status"], "unsupported")
            self.assertFalse(activity.start()["active"])
        self.assertEqual(activity.close()["status"], "unsupported")

    def test_native_bridge_retains_before_autorelease_pool_drains(self):
        # Exercise ownership ordering without loading Foundation or asserting
        # any real process activity.
        backend = _FoundationActivityBackend.__new__(_FoundationActivityBackend)
        backend._selectors = {s: s for s in (
            "alloc", "init", "initWithUTF8String:", "retain", "release", "drain",
            "beginActivityWithOptions:reason:", "endActivity:")}
        backend._pool_class, backend._string_class, backend._process = 1, 2, 3
        calls = []
        def object_call(obj, selector):
            calls.append((selector, obj))
            return obj
        backend._object = object_call
        backend._string = lambda *args: 4
        backend._begin = lambda *args: 5
        backend._void = lambda obj, selector: calls.append((selector, obj))
        backend._void_object = lambda obj, selector, token: calls.append((selector, token))
        token = backend.begin("test", USER_INITIATED_ALLOWING_IDLE_SYSTEM_SLEEP)
        self.assertEqual(token, 5)
        self.assertLess(calls.index(("retain", 5)), calls.index(("drain", 1)))
        self.assertNotIn(("release", 5), calls)
        backend.end(token)
        backend.release(token)
        self.assertEqual(calls[-2:], [("endActivity:", 5), ("release", 5)])


if __name__ == "__main__":
    unittest.main()
