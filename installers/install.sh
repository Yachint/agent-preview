#!/bin/sh
set -eu

PREFIX=/usr/local
PUBLIC_URL=
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_DIR=$(dirname -- "$SCRIPT_DIR")

usage() {
  echo "usage: install.sh [--prefix PATH] [--public-url HTTPS_URL]" >&2
  exit 2
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --prefix) [ "$#" -ge 2 ] || usage; PREFIX=$2; shift 2 ;;
    --public-url) [ "$#" -ge 2 ] || usage; PUBLIC_URL=$2; shift 2 ;;
    *) usage ;;
  esac
done

DESTDIR=${DESTDIR:-}
BIN_DIR=$DESTDIR$PREFIX/bin
LIB_DIR=$DESTDIR$PREFIX/lib/agent-preview
SHARE_DIR=$DESTDIR$PREFIX/share/agent-preview
CONFIG_DIR=$DESTDIR/etc
CONFIG_PATH=$CONFIG_DIR/agent-preview.conf

if [ -n "$PUBLIC_URL" ]; then
  python3 -c '
import sys
import ipaddress
import re
from urllib.parse import urlsplit
try:
    value = urlsplit(sys.argv[1])
    port = value.port
except ValueError:
    raise SystemExit("install.sh: invalid --public-url")
if (value.scheme != "https" or not value.hostname or value.username is not None
        or value.password is not None or port not in (None, 443)
        or value.path not in ("", "/") or value.query or value.fragment):
    raise SystemExit("install.sh: --public-url must be an HTTPS origin without credentials, path, query, or fragment")
try:
    ipaddress.ip_address(value.hostname)
except ValueError:
    pass
else:
    raise SystemExit("install.sh: --public-url must use a hostname, not an IP address")
labels = value.hostname.rstrip(".").split(".")
label_re = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
if not value.hostname or len(value.hostname.rstrip(".")) > 253 or not all(label_re.fullmatch(label) for label in labels):
    raise SystemExit("install.sh: --public-url has an invalid DNS hostname")
' "$PUBLIC_URL"
fi

install -d -m 0755 "$BIN_DIR" "$LIB_DIR/agent_preview" "$SHARE_DIR/skills/agent-preview/references" "$CONFIG_DIR"
install -m 0755 "$PROJECT_DIR/bin/agent-preview" "$BIN_DIR/agent-preview"
install -m 0644 "$PROJECT_DIR/lib/agent_preview/__init__.py" "$LIB_DIR/agent_preview/__init__.py"
install -m 0644 "$PROJECT_DIR/lib/agent_preview/cli.py" "$LIB_DIR/agent_preview/cli.py"
install -m 0644 "$PROJECT_DIR/skills/agent-preview/SKILL.md" "$SHARE_DIR/skills/agent-preview/SKILL.md"
install -m 0644 "$PROJECT_DIR/skills/agent-preview/references/frameworks.md" "$SHARE_DIR/skills/agent-preview/references/frameworks.md"

if [ ! -e "$CONFIG_PATH" ]; then
  [ -n "$PUBLIC_URL" ] || {
    echo "install.sh: --public-url is required when $CONFIG_PATH does not exist" >&2
    exit 2
  }
  umask 022
  {
    echo 'AGENT_PREVIEW_HOST=127.0.0.1'
    echo 'AGENT_PREVIEW_PORT=4317'
    echo "AGENT_PREVIEW_PUBLIC_URL=$PUBLIC_URL"
    echo 'AGENT_PREVIEW_TUNNEL_SERVICE=cloudflared.service'
  } > "$CONFIG_PATH"
  chmod 0644 "$CONFIG_PATH"
else
  echo "Preserved existing configuration: $CONFIG_PATH"
fi

echo "Installed agent-preview under $DESTDIR$PREFIX"
