"""Locate native test-process failures without treating an abort as success."""
from __future__ import annotations

import argparse
import gc
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def bool_refs():
    return {"true": sys.getrefcount(True), "false": sys.getrefcount(False)}


class RefcountResult(unittest.TextTestResult):
    def startTest(self, test):
        self.before = bool_refs()
        super().startTest(test)

    def stopTest(self, test):
        after = bool_refs()
        print("BOOL_REFS", json.dumps({"test": test.id(), "before": self.before,
                                       "after": after}), flush=True)
        super().stopTest(test)


def child(module, shutdown_qt=False):
    print("PROCESS_START", json.dumps({"module": module, "refs": bool_refs()}), flush=True)
    loader = unittest.TestLoader()
    suite = loader.discover(str(ROOT / "tests")) if module == "ALL" else loader.loadTestsFromNames(module.split(","))
    print("COLLECTED", json.dumps({"tests": suite.countTestCases(), "refs": bool_refs()}), flush=True)
    result = unittest.TextTestRunner(verbosity=1, resultclass=RefcountResult).run(suite)
    print("SUITE_FINISHED", json.dumps({"module": module, "tests": result.testsRun,
                                       "successful": result.wasSuccessful(), "refs": bool_refs()}), flush=True)
    gc.collect()
    print("GC_FINISHED", json.dumps({"module": module, "refs": bool_refs()}), flush=True)
    if shutdown_qt:
        from PySide6.QtWidgets import QApplication
        application = QApplication.instance()
        if application is not None:
            # Documented PySide testing support; normal interpreter shutdown
            # still follows and its exit status remains part of the result.
            # https://doc.qt.io/qtforpython-6/considerations.html#testing-support
            application.shutdown()
            if QApplication.instance() is not None:
                raise AssertionError("QApplication survived explicit shutdown")
        print("QT_SHUTDOWN_FINISHED", json.dumps({"module": module, "refs": bool_refs()}), flush=True)
    return 0 if result.wasSuccessful() else 1


def windows(kind):
    """Measure widget lifetime without USB, sockets, video, or test fixtures."""
    from PySide6.QtWidgets import QApplication, QMainWindow
    from hu_lab import desktop
    from tests import dispose_qt_object

    app = QApplication([])
    app.setQuitOnLastWindowClosed(False)
    with tempfile.TemporaryDirectory() as directory, patch.object(desktop, "list_usb_devices", return_value=[]):
        for iteration in range(40):
            window = desktop.HeadUnitWindow(Path(directory)) if kind == "headunit" else QMainWindow()
            window.show()
            deadline = time.monotonic() + 5
            while True:
                app.processEvents()
                if not getattr(window, "busy", False):
                    break
                if time.monotonic() > deadline:
                    raise AssertionError("Window USB-discovery mock failed to settle")
                time.sleep(0.005)
            window.close()
            dispose_qt_object(window)
            del window
            gc.collect()
            print("WINDOW_CYCLE", json.dumps({"kind": kind, "iteration": iteration, "refs": bool_refs()}), flush=True)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--module")
    parser.add_argument("--shutdown-qt", action="store_true")
    parser.add_argument("--groups", action="store_true")
    parser.add_argument("--windows", choices=["headunit", "plain"])
    args = parser.parse_args()
    if args.windows:
        return windows(args.windows)
    if args.module:
        return child(args.module, args.shutdown_qt)
    results = {}
    modules = ["tests." + path.stem for path in sorted((ROOT / "tests").glob("test_*.py"))]
    groups = {module: module for module in ["ALL"] + modules}
    if args.groups:
        qt = {"tests.test_desktop_audio", "tests.test_desktop_input_ui",
              "tests.test_desktop_keyboard_ui", "tests.test_desktop_ui",
              "tests.test_gui_frame_wake", "tests.test_keyboard_input"}
        qt_modules = [module for module in modules if module in qt]
        groups = {"qt_first_half": ",".join(qt_modules[:3]),
                  "qt_second_half": ",".join(qt_modules[3:])}
        for kind in ("plain", "headunit"):
            completed = subprocess.run([sys.executable, "-X", "faulthandler", __file__, "--windows", kind], cwd=ROOT)
            results["windows_" + kind] = completed.returncode
            print("PROCESS_EXIT", json.dumps({"module": "windows_" + kind, "returncode": completed.returncode}), flush=True)
    for label, module in groups.items():
        print("::group::Native process diagnostic: " + label, flush=True)
        command = [sys.executable, "-X", "faulthandler", __file__, "--module", module]
        if args.shutdown_qt:
            command.append("--shutdown-qt")
        completed = subprocess.run(command, cwd=ROOT)
        results[label] = completed.returncode
        print("PROCESS_EXIT", json.dumps({"module": label, "returncode": completed.returncode}), flush=True)
        print("::endgroup::", flush=True)
    print("DIAGNOSTIC_RESULTS", json.dumps(results), flush=True)
    return 1 if any(results.values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
