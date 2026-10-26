"""Run isolated test modules after a failed suite, preserving every exit status."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def child(module):
    suite = unittest.TestLoader().loadTestsFromName(module)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    print("MODULE_RESULT", json.dumps({"module": module, "tests": result.testsRun,
                                       "successful": result.wasSuccessful()}), flush=True)
    return 0 if result.wasSuccessful() else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--module")
    args = parser.parse_args()
    if args.module:
        return child(args.module)
    results = {}
    for path in sorted((ROOT / "tests").glob("test_*.py")):
        module = "tests." + path.stem
        print("::group::Isolated test process: " + module, flush=True)
        completed = subprocess.run([sys.executable, "-X", "faulthandler", __file__,
                                    "--module", module], cwd=ROOT)
        results[module] = completed.returncode
        print("PROCESS_EXIT", json.dumps({"module": module, "returncode": completed.returncode}), flush=True)
        print("::endgroup::", flush=True)
    print("DIAGNOSTIC_RESULTS", json.dumps(results), flush=True)
    return 1 if any(results.values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
