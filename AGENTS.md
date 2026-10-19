# Agent Preview contributor guide

This file applies to the entire repository. Agent Preview is a Linux-only,
standard-library Python 3 CLI plus a provider-neutral Agent Skill. It lets an
agent place one temporary HTTP application behind an existing, permanent
Cloudflare Tunnel without managing Cloudflare itself.

## Architecture

```text
agent harness
  -> agent-preview CLI
  -> application on 127.0.0.1:4317
  -> existing cloudflared.service
  -> Cloudflare Access
  -> configured HTTPS hostname
```

The fixed endpoint policy comes from the root-owned
`/etc/agent-preview.conf`. Runtime state belongs to the invoking user and is
stored under `/run/user/<uid>/agent-preview`, with a private `/tmp` fallback.
The CLI uses Linux `/proc` to prove process, socket, UID, start-time, and
process-group ownership before it registers or signals anything.

## Repository map

- `bin/agent-preview`: installed executable wrapper.
- `lib/agent_preview/cli.py`: CLI, configuration, procfs inspection, state,
  readiness, lifecycle, and safety logic.
- `lib/agent_preview/__init__.py`: package version.
- `skills/agent-preview/`: canonical provider-neutral skill and framework
  reference.
- `installers/install.sh`: root program/configuration installer.
- `installers/install-skill.py`: per-user Claude, Codex, OpenCode, Cursor, and
  Grok Build skill adapter installer.
- `installers/uninstall.sh`: ownership-aware program removal; it intentionally
  preserves `/etc/agent-preview.conf`.
- `examples/`: configuration and framework command examples.
- `tests/`: unit, installer, release, and Linux integration coverage.
- `scripts/validate_skill.py`: portable skill validation.
- `scripts/build_release.py`: deterministic archive and SHA-256 generation.
- `.github/workflows/ci.yml`: AMD64 and ARM64-compatible Linux CI.
- `docs/acceptance.md`: approval-gated VPS acceptance procedure.
- `CLAUDE.md`: Claude-compatible reference to this guide.
- `MEMORY.md`: chronological decisions and their rationale.

## Non-negotiable safety invariants

- Never start, stop, restart, reconfigure, uninstall, or read credentials for
  `cloudflared`. Tunnel health checks are diagnostic only.
- Never terminate a process merely because it uses port 4317, has a familiar
  command name, or appears in stale metadata.
- An occupied port must be reported before launching. The user must explicitly
  choose either **Kill process and replace it** or **Keep existing process**.
- Before signaling, revalidate the same UID, PID start time, exact loopback
  listener, socket ownership, process identity, and, where applicable, PGID.
- Signal only same-UID processes. Registered processes require
  `stop --include-registered` after explicit user confirmation.
- Graceful replacement sends `SIGTERM`. Do not use `SIGKILL` unless the user
  separately approved force and `--force` was supplied.
- Accept only `127.0.0.1` listeners. Reject wildcard, public, and other
  interface bindings.
- Execute application commands as argument arrays with no shell, `eval`, or
  command interpolation.
- Preserve configuration and runtime file ownership, mode, no-follow, locking,
  and atomic-write protections.
- Treat Linux zombie (`Z`) and dead (`X`) procfs states as exited processes.

## Development workflow

Use Python 3.10 or newer and do not add third-party runtime dependencies.
Preserve stable exit codes and JSON field compatibility unless a deliberate
versioned change is documented.

Run before every commit:

```bash
python3 -m compileall -q bin lib installers scripts tests
PYTHONPATH=lib python3 -m unittest discover -s tests -v
python3 scripts/validate_skill.py
python3 scripts/build_release.py
git diff --check
```

The two real procfs lifecycle tests skip on non-Linux hosts; both must pass in
Linux CI. Add regression coverage for safety fixes, including negative paths
that prove no signal was sent.

When changing user-facing occupied-port behavior, update the CLI, canonical
skill, README, and tests together. Keep `SKILL.md` frontmatter portable and
provider-neutral. All harness adapters must continue to use the same canonical
skill content and refuse unmanaged destination collisions.

## Privacy and release hygiene

- Do not commit credentials, tokens, tunnel IDs, personal email addresses,
  machine IPs, private hostnames, actual server names, or production URLs.
- Use `server.example.com`, generic primary/secondary server labels, and
  loopback addresses in committed examples.
- Inspect the staged snapshot, not only the working tree, before publishing.
- Keep generated archives, caches, logs, PID files, state, virtual
  environments, and local configuration out of Git.
- Build releases deterministically, verify their SHA-256 files, inspect archive
  paths and contents, and download published assets once to compare them with
  the local artifacts.
- Existing tags and releases are immutable. Bump the package version for the
  next release instead of moving or replacing a published tag.

Do not modify a VPS, GitHub release, Cloudflare configuration, or other
external system without explicit authorization for that action.
