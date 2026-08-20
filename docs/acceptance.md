# VPS acceptance runbook

Run this only after receiving authorization to modify the target VPS. Infrastructure onboarding must already be complete.

## Install the primary server first

1. Copy a verified release archive and its `.sha256` file to the primary server.
2. Verify the checksum before extracting it.
3. Install the program with `sudo ./installers/install.sh --public-url https://preview-primary.example.com`.
4. As the agent-running user, install skills with `./installers/install-skill.py --harness all`.
5. Run `agent-preview check --json` and confirm the tunnel is active and the port is free.

## Exercise the lifecycle

1. Start the Python example and authenticate at the returned public URL.
2. Exercise navigation, a form POST, cookies, redirects, streaming, SSE, and WebSockets with the appropriate fixtures.
3. While it is running, request another preview. Confirm the agent reports the occupied-port message and waits.
4. Choose **Keep existing process**. Confirm the first application remains available and the second is not launched.
5. Repeat and choose **Kill process and replace it**. Confirm graceful termination precedes the replacement.
6. Stop the preview and confirm `systemctl is-active cloudflared.service` still reports `active`.
7. Verify the public hostname returns the expected no-origin response after the preview stops.

Repeat on the secondary server with its own configured public URL only after the primary server passes. Do not copy the primary server's configuration file to the secondary server.
