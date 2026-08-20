# Generic HTTP server

Run a foreground command with explicit loopback and strict-port controls:

```bash
agent-preview run -- your-server --host 127.0.0.1 --port 4317
```

The command must remain attached to its process group. Self-daemonizing servers are intentionally unsupported.
