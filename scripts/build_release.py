#!/usr/bin/env python3
"""Build a reproducible Agent Preview release archive and checksum."""

from __future__ import annotations

import argparse
import gzip
import hashlib
from pathlib import Path
import re
import tarfile


ROOT = Path(__file__).resolve().parents[1]
INCLUDED = (
    ".github",
    ".gitattributes",
    ".gitignore",
    "AGENTS.md",
    "CLAUDE.md",
    "LICENSE",
    "MEMORY.md",
    "README.md",
    "bin",
    "docs",
    "examples",
    "installers",
    "lib",
    "scripts",
    "skills",
    "tests",
)
EXCLUDED_PARTS = {"__pycache__", ".pytest_cache", ".DS_Store"}


def version() -> str:
    content = (ROOT / "lib" / "agent_preview" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__ = "([0-9]+\.[0-9]+\.[0-9]+)"$', content, re.MULTILINE)
    if not match:
        raise RuntimeError("could not read release version")
    return match.group(1)


def files() -> list[Path]:
    result: list[Path] = []
    for name in INCLUDED:
        path = ROOT / name
        if not path.exists():
            continue
        if path.is_file():
            result.append(path)
        else:
            result.extend(
                item
                for item in path.rglob("*")
                if item.is_file() and not any(part in EXCLUDED_PARTS for part in item.parts)
            )
    return sorted(set(result), key=lambda item: item.relative_to(ROOT).as_posix())


def normalized_info(source: Path, archive_name: str, epoch: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(archive_name)
    info.size = source.stat().st_size
    info.mtime = epoch
    info.uid = 0
    info.gid = 0
    info.uname = "root"
    info.gname = "root"
    executable = source.suffix in {".sh", ".py"} and source.parts[-2] in {"scripts", "installers"}
    if source.name == "agent-preview":
        executable = True
    info.mode = 0o755 if executable else 0o644
    return info


def build(output_directory: Path, epoch: int) -> tuple[Path, Path]:
    release_version = version()
    base = f"agent-preview-v{release_version}"
    output_directory.mkdir(parents=True, exist_ok=True)
    archive = output_directory / f"{base}.tar.gz"
    with archive.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=epoch) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as bundle:
                for source in files():
                    relative = source.relative_to(ROOT).as_posix()
                    info = normalized_info(source, f"{base}/{relative}", epoch)
                    with source.open("rb") as handle:
                        bundle.addfile(info, handle)
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    checksum_path = archive.with_suffix(archive.suffix + ".sha256")
    checksum_path.write_text(f"{checksum}  {archive.name}\n", encoding="ascii")
    return archive, checksum_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    parser.add_argument("--epoch", type=int, default=0)
    args = parser.parse_args()
    archive, checksum = build(args.output, args.epoch)
    print(archive)
    print(checksum)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
