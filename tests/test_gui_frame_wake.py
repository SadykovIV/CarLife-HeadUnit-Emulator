"""Offline event-render tests: actual Qt delivery, no USB or device access."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from PySide6.QtCore import QEvent, QThread
from PySide6.QtWidgets import QApplication

from hu_lab.client import HUClient
from hu_lab.frame_wake import FrameWake


class FrameWakeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def pump_until(self, predicate):
        deadline = time.monotonic() + 1
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents()
        self.assertTrue(predicate(), "queued Qt callback did not arrive")

    def test_worker_burst_retains_latest_and_refreshes_on_gui_thread(self):
        latest, seen = [None], []
        wake = FrameWake(lambda: seen.append((latest[0], QThread.currentThread() == self.app.thread())))
        self.addCleanup(wake.close)
        def worker():
            for index in range(1000):
                latest[0] = index
                wake.notify()
        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertFalse(seen)
        self.assertEqual(wake.snapshot()["emitted"], 1)
        self.pump_until(lambda: bool(seen))
        self.assertEqual(seen, [(999, True)])
        self.assertEqual(wake.snapshot()["coalesced"], 999)

    def test_publication_during_refresh_schedules_next_wake(self):
        latest, seen = [1], []
        def refresh():
            seen.append(latest[0])
            if len(seen) == 1:
                latest[0] = 2
                wake.notify()
        wake = FrameWake(refresh)
        self.addCleanup(wake.close)
        wake.notify()
        self.pump_until(lambda: len(seen) == 2)
        self.assertEqual(seen, [1, 2])
        self.assertEqual(wake.snapshot()["emitted"], 2)

    def test_close_suppresses_pending_and_future_refreshes_and_is_idempotent(self):
        seen = []
        wake = FrameWake(lambda: seen.append(True))
        wake.notify()
        wake.close()
        wake.close()
        wake.notify()
        self.app.processEvents()
        self.assertFalse(seen)
        self.assertEqual(wake.snapshot()["notifications"], 1)

    def test_close_waits_for_inflight_emit_before_qobject_destruction(self):
        """Pause the real signal emission; close must not return through it."""
        seen, errors = [], []
        wake = FrameWake(lambda: seen.append(True))
        actual_signal = wake.ready
        emit_entered, allow_emit, close_returned = (threading.Event() for _ in range(3))
        emit_finished = threading.Event()
        release_observations = []

        class DelayedSignal:
            def emit(self):
                emit_entered.set()
                if not allow_emit.wait(2):
                    raise AssertionError("test release timeout")
                actual_signal.emit()
                emit_finished.set()
            def disconnect(self, slot):
                actual_signal.disconnect(slot)

        def notify():
            try:
                wake.notify()
            except BaseException as error:
                errors.append(error)

        def release():
            # Test only: let the GUI thread enter close while emission is held.
            release_observations.append(close_returned.wait(.03))
            allow_emit.set()

        # PySide caches its bound signals in the QObject instance dictionary.
        with patch.object(wake, "ready", DelayedSignal()):
            worker = threading.Thread(target=notify)
            worker.start()
            self.assertTrue(emit_entered.wait(1))
            releaser = threading.Thread(target=release)
            releaser.start()
            wake.close()
            close_returned.set()
            self.assertTrue(emit_finished.is_set())
            worker.join(1)
            releaser.join(1)
            self.assertFalse(worker.is_alive())
            self.assertFalse(releaser.is_alive())
        self.assertEqual(release_observations, [False])
        self.assertEqual(errors, [])
        wake.deleteLater()
        self.app.sendPostedEvents(None, QEvent.DeferredDelete)
        # A late producer uses only the closed Python state, never the dead QObject.
        wake.notify()
        self.app.processEvents()
        self.assertEqual(seen, [])

    def test_client_observer_precedes_publication_and_notify_runs_after_unlock(self):
        import av
        seen = []
        def observe(record, pixels, width, height, stride):
            self.assertIsNone(client.latest_frame)
            seen.append(("observer", record["frame_index"]))
        def available():
            acquired = client.frame_lock.acquire(blocking=False)
            self.assertTrue(acquired)
            try:
                seen.append(("available", client.latest_frame[-1]))
            finally:
                if acquired:
                    client.frame_lock.release()
        with tempfile.TemporaryDirectory() as directory:
            client = HUClient("127.0.0.1", 1, directory, on_decoded_frame=observe,
                              on_frame_available=available)
            try:
                frame = av.VideoFrame(1280, 720, "yuv420p")
                for plane in frame.planes:
                    plane.update(bytes(plane.buffer_size))
                worker = threading.Thread(target=client._guard, args=(lambda: client._frame(frame),))
                worker.start()
                worker.join(2)
                self.assertFalse(worker.is_alive())
                self.assertIsNone(client.error)
                self.assertEqual(seen, [("observer", 1), ("available", 1)])
            finally:
                client.halt.set()
                client.stop()


if __name__ == "__main__":
    unittest.main()
