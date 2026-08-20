#!/bin/sh
set -eu

PREFIX=/usr/local
if [ "${1:-}" = "--prefix" ]; then
  [ "$#" -eq 2 ] || { echo "usage: uninstall.sh [--prefix PATH]" >&2; exit 2; }
  PREFIX=$2
elif [ "$#" -ne 0 ]; then
  echo "usage: uninstall.sh [--prefix PATH]" >&2
  exit 2
fi

DESTDIR=${DESTDIR:-}
rm -f -- "$DESTDIR$PREFIX/bin/agent-preview"
rm -f -- "$DESTDIR$PREFIX/lib/agent-preview/agent_preview/__init__.py"
rm -f -- "$DESTDIR$PREFIX/lib/agent-preview/agent_preview/cli.py"
rm -f -- "$DESTDIR$PREFIX/share/agent-preview/skills/agent-preview/SKILL.md"
rm -f -- "$DESTDIR$PREFIX/share/agent-preview/skills/agent-preview/references/frameworks.md"
rmdir -- "$DESTDIR$PREFIX/lib/agent-preview/agent_preview" 2>/dev/null || true
rmdir -- "$DESTDIR$PREFIX/lib/agent-preview" 2>/dev/null || true
rmdir -- "$DESTDIR$PREFIX/share/agent-preview/skills/agent-preview/references" 2>/dev/null || true
rmdir -- "$DESTDIR$PREFIX/share/agent-preview/skills/agent-preview" 2>/dev/null || true
rmdir -- "$DESTDIR$PREFIX/share/agent-preview/skills" 2>/dev/null || true
rmdir -- "$DESTDIR$PREFIX/share/agent-preview" 2>/dev/null || true
echo "Removed Agent Preview program files. /etc/agent-preview.conf was preserved."
