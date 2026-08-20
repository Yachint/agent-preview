from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from agent_preview import cli


def write_config(directory: Path, **changes: str) -> Path:
    values = {
        "AGENT_PREVIEW_HOST": "127.0.0.1",
        "AGENT_PREVIEW_PORT": "4317",
        "AGENT_PREVIEW_PUBLIC_URL": "https://server.example.com",
        "AGENT_PREVIEW_TUNNEL_SERVICE": "cloudflared.service",
    }
    values.update(changes)
    path = directory / "agent-preview.conf"
    path.write_text("".join(f"{key}={value}\n" for key, value in values.items()), encoding="utf-8")
    path.chmod(0o644)
    return path


class ConfigTests(unittest.TestCase):
    def test_parses_strict_config(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = write_config(Path(raw))
            value = cli.load_config(path, require_root_owner=False)
            self.assertEqual(value.origin_url, "http://127.0.0.1:4317")
            self.assertEqual(value.public_url, "https://server.example.com")

    def test_rejects_unsafe_host(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = write_config(Path(raw), AGENT_PREVIEW_HOST="0.0.0.0")
            with self.assertRaises(cli.AgentPreviewError) as raised:
                cli.load_config(path, require_root_owner=False)
            self.assertEqual(raised.exception.exit_code, cli.EXIT_CONFIG)

    def test_rejects_shell_and_unknown_keys(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = write_config(Path(raw))
            with path.open("a", encoding="utf-8") as handle:
                handle.write("EVIL=$(touch /tmp/nope)\n")
            with self.assertRaisesRegex(cli.AgentPreviewError, "Unknown configuration key"):
                cli.load_config(path, require_root_owner=False)

    def test_rejects_group_writable_config(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = write_config(Path(raw))
            path.chmod(0o664)
            with self.assertRaisesRegex(cli.AgentPreviewError, "group/world-writable"):
                cli.load_config(path, require_root_owner=False)

    def test_rejects_malformed_https_url(self) -> None:
        invalid = (
            "http://server.example.com",
            "https://user@server.example.com",
            "https://server.example.com/path",
            "https://server.example.com:bad",
            "https://127.0.0.1",
            "https://bad_host.example.com",
            "https://bad\\host.example.com",
        )
        for value in invalid:
            with self.subTest(value=value), tempfile.TemporaryDirectory() as raw:
                path = write_config(Path(raw), AGENT_PREVIEW_PUBLIC_URL=value)
                with self.assertRaises(cli.AgentPreviewError):
                    cli.load_config(path, require_root_owner=False)

    def test_endpoint_environment_variables_do_not_override_policy(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            path = write_config(root)
            output = io.StringIO()
            with mock.patch.dict(
                os.environ,
                {
                    "AGENT_PREVIEW_HOST": "0.0.0.0",
                    "AGENT_PREVIEW_PORT": "9999",
                    "AGENT_PREVIEW_PUBLIC_URL": "https://wrong.example.com",
                },
            ), contextlib.redirect_stdout(output):
                result = cli.main(
                    ["status", "--json"],
                    config_path=path,
                    runtime_base=root / "runtime",
                    inspector=FakeInspector({}, []),
                    tunnel_checker=lambda _: True,
                    require_root_config=False,
                )
            value = json.loads(output.getvalue())
            self.assertEqual(result, cli.EXIT_OK)
            self.assertEqual(value["host"], "127.0.0.1")
            self.assertEqual(value["port"], 4317)
            self.assertEqual(value["public_url"], "https://server.example.com")


class ProcInspectorTests(unittest.TestCase):
    def write_process(self, root: Path, pid: int, state: str) -> None:
        process = root / str(pid)
        process.mkdir()
        fields = [state, "1", str(pid), *(["0"] * 16), "456789"]
        (process / "stat").write_text(
            f"{pid} (worker with spaces) {' '.join(fields)}\n",
            encoding="utf-8",
        )
        (process / "cmdline").write_bytes(b"python3\0server.py\0")

    def test_reads_live_process_identity(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self.write_process(root, 1234, "S")
            value = cli.ProcInspector(root).process(1234)
            self.assertIsNotNone(value)
            assert value is not None
            self.assertEqual(value.pid, 1234)
            self.assertEqual(value.pgid, 1234)
            self.assertEqual(value.start_time, 456789)
            self.assertEqual(value.command, ("python3", "server.py"))

    def test_treats_zombie_and_dead_processes_as_exited(self) -> None:
        for state in ("Z", "X"):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                self.write_process(root, 1234, state)
                self.assertIsNone(cli.ProcInspector(root).process(1234))


class RuntimeStoreTests(unittest.TestCase):
    def test_state_round_trip_is_private(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            runtime = Path(raw) / "runtime"
            store = cli.RuntimeStore(base=runtime)
            store.ensure()
            process = cli.ProcessInfo(os.getpid(), os.getuid(), os.getpgid(0), 123, ("test",), None, None)
            config = cli.Config("127.0.0.1", 4317, "https://server.example.com", "cloudflared.service", Path("/test"))
            value = cli._state_for_process(process, "registered", config)
            store.write(value)
            loaded, metadata = store.read()
            self.assertEqual(metadata, "present")
            self.assertEqual(loaded, value)
            self.assertEqual(stat.S_IMODE(runtime.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(store.state_path.stat().st_mode), 0o600)

    def test_rejects_symlink_state(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            store = cli.RuntimeStore(base=root / "runtime")
            store.ensure()
            target = root / "target"
            target.write_text("{}", encoding="utf-8")
            store.state_path.symlink_to(target)
            state, metadata = store.read()
            self.assertIsNone(state)
            self.assertEqual(metadata, "unsafe")


class ExitCodeTests(unittest.TestCase):
    def test_missing_config_is_exit_2(self) -> None:
        with tempfile.TemporaryDirectory() as raw, contextlib.redirect_stderr(io.StringIO()):
            result = cli.main(
                ["status"],
                config_path=Path(raw) / "missing",
                require_root_config=False,
            )
        self.assertEqual(result, cli.EXIT_CONFIG)

    def test_inactive_tunnel_is_exit_5(self) -> None:
        with tempfile.TemporaryDirectory() as raw, contextlib.redirect_stdout(io.StringIO()):
            root = Path(raw)
            result = cli.main(
                ["check"],
                config_path=write_config(root),
                runtime_base=root / "runtime",
                inspector=FakeInspector({}, []),
                tunnel_checker=lambda _: False,
                require_root_config=False,
            )
        self.assertEqual(result, cli.EXIT_READINESS)

    def test_unknown_listener_is_exit_3(self) -> None:
        process = cli.ProcessInfo(
            7171, os.getuid(), 7171, 42, ("server",), "/server", "/work"
        )
        listener = cli.Listener("127.0.0.1", 4317, 7171, process)
        with tempfile.TemporaryDirectory() as raw, contextlib.redirect_stdout(io.StringIO()):
            root = Path(raw)
            result = cli.main(
                ["check"],
                config_path=write_config(root),
                runtime_base=root / "runtime",
                inspector=FakeInspector({7171: process}, [listener]),
                tunnel_checker=lambda _: True,
                require_root_config=False,
            )
        self.assertEqual(result, cli.EXIT_CONFLICT)


class FakeInspector:
    def __init__(self, processes: dict[int, cli.ProcessInfo], listeners: list[cli.Listener]):
        self.processes = processes
        self.listener_values = listeners

    def process(self, pid: int) -> cli.ProcessInfo | None:
        return self.processes.get(pid)

    def listeners(self, port: int) -> list[cli.Listener]:
        return [item for item in self.listener_values if item.port == port]


class ConflictTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = cli.Config(
            "127.0.0.1", 4317, "https://server.example.com", "cloudflared.service", Path("/test")
        )
        self.process = cli.ProcessInfo(
            4242, os.getuid(), 4242, 987654, ("npm", "run", "dev"), "/usr/bin/npm", "/work/app"
        )
        self.listener = cli.Listener("127.0.0.1", 4317, 111, self.process)

    def test_run_reports_conflict_without_signaling(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            store = cli.RuntimeStore(base=Path(raw) / "runtime")
            inspector = FakeInspector({4242: self.process}, [self.listener])
            output = io.StringIO()
            with mock.patch("os.kill") as kill, mock.patch("os.killpg") as killpg, contextlib.redirect_stdout(output):
                result = cli.run_preview(
                    self.config, store, inspector, lambda _: True, ["new-server"], startup_timeout=0.1
                )
            self.assertEqual(result, cli.EXIT_CONFLICT)
            self.assertIn("There is already a process being served on the agent-preview port.", output.getvalue())
            self.assertIn("1. Kill process and replace it", output.getvalue())
            self.assertIn("2. Keep existing process", output.getvalue())
            kill.assert_not_called()
            killpg.assert_not_called()

    def test_keep_existing_process_registers_listener(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            store = cli.RuntimeStore(base=Path(raw) / "runtime")
            inspector = FakeInspector({4242: self.process}, [self.listener])
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = cli.register_process(self.config, store, inspector, 4242)
            state_value, metadata = store.read()
            self.assertEqual(result, cli.EXIT_OK)
            self.assertEqual(metadata, "present")
            self.assertEqual(state_value.ownership, "registered")
            self.assertIn("Keeping existing process", output.getvalue())

    def test_keep_existing_process_clears_only_stale_metadata(self) -> None:
        stale = cli.ProcessInfo(1111, os.getuid(), 1111, 1, ("old",), None, "/old")
        with tempfile.TemporaryDirectory() as raw:
            store = cli.RuntimeStore(base=Path(raw) / "runtime")
            store.ensure()
            store.write(cli._state_for_process(stale, "launched", self.config))
            inspector = FakeInspector({4242: self.process}, [self.listener])
            with mock.patch("os.kill") as kill, mock.patch("os.killpg") as killpg:
                cli.register_process(self.config, store, inspector, 4242)
            state_value, _ = store.read()
            self.assertEqual(state_value.pid, 4242)
            self.assertEqual(state_value.ownership, "registered")
            kill.assert_not_called()
            killpg.assert_not_called()

    def test_register_preserves_launched_ownership(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            store = cli.RuntimeStore(base=Path(raw) / "runtime")
            store.ensure()
            store.write(cli._state_for_process(self.process, "launched", self.config, command=self.process.command))
            inspector = FakeInspector({4242: self.process}, [self.listener])
            cli.register_process(self.config, store, inspector, 4242)
            state_value, _ = store.read()
            self.assertEqual(state_value.ownership, "launched")

    def test_replace_refuses_changed_identity_without_signaling(self) -> None:
        changed = cli.ProcessInfo(
            4242, os.getuid(), 4242, 999999, ("other",), "/usr/bin/other", "/work/other"
        )
        inspector = FakeInspector({4242: changed}, [cli.Listener("127.0.0.1", 4317, 222, changed)])
        with tempfile.TemporaryDirectory() as raw:
            store = cli.RuntimeStore(base=Path(raw) / "runtime")
            with mock.patch("os.kill") as kill, mock.patch("os.killpg") as killpg:
                with self.assertRaises(cli.AgentPreviewError) as raised:
                    cli.replace_process(
                        self.config,
                        store,
                        inspector,
                        lambda _: True,
                        4242,
                        987654,
                        [sys.executable, "-c", "pass"],
                        force=False,
                        grace_seconds=0.1,
                        detach=True,
                        startup_timeout=0.1,
                    )
            self.assertEqual(raised.exception.exit_code, cli.EXIT_SAFETY)
            kill.assert_not_called()
            killpg.assert_not_called()

    def test_register_refuses_different_uid(self) -> None:
        other = cli.ProcessInfo(
            5252, os.getuid() + 1, 5252, 111, ("server",), "/server", "/work"
        )
        inspector = FakeInspector({5252: other}, [cli.Listener("127.0.0.1", 4317, 333, other)])
        with tempfile.TemporaryDirectory() as raw:
            with self.assertRaises(cli.AgentPreviewError) as raised:
                cli.register_process(self.config, cli.RuntimeStore(base=Path(raw) / "runtime"), inspector, 5252)
        self.assertEqual(raised.exception.exit_code, cli.EXIT_SAFETY)

    def test_replace_checks_unsafe_state_before_signaling(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            store = cli.RuntimeStore(base=root / "runtime")
            store.ensure()
            target = root / "outside"
            target.write_text("{}", encoding="utf-8")
            store.state_path.symlink_to(target)
            inspector = FakeInspector({4242: self.process}, [self.listener])
            with mock.patch("os.kill") as kill, mock.patch("os.killpg") as killpg:
                with self.assertRaises(cli.AgentPreviewError) as raised:
                    cli.replace_process(
                        self.config,
                        store,
                        inspector,
                        lambda _: True,
                        4242,
                        987654,
                        [sys.executable, "-c", "pass"],
                        force=False,
                        grace_seconds=0.1,
                        detach=True,
                        startup_timeout=0.1,
                    )
            self.assertEqual(raised.exception.exit_code, cli.EXIT_SAFETY)
            kill.assert_not_called()
            killpg.assert_not_called()

    def test_replace_validates_new_command_before_signaling(self) -> None:
        inspector = FakeInspector({4242: self.process}, [self.listener])
        with tempfile.TemporaryDirectory() as raw:
            store = cli.RuntimeStore(base=Path(raw) / "runtime")
            with mock.patch("os.kill") as kill, mock.patch("os.killpg") as killpg:
                with self.assertRaises(cli.AgentPreviewError) as raised:
                    cli.replace_process(
                        self.config,
                        store,
                        inspector,
                        lambda _: True,
                        4242,
                        987654,
                        ["definitely-not-an-agent-preview-executable"],
                        force=False,
                        grace_seconds=0.1,
                        detach=True,
                        startup_timeout=0.1,
                    )
            self.assertEqual(raised.exception.exit_code, cli.EXIT_CONFIG)
            kill.assert_not_called()
            killpg.assert_not_called()


@unittest.skipUnless(sys.platform.startswith("linux") and Path("/proc/net/tcp").exists(), "Linux procfs required")
class LinuxIntegrationTests(unittest.TestCase):
    def free_port(self) -> int:
        with socket.socket() as candidate:
            candidate.bind(("127.0.0.1", 0))
            return candidate.getsockname()[1]

    def test_launch_status_and_stop(self) -> None:
        port = self.free_port()
        config = cli.Config(
            "127.0.0.1", port, "https://server.example.com", "cloudflared.service", Path("/test")
        )
        with tempfile.TemporaryDirectory() as raw:
            store = cli.RuntimeStore(base=Path(raw) / "runtime")
            inspector = cli.ProcInspector()
            result = cli.run_preview(
                config,
                store,
                inspector,
                lambda _: True,
                [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"],
                startup_timeout=5,
            )
            self.assertEqual(result, cli.EXIT_OK)
            status_value = cli.collect_status(config, store, inspector, lambda _: True)
            self.assertEqual(status_value["port_state"], "owned")
            self.assertTrue(status_value["ready"])
            self.assertEqual(
                cli.stop_preview(
                    config,
                    store,
                    inspector,
                    include_registered=False,
                    force=False,
                    grace_seconds=5,
                ),
                cli.EXIT_OK,
            )

    def test_register_requires_confirmation_to_stop(self) -> None:
        port = self.free_port()
        config = cli.Config(
            "127.0.0.1", port, "https://server.example.com", "cloudflared.service", Path("/test")
        )
        process = subprocess.Popen(
            [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            inspector = cli.ProcInspector()
            deadline = time.monotonic() + 5
            while not inspector.listeners(port) and time.monotonic() < deadline:
                time.sleep(0.05)
            with tempfile.TemporaryDirectory() as raw:
                store = cli.RuntimeStore(base=Path(raw) / "runtime")
                self.assertEqual(cli.register_process(config, store, inspector, process.pid), cli.EXIT_OK)
                with self.assertRaises(cli.AgentPreviewError) as raised:
                    cli.stop_preview(
                        config,
                        store,
                        inspector,
                        include_registered=False,
                        force=False,
                        grace_seconds=1,
                    )
                self.assertEqual(raised.exception.exit_code, cli.EXIT_SAFETY)
                self.assertIsNone(process.poll())
                self.assertEqual(
                    cli.stop_preview(
                        config,
                        store,
                        inspector,
                        include_registered=True,
                        force=False,
                        grace_seconds=5,
                    ),
                    cli.EXIT_OK,
                )
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
