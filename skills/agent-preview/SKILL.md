---
name: agent-preview
description: Expose, preview, serve, or temporarily host an interactive local web application through the server's preconfigured authenticated Agent Preview URL. Use for development servers and interactive pages; do not use for non-HTTP services or permanent production deployment.
---

# Agent Preview

Use the installed `agent-preview` CLI. The Cloudflare tunnel is permanent infrastructure: never stop, restart, reconfigure, uninstall, or read credentials for `cloudflared` during a preview workflow.

## Start or reuse a preview

1. Run `agent-preview status --json`.
2. Inspect the project's documentation and package scripts to identify its normal development command.
3. Run `agent-preview check --json`.
4. Configure the application with its native flags so it listens on the reported host and port. Prefer strict-port behavior. Read [frameworks.md](references/frameworks.md) when framework-specific flags, trusted-host settings, redirects, cookies, WebSockets, or CORS matter.
5. Run the command through `agent-preview run -- <command...>`. Pass arguments directly; do not construct an evaluated shell string.
6. Report the returned HTTPS URL and mention that Cloudflare authentication may appear.
7. Leave the detached preview running while the user needs it. Stop it only when the user explicitly says the preview is finished.

Do not serve a home directory, repository root, `.git`, credentials, environment files, a terminal, an unrestricted upload directory, an admin console, or a debug endpoint without warning the user and obtaining explicit approval.

## Occupied preview port

If `check` or `run` reports an occupied port, do not kill, stop, replace, register, or launch anything yet. Tell the user exactly:

```text
There is already a process being served on the agent-preview port.
```

Show every available listener detail from `status --json`: PID, UID, command, executable, working directory, process start time, bind address, ownership, public URL, and state-metadata status.

Then ask the user to choose exactly one:

1. **Kill process and replace it** — terminate the reported listener and launch the newly requested command.
2. **Keep existing process** — leave it running, register it when necessary, return its public URL, and cancel the newly requested launch.

If the response is absent or ambiguous, take no action.

- For **Kill process and replace it**, copy the exact `pid` and `start_time` reported for the verified loopback listener and run `agent-preview replace --pid PID --start-time TICKS -- <command...>`. Do not add `--force` unless graceful replacement failed and the user separately approves forced termination.
- For **Keep existing process**, run `agent-preview register --pid PID` only for the exact verified listener. If status already shows valid `launched` ownership, do not rewrite it; report the existing URL.
- If PID, UID, start time, or socket ownership is unavailable, explain that the process cannot be safely killed or registered. Do not work around the refusal with `kill`, `pkill`, `fuser`, port matching, or command-name matching.

## Stop and logs

- Use `agent-preview logs` or `agent-preview logs --follow`; do not paste logs containing credentials into chat.
- Use `agent-preview stop` for a process launched by Agent Preview.
- When ownership is `registered`, explain that it was started outside Agent Preview and obtain explicit confirmation before using `agent-preview stop --include-registered`.
- Use `--force` only after graceful termination failed and the user explicitly approves forced termination.
