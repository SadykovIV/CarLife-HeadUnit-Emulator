"""Locate native test-process failures without treating an abort as success."""
from __future__ import annotations

import argparse
import gc
import json
from pathlib import Path
import subprocess
import sys
import unittest

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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--module")
    parser.add_argument("--shutdown-qt", action="store_true")
    parser.add_argument("--groups", action="store_true")
    args = parser.parse_args()
    if args.module:
        return child(args.module, args.shutdown_qt)
    results = {}
    modules = ["tests." + path.stem for path in sorted((ROOT / "tests").glob("test_*.py"))]
    groups = {module: module for module in ["ALL"] + modules}
    if args.groups:
        qt = {"tests.test_desktop_audio", "tests.test_desktop_input_ui",
              "tests.test_desktop_keyboard_ui", "tests.test_desktop_ui",
              "tests.test_gui_frame_wake", "tests.test_keyboard_input"}
        split = len(modules) // 2
        groups = {"first_half": ",".join(modules[:split]),
                  "second_half": ",".join(modules[split:]),
                  "qt_application_modules": ",".join(module for module in modules if module in qt),
                  "other_modules": ",".join(module for module in modules if module not in qt)}
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
