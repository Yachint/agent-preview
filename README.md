# Agent Preview

Agent Preview lets terminal-based coding agents present temporary HTTP applications through an already-onboarded, Cloudflare Access-protected VPS hostname. It manages the local application process only. It does not create or modify tunnels, DNS, Access policies, firewall rules, or Cloudflare credentials.

```text
browser → Cloudflare Access → named tunnel → http://127.0.0.1:4317
```

## Requirements

- Linux with `/proc` and systemd
- Python 3.10 or newer; no third-party Python packages
- A healthy `cloudflared.service`
- A published tunnel route to `http://127.0.0.1:4317`
- A root-owned `/etc/agent-preview.conf`

## Install from a release

Verify the release checksum before extracting the archive:

```bash
sha256sum -c agent-preview-v0.1.1.tar.gz.sha256
tar -xzf agent-preview-v0.1.1.tar.gz
cd agent-preview-v0.1.1
sudo ./installers/install.sh --public-url https://server.example.com
./installers/install-skill.py --harness all
```

The program is installed under `/usr/local`; the canonical skill is stored at `/usr/local/share/agent-preview/skills/agent-preview`. An existing `/etc/agent-preview.conf` is preserved during upgrades.

`--harness all` installs the shared Agent Skills package at `~/.agents/skills/agent-preview` for Codex, OpenCode, and Cursor, plus `~/.claude/skills/agent-preview` for Claude Code. Native single-harness targets are also available:

```bash
./installers/install-skill.py --harness claude
./installers/install-skill.py --harness codex
./installers/install-skill.py --harness opencode
./installers/install-skill.py --harness cursor
```

Symlinks are used by default. Add `--copy` when the harness environment cannot follow them. The installer refuses to overwrite unmanaged destinations.

## Configuration

The endpoint is administrator policy and cannot be changed by ordinary CLI flags or environment variables:

```text
AGENT_PREVIEW_HOST=127.0.0.1
AGENT_PREVIEW_PORT=4317
AGENT_PREVIEW_PUBLIC_URL=https://server.example.com
AGENT_PREVIEW_TUNNEL_SERVICE=cloudflared.service
```

The file must be root-owned, regular, non-symlinked, and not group/world-writable. Values are parsed literally and never sourced by a shell.

## Commands

```bash
agent-preview check
agent-preview run -- npm run dev -- --host 127.0.0.1 --port 4317 --strictPort
agent-preview status --json
agent-preview logs --follow
agent-preview stop
```

`run` is detached by default. Use `--foreground` when the invoking terminal should wait for the application. The child receives `HOST`, `PORT`, `AGENT_PREVIEW_HOST`, `AGENT_PREVIEW_PORT`, and `AGENT_PREVIEW_PUBLIC_URL`.

### Occupied port

`run` never kills an existing listener. It reports:

```text
There is already a process being served on the agent-preview port.
```

After inspecting the reported identity, the user may explicitly choose:

1. **Kill process and replace it**:

   ```bash
   agent-preview replace --pid PID --start-time TICKS -- new-command
   ```

2. **Keep existing process**:

   ```bash
   agent-preview register --pid PID
   ```

Replacement rechecks PID, UID, process start time, loopback socket ownership, and bind address before sending `SIGTERM`. It does not escalate to `SIGKILL` without a separate `--force`. Registration leaves the listener running; externally started processes require `stop --include-registered` before Agent Preview may terminate them.

## Runtime model

Private state and logs live under `/run/user/<uid>/agent-preview`, with a validated `/tmp/agent-preview-<uid>` fallback. PID identity includes the Linux process start time and process group so PID reuse does not authorize a signal. Applications listening on wildcard or public addresses are rejected.

Stable exit codes are: `0` success, `1` runtime failure, `2` usage/configuration error, `3` occupied port, `4` ownership/safety refusal, and `5` readiness/tunnel-health failure.

## Development

```bash
PYTHONPATH=lib python3 -m unittest discover -s tests -v
python3 scripts/validate_skill.py
python3 /path/to/skill-creator/scripts/quick_validate.py skills/agent-preview
python3 scripts/build_release.py
```

Linux integration tests exercise real `/proc` socket ownership and are skipped on other operating systems. The repository does not need network access for its test suite.

## Removal

Remove harness links first, then program files:

```bash
./installers/install-skill.py --harness all --uninstall
sudo ./installers/uninstall.sh
```

Uninstall preserves `/etc/agent-preview.conf`. Remove that file separately only when intentionally offboarding the server.

See [VPS acceptance](docs/acceptance.md) for the approval-gated, primary-server-first rollout procedure.

## License

Agent Preview is available under the [Apache License 2.0](LICENSE).
