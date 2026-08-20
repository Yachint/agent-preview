from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "installers" / "install.sh"


class ProgramInstallerTests(unittest.TestCase):
    def test_staged_install_and_upgrade_preserve_config(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            destination = Path(raw)
            environment = os.environ.copy()
            environment["DESTDIR"] = str(destination)
            first = subprocess.run(
                [str(INSTALLER), "--public-url", "https://server.example.com"],
                cwd=ROOT,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            config = destination / "etc" / "agent-preview.conf"
            original = config.read_text(encoding="utf-8")
            second = subprocess.run(
                [str(INSTALLER), "--public-url", "https://different.example.com"],
                cwd=ROOT,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(config.read_text(encoding="utf-8"), original)
            installed = destination / "usr" / "local" / "bin" / "agent-preview"
            version = subprocess.run(
                [str(installed), "--version"], text=True, capture_output=True, check=False
            )
            self.assertEqual(version.returncode, 0, version.stderr)
            self.assertIn("0.1.1", version.stdout)

    def test_invalid_public_url_fails_before_installing(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            destination = Path(raw)
            environment = os.environ.copy()
            environment["DESTDIR"] = str(destination)
            result = subprocess.run(
                [str(INSTALLER), "--public-url", "https://bad_host.example.com"],
                cwd=ROOT,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((destination / "usr" / "local" / "bin" / "agent-preview").exists())


if __name__ == "__main__":
    unittest.main()
