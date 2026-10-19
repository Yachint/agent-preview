from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "installers" / "install-skill.py"
SPEC = importlib.util.spec_from_file_location("install_skill", SCRIPT)
assert SPEC and SPEC.loader
install_skill = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(install_skill)


class InstallerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = Path(__file__).resolve().parents[1] / "skills" / "agent-preview"

    def test_cursor_symlink_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            args = ["--harness", "cursor", "--home", str(home), "--source", str(self.source)]
            self.assertEqual(install_skill.main(args), 0)
            self.assertEqual(install_skill.main(args), 0)
            target = home / ".cursor" / "skills" / "agent-preview"
            self.assertTrue(target.is_symlink())
            self.assertEqual(target.resolve(), self.source.resolve())

    def test_all_uses_shared_agents_path_without_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            self.assertEqual(
                install_skill.main(
                    ["--harness", "all", "--home", str(home), "--source", str(self.source)]
                ),
                0,
            )
            self.assertTrue((home / ".agents" / "skills" / "agent-preview").is_symlink())
            self.assertTrue((home / ".claude" / "skills" / "agent-preview").is_symlink())
            grok = home / ".grok" / "skills" / "agent-preview"
            self.assertTrue(grok.is_symlink())
            self.assertEqual(grok.resolve(), self.source.resolve())
            self.assertFalse((home / ".cursor" / "skills" / "agent-preview").exists())
            self.assertFalse((home / ".config" / "opencode" / "skills" / "agent-preview").exists())

    def test_grok_symlink_install_and_uninstall(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            args = ["--harness", "grok", "--home", str(home), "--source", str(self.source)]
            self.assertEqual(install_skill.main(args), 0)
            self.assertEqual(install_skill.main(args), 0)
            target = home / ".grok" / "skills" / "agent-preview"
            self.assertTrue(target.is_symlink())
            self.assertEqual(target.resolve(), self.source.resolve())
            self.assertEqual(install_skill.main(args + ["--uninstall"]), 0)
            self.assertFalse(target.exists())
            self.assertEqual(install_skill.main(args + ["--uninstall"]), 0)

    def test_copy_uninstall_requires_marker(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            args = [
                "--harness",
                "opencode",
                "--home",
                str(home),
                "--source",
                str(self.source),
                "--copy",
            ]
            self.assertEqual(install_skill.main(args), 0)
            target = home / ".config" / "opencode" / "skills" / "agent-preview"
            self.assertTrue((target / install_skill.MARKER).is_file())
            self.assertEqual(install_skill.main(args + ["--uninstall"]), 0)
            self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
