from __future__ import annotations

import importlib.util
from pathlib import Path
import tarfile
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build_release.py"
SPEC = importlib.util.spec_from_file_location("build_release", SCRIPT)
assert SPEC and SPEC.loader
build_release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build_release)


class ReleaseTests(unittest.TestCase):
    def test_release_is_reproducible(self) -> None:
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            archive_a, checksum_a = build_release.build(Path(first), 0)
            archive_b, checksum_b = build_release.build(Path(second), 0)
            self.assertEqual(archive_a.read_bytes(), archive_b.read_bytes())
            self.assertEqual(checksum_a.read_text(), checksum_b.read_text())
            with tarfile.open(archive_a, "r:gz") as bundle:
                self.assertIn("agent-preview-v0.1.0/LICENSE", bundle.getnames())


if __name__ == "__main__":
    unittest.main()
