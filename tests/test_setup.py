from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.setup import ROOT, ensure_local_file


class SetupTests(unittest.TestCase):
    def test_local_file_is_created_once_and_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            path = Path(directory) / "global.js"

            ensure_local_file(path, "initial\n")
            ensure_local_file(path, "replacement\n")

            self.assertEqual(path.read_text(encoding="utf-8"), "initial\n")


if __name__ == "__main__":
    unittest.main()
