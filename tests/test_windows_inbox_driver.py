"""Run the production helper's portable C guards and registry fault scenarios."""
from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import unittest


class InboxDriverPolicyTests(unittest.TestCase):
    def test_native_policy_and_guid_ownership_transactions(self):
        compiler = shutil.which("cc") or shutil.which("clang") or shutil.which("gcc")
        if not compiler:
            self.skipTest("A C compiler is required for the native installer policy tests")
        fixture = Path(__file__).parent / "native" / "windows_setup_policy.c"
        with tempfile.TemporaryDirectory() as work:
            binary = Path(work) / ("check.exe" if os.name == "nt" else "check")
            subprocess.run([compiler, "-std=c11", "-Wall", "-Wextra", "-Werror", str(fixture),
                            "-o", str(binary)], check=True, timeout=20)
            subprocess.run([str(binary)], check=True, timeout=5)
