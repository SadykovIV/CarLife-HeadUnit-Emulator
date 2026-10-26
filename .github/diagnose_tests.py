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


def child(module):
    print("PROCESS_START", json.dumps({"module": module, "refs": bool_refs()}), flush=True)
    loader = unittest.TestLoader()
    suite = loader.discover(str(ROOT / "tests")) if module == "ALL" else loader.loadTestsFromName(module)
    print("COLLECTED", json.dumps({"tests": suite.countTestCases(), "refs": bool_refs()}), flush=True)
    result = unittest.TextTestRunner(verbosity=1, resultclass=RefcountResult).run(suite)
    print("SUITE_FINISHED", json.dumps({"module": module, "tests": result.testsRun,
                                       "successful": result.wasSuccessful(), "refs": bool_refs()}), flush=True)
    gc.collect()
    print("GC_FINISHED", json.dumps({"module": module, "refs": bool_refs()}), flush=True)
    return 0 if result.wasSuccessful() else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--module")
    args = parser.parse_args()
    if args.module:
        return child(args.module)
    results = {}
    modules = ["ALL"] + ["tests." + path.stem for path in sorted((ROOT / "tests").glob("test_*.py"))]
    for module in modules:
        print("::group::Native process diagnostic: " + module, flush=True)
        completed = subprocess.run([sys.executable, "-X", "faulthandler", __file__, "--module", module], cwd=ROOT)
        results[module] = completed.returncode
        print("PROCESS_EXIT", json.dumps({"module": module, "returncode": completed.returncode}), flush=True)
        print("::endgroup::", flush=True)
    print("DIAGNOSTIC_RESULTS", json.dumps(results), flush=True)
    return 1 if any(results.values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
