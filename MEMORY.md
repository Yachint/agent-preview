# Agent Preview project memory

This is a chronological record of durable project decisions. It explains why
the repository is shaped as it is; current contributor instructions live in
`AGENTS.md`.

## 2026-08-20: v1 architecture selected

- The project manages one temporary HTTP origin per Linux VPS while treating
  the existing Cloudflare Tunnel and Access application as permanent,
  read-only infrastructure.
- The implementation is a dependency-free Python 3 CLI targeting systemd
  Linux, `/proc`, AMD64 Debian-class systems, and ARM64 Ubuntu-class systems.
- The endpoint is administrator policy in `/etc/agent-preview.conf`, fixed to
  `127.0.0.1:4317` with a per-server HTTPS public URL. Environment variables
  and ordinary CLI flags cannot override it.
- Configuration is parsed as strict literal `KEY=value` data rather than shell
  syntax. Unknown/duplicate fields, unsafe permissions, symlinks, wildcard
  hosts, IP-based public URLs, credentials, and unsafe systemd unit names are
  rejected.

## 2026-08-20: process ownership and lifecycle safety

- Runtime state is private per user, locked, written atomically, and guarded
  against symlinks and ownership changes.
- Process identity is PID plus UID, Linux start-time ticks, PGID, and listener
  socket ownership. This prevents PID reuse, stale metadata, and port-only
  termination from authorizing a signal.
- Commands run as argument arrays in new process groups. Readiness requires an
  HTTP response from the local origin and proof that the listener belongs to
  the launched group.
- Applications must bind the configured loopback address. Wildcard, public,
  other-interface, self-daemonized, or unverifiable listeners are refused.
- `cloudflared.service` is never managed by Agent Preview; an inactive service
  produces diagnostics and exit code 5 only.

## 2026-08-20: occupied-port interaction finalized

- `run` never kills an existing listener automatically. It first reports:
  “There is already a process being served on the agent-preview port.”
- The user-facing choices are exactly **Kill process and replace it** and
  **Keep existing process**.
- Keeping preserves valid launched ownership or registers a verified external
  listener, cancels the new launch, and returns the existing public URL.
- Replacement revalidates identity immediately before `SIGTERM`. Failure to
  stop gracefully requires separate force approval; there is no implicit
  `SIGKILL`.
- Registered listeners require explicit `--include-registered` confirmation
  before a later stop.

## 2026-08-20: one canonical Agent Skill

- The canonical skill is provider-neutral and stored in
  `skills/agent-preview/`.
- Claude Code, Codex, OpenCode, and Cursor CLI adapters link or copy that same
  content. Installers are idempotent and refuse to overwrite unmanaged paths.
- The skill checks status and tunnel health, discovers a framework's native
  loopback/strict-port command, handles an occupied port interactively, starts
  through the CLI, reports the HTTPS URL and possible Cloudflare login, and
  keeps the preview alive until the user explicitly finishes.

## 2026-08-20: public repository and licensing

- The project was made public after scanning the entire initial snapshot for
  credentials, personal identifiers, operational hostnames, and private URLs.
- Deployment documentation uses generic primary/secondary server labels and
  reserved `example.com` hostnames so the code is not tied to personal
  infrastructure.
- Apache License 2.0 was selected for its permissive terms and explicit patent
  grant. Commit and tag metadata use a GitHub noreply address.
- Generated artifacts, runtime data, local configuration, logs, caches, and
  environment files are excluded from Git.

## 2026-08-20: initial CI failure and Linux fix

- The first AMD64 and ARM64 CI run exposed a Linux-only shutdown bug: a child
  that had received `SIGTERM` could remain in `/proc` as a zombie until its
  parent reaped it, and the inspector incorrectly considered that task alive.
- `ProcInspector` now treats procfs states `Z` (zombie) and `X` (dead) as
  exited. Regression fixtures verify live identity parsing and both terminal
  states.
- Commit `750fae8` passed the complete AMD64 and ARM64 matrix, including tests,
  skill validation, and release generation.

## 2026-08-20: v0.1.0 published

- Annotated tag `v0.1.0` points to commit `750fae8` and is not to be moved.
- The deterministic archive includes the Apache license and has SHA-256:
  `13a0cc33b83ade64704a2bc9ea80f877a4a0cb96d2a3c6a2a14ac0ffb74a9f44`.
- Both release assets were downloaded after publication, checksum-verified,
  and compared byte-for-byte with the locally verified artifacts.
- The next operational milestone is a controlled installation and acceptance
  test on a primary VPS, followed by a secondary VPS only after the first
  passes. Each server must use its own configuration; no configuration file is
  copied between machines.

## 2026-08-20: repository agent context added

- `AGENTS.md` became the repository-wide source of architecture, structure,
  safety, development, privacy, and release instructions for future agents.
- `CLAUDE.md` contains only `@AGENTS.md`, avoiding a second provider-specific
  copy that could drift from the canonical guidance.
- `MEMORY.md` records durable decisions chronologically, while actionable
  current rules remain in `AGENTS.md`.

## 2026-08-20: v0.1.1 occupied-port guidance tightened

- The first real-agent acceptance test passed discovery, launch, status, logs,
  graceful replacement, stop, and tunnel-isolation checks on an ARM64 VPS.
- During the collision interaction, the agent added a third “Do nothing”
  choice, abbreviated **Keep existing process**, and created content beneath
  the running server's document root before the user chose. The CLI remained
  safe, but the skill did not follow the required two-choice contract.
- The canonical workflow now checks status and port health before inspecting
  or modifying a requested preview, forbids exposing new content beneath a
  running preview, and requires exactly the two complete, ordered labels with
  no recommendations or alternatives.
- Static contract tests protect the wording and workflow order. Release
  archives now also include `AGENTS.md`, `CLAUDE.md`, and `MEMORY.md`.

## 2026-08-20: v0.1.1 published and primary server upgraded

- Annotated tag `v0.1.1` points to commit `3bce706` and is immutable. AMD64 and
  ARM64 CI passed before publication.
- The published deterministic archive has SHA-256
  `9716aff705e535fbcdeb3dbebecd144bad95b407813c2fa9b4d7b3b54795fe3e`.
  Published assets were downloaded, checksum-verified, and compared
  byte-for-byte with the locally tested artifacts.
- The primary ARM64 VPS was upgraded in place from `v0.1.0` to `v0.1.1` using
  the release archive. Its existing root-owned endpoint configuration was
  preserved, and its harness adapters continued resolving to the updated
  canonical skill under `/usr/local/share/agent-preview`.
- Post-upgrade checks confirmed version `0.1.1`, a free loopback preview port,
  absent runtime metadata, valid configuration, and an active tunnel service.
  No preview process needed to be stopped during the upgrade.
- This confirms the release installation and upgrade path only. Full
  `v0.1.1` agent-behavior acceptance—including both strict occupied-port
  choices—remains pending, as does rollout to the secondary VPS.
