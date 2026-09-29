import subprocess
import sys
import unittest
from pathlib import Path


class SmokeTest(unittest.TestCase):
    def test_module_help(self):
        result = subprocess.run(
            [sys.executable, "-m", "lkfetch", "--help"],
            cwd=Path(__file__).resolve().parents[1] / "src",
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertIn("download", result.stdout)
        self.assertIn("login", result.stdout)
