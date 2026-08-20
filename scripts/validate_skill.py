#!/usr/bin/env python3
"""Minimal dependency-free validation for the portable Agent Skill."""

from pathlib import Path
import re
import sys


SKILL = Path(__file__).resolve().parents[1] / "skills" / "agent-preview" / "SKILL.md"
NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def main() -> int:
    text = SKILL.read_text(encoding="utf-8")
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        print("invalid or missing YAML frontmatter", file=sys.stderr)
        return 1
    frontmatter = text.split("---\n", 2)[1]
    fields: dict[str, str] = {}
    for line in frontmatter.splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            fields[key.strip()] = value.strip()
    name = fields.get("name", "")
    description = fields.get("description", "")
    if not NAME_RE.fullmatch(name) or name != SKILL.parent.name:
        print("skill name is invalid or does not match its directory", file=sys.stderr)
        return 1
    if not description or len(description) > 1024:
        print("skill description is missing or too long", file=sys.stderr)
        return 1
    reference = SKILL.parent / "references" / "frameworks.md"
    if not reference.is_file():
        print("referenced framework guide is missing", file=sys.stderr)
        return 1
    print(f"Validated {SKILL}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
