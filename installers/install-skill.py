#!/usr/bin/env python3
"""Install the canonical Agent Preview skill for supported agent harnesses."""

from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import sys


MARKER = ".agent-preview-managed"


def repository_skill() -> Path:
    return Path(__file__).resolve().parents[1] / "skills" / "agent-preview"


def destinations(home: Path, harness: str) -> list[Path]:
    mapping = {
        "claude": home / ".claude" / "skills" / "agent-preview",
        # Codex's current user-level Agent Skills directory. Cursor and OpenCode
        # also recognize it, so `all` does not duplicate their private paths.
        # Grok Build's native user directory is separate and is included in `all`.
        "codex": home / ".agents" / "skills" / "agent-preview",
        "opencode": home / ".config" / "opencode" / "skills" / "agent-preview",
        "cursor": home / ".cursor" / "skills" / "agent-preview",
        "grok": home / ".grok" / "skills" / "agent-preview",
    }
    if harness == "all":
        return [mapping["codex"], mapping["claude"], mapping["grok"]]
    return [mapping[harness]]


def is_managed_copy(path: Path) -> bool:
    return path.is_dir() and not path.is_symlink() and (path / MARKER).is_file()


def install_one(source: Path, target: Path, copy: bool) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        if target.resolve() == source.resolve() and not copy:
            print(f"Already installed: {target}")
            return
        raise RuntimeError(f"refusing to replace an unmanaged or differently targeted symlink: {target}")
    if target.exists():
        if not is_managed_copy(target):
            raise RuntimeError(f"refusing to replace an unmanaged destination: {target}")
        if not copy:
            raise RuntimeError(f"managed copy exists; rerun with --copy or uninstall it first: {target}")
        shutil.rmtree(target)
    if copy:
        shutil.copytree(source, target)
        (target / MARKER).write_text("managed by agent-preview\n", encoding="utf-8")
    else:
        target.symlink_to(source, target_is_directory=True)
    print(f"Installed: {target}")


def uninstall_one(source: Path, target: Path) -> None:
    if target.is_symlink():
        if target.resolve() != source.resolve():
            raise RuntimeError(f"refusing to remove a differently targeted symlink: {target}")
        target.unlink()
        print(f"Removed: {target}")
        return
    if is_managed_copy(target):
        shutil.rmtree(target)
        print(f"Removed: {target}")
        return
    if target.exists():
        raise RuntimeError(f"refusing to remove an unmanaged destination: {target}")
    print(f"Not installed: {target}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="install-skill.py")
    parser.add_argument(
        "--harness",
        choices=("claude", "codex", "opencode", "cursor", "grok", "all"),
        required=True,
    )
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument("--source", type=Path)
    parser.add_argument("--copy", action="store_true", help="copy instead of symlinking")
    parser.add_argument("--uninstall", action="store_true")
    args = parser.parse_args(argv)

    source = (args.source or Path("/usr/local/share/agent-preview/skills/agent-preview")).expanduser()
    if not source.is_dir() and args.source is None:
        fallback = repository_skill()
        if fallback.is_dir():
            source = fallback
    if not source.is_dir():
        parser.error(f"canonical skill directory does not exist: {source}")
    home = args.home.expanduser()
    try:
        for target in destinations(home, args.harness):
            if args.uninstall:
                uninstall_one(source, target)
            else:
                install_one(source, target, args.copy)
    except (OSError, RuntimeError) as exc:
        print(f"install-skill: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
