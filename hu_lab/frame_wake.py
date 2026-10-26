"""One queued Qt wake for the latest-frame slot; never a queue of frames."""
import threading
from PySide6.QtCore import QObject, Qt, Signal, Slot


class FrameWake(QObject):
    ready = Signal()

    def __init__(self, refresh, parent=None):
        super().__init__(parent)
        self.refresh = refresh
        self.lock = threading.Lock()
        self.pending = False
        self.closed = False
        self.counts = dict(notifications=0, emitted=0, coalesced=0, consumed=0)
        self.ready.connect(self.consume, Qt.QueuedConnection)

    def notify(self):
        """May run on the decoder worker; emits no more than one pending wake."""
        with self.lock:
            if self.closed:
                return
            self.counts["notifications"] += 1
            if self.pending:
                self.counts["coalesced"] += 1
                return
            self.pending = True
            self.counts["emitted"] += 1
            # QueuedConnection cannot run consume synchronously. Keeping
            # emit under this lock makes close wait for an in-flight emit
            # before the QObject can be destroyed (worker join is bounded).
            self.ready.emit()

    @Slot()
    def consume(self):
        with self.lock:
            if self.closed:
                return
            # Clear BEFORE reading latest. A publication during refresh can
            # schedule one further wake; publications before it are coalesced.
            self.pending = False
            self.counts["consumed"] += 1
        self.refresh()

    def close(self):
        """Stop notifications on the GUI thread, before deleting the parent."""
        with self.lock:
            if self.closed:
                return
            self.closed = True
            self.pending = False
        self.ready.disconnect(self.consume)

    def snapshot(self):
        with self.lock:
            return {**self.counts, "pending": self.pending,
                    "mode": "queued Qt signal; one pending wake; latest frame only; 33ms timer fallback"}
