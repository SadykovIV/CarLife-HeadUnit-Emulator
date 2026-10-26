"""Reproduce PySide SignalInstance boolean ownership errors without app code.

PySide6 6.12.0's Linux wheel loses one True reference per emit/disconnect
under CPython 3.11. This resembles extensions built with the affected limited
ABI Py_RETURN_TRUE macro; the wheel's build headers are not known here.
The corresponding upstream CPython fix is:
https://github.com/python/cpython/commit/9258f3da9175134d03f2c8c7c7eed223802ad945

The macOS wheel on 3.11 passes; Linux CI uses Python 3.13.16. Run this script
with the target runtime and wheel to verify its native signal operations.
Every process still exits normally or reports its real error/abort status.
"""
import gc
import json
import sys

from PySide6.QtCore import QObject, Signal, Slot


class Receiver(QObject):
    ready = Signal()

    @Slot()
    def receive(self):
        pass


def measure(label, callback):
    callback()  # Warm up the native signal path before counting references.
    gc.collect()
    before = sys.getrefcount(True)
    for _ in range(200):
        callback()
    gc.collect()
    after = sys.getrefcount(True)
    print("SIGNAL_REFS", json.dumps({"operation": label, "before": before,
                                    "after": after, "delta": after - before}), flush=True)
    return after >= before


def main():
    receiver = Receiver()
    receiver.ready.connect(receiver.receive)
    emit_ok = measure("emit", receiver.ready.emit)
    reconnect_ok = measure("disconnect/connect", lambda: (
        receiver.ready.disconnect(receiver.receive), receiver.ready.connect(receiver.receive)))
    if not emit_ok or not reconnect_ok:
        raise AssertionError("Native SignalInstance calls lost references to the True singleton")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
