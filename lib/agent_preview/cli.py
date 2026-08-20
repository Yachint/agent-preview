"""Safe lifecycle management for a single Cloudflare-backed preview origin."""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import datetime as dt
import fcntl
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
from typing import Callable, Iterable, Iterator, Sequence
from urllib.parse import urlsplit

from . import __version__


EXIT_OK = 0
EXIT_RUNTIME = 1
EXIT_CONFIG = 2
EXIT_CONFLICT = 3
EXIT_SAFETY = 4
EXIT_READINESS = 5

SYSTEM_CONFIG_PATH = Path("/etc/agent-preview.conf")
CONFIG_KEYS = {
    "AGENT_PREVIEW_HOST",
    "AGENT_PREVIEW_PORT",
    "AGENT_PREVIEW_PUBLIC_URL",
    "AGENT_PREVIEW_TUNNEL_SERVICE",
}
SERVICE_RE = re.compile(r"^[A-Za-z0-9_.@:-]+\.service$")
DNS_LABEL_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


class AgentPreviewError(Exception):
    """An expected user-facing error with a stable exit code."""

    def __init__(self, message: str, exit_code: int = EXIT_RUNTIME):
        super().__init__(message)
        self.exit_code = exit_code


@dataclasses.dataclass(frozen=True)
class Config:
    host: str
    port: int
    public_url: str
    tunnel_service: str
    path: Path

    @property
    def origin_url(self) -> str:
        return f"http://{self.host}:{self.port}"


@dataclasses.dataclass(frozen=True)
class ProcessInfo:
    pid: int
    uid: int
    pgid: int
    start_time: int
    command: tuple[str, ...]
    executable: str | None
    cwd: str | None

    def to_dict(self) -> dict[str, object]:
        return dataclasses.asdict(self) | {"command": list(self.command)}


@dataclasses.dataclass(frozen=True)
class Listener:
    address: str
    port: int
    inode: int
    process: ProcessInfo | None

    def to_dict(self, ownership: str = "unknown") -> dict[str, object]:
        result: dict[str, object] = {
            "address": self.address,
            "port": self.port,
            "inode": self.inode,
            "ownership": ownership,
        }
        if self.process:
            result.update(self.process.to_dict())
        else:
            result.update(
                {
                    "pid": None,
                    "uid": None,
                    "pgid": None,
                    "start_time": None,
                    "command": [],
                    "executable": None,
                    "cwd": None,
                }
            )
        return result


@dataclasses.dataclass(frozen=True)
class ActiveState:
    pid: int
    pgid: int
    process_start_time: int
    uid: int
    ownership: str
    command: tuple[str, ...]
    working_directory: str
    origin_url: str
    public_url: str
    log_path: str | None
    started_at: str

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "pid": self.pid,
            "pgid": self.pgid,
            "process_start_time": self.process_start_time,
            "uid": self.uid,
            "ownership": self.ownership,
            "command": list(self.command),
            "working_directory": self.working_directory,
            "origin_url": self.origin_url,
            "public_url": self.public_url,
            "log_path": self.log_path,
            "started_at": self.started_at,
        }

    @classmethod
    def from_dict(cls, value: object) -> "ActiveState":
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            raise ValueError("unsupported state schema")
        command = value.get("command")
        if not isinstance(command, list) or not all(isinstance(x, str) for x in command):
            raise ValueError("invalid state command")
        ownership = value.get("ownership")
        if ownership not in {"launched", "registered"}:
            raise ValueError("invalid state ownership")
        log_path = value.get("log_path")
        if log_path is not None and not isinstance(log_path, str):
            raise ValueError("invalid state log path")
        return cls(
            pid=_required_int(value, "pid"),
            pgid=_required_int(value, "pgid"),
            process_start_time=_required_int(value, "process_start_time"),
            uid=_required_int(value, "uid"),
            ownership=ownership,
            command=tuple(command),
            working_directory=_required_str(value, "working_directory"),
            origin_url=_required_str(value, "origin_url"),
            public_url=_required_str(value, "public_url"),
            log_path=log_path,
            started_at=_required_str(value, "started_at"),
        )


@dataclasses.dataclass
class ForegroundRun:
    process: subprocess.Popen[bytes]
    state: ActiveState
    log_handle: object


def _required_int(value: dict[str, object], key: str) -> int:
    item = value.get(key)
    if not isinstance(item, int) or isinstance(item, bool):
        raise ValueError(f"invalid state field: {key}")
    return item


def _required_str(value: dict[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str):
        raise ValueError(f"invalid state field: {key}")
    return item


def load_config(path: Path = SYSTEM_CONFIG_PATH, *, require_root_owner: bool = True) -> Config:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise AgentPreviewError(
            f"Agent Preview is not configured: {path} does not exist.", EXIT_CONFIG
        ) from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise AgentPreviewError(f"Configuration must be a regular non-symlink file: {path}", EXIT_CONFIG)
    if require_root_owner and info.st_uid != 0:
        raise AgentPreviewError(f"Configuration must be owned by root: {path}", EXIT_CONFIG)
    if info.st_mode & 0o022:
        raise AgentPreviewError(f"Configuration must not be group/world-writable: {path}", EXIT_CONFIG)

    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise AgentPreviewError(f"Could not read configuration: {exc}", EXIT_CONFIG) from exc
    for number, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise AgentPreviewError(f"Invalid configuration line {number}: expected KEY=value", EXIT_CONFIG)
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if key not in CONFIG_KEYS:
            raise AgentPreviewError(f"Unknown configuration key on line {number}: {key}", EXIT_CONFIG)
        if key in values:
            raise AgentPreviewError(f"Duplicate configuration key on line {number}: {key}", EXIT_CONFIG)
        if not value or "\x00" in value or "\n" in value or "\r" in value:
            raise AgentPreviewError(f"Invalid empty/control value for {key}", EXIT_CONFIG)
        values[key] = value
    missing = sorted(CONFIG_KEYS - values.keys())
    if missing:
        raise AgentPreviewError(f"Missing configuration keys: {', '.join(missing)}", EXIT_CONFIG)

    host = values["AGENT_PREVIEW_HOST"]
    if host != "127.0.0.1":
        raise AgentPreviewError("AGENT_PREVIEW_HOST must be exactly 127.0.0.1", EXIT_CONFIG)
    try:
        port = int(values["AGENT_PREVIEW_PORT"], 10)
    except ValueError as exc:
        raise AgentPreviewError("AGENT_PREVIEW_PORT must be numeric", EXIT_CONFIG) from exc
    if not 1024 <= port <= 65535:
        raise AgentPreviewError("AGENT_PREVIEW_PORT must be between 1024 and 65535", EXIT_CONFIG)

    public_url = values["AGENT_PREVIEW_PUBLIC_URL"]
    parsed = urlsplit(public_url)
    try:
        parsed_port = parsed.port
    except ValueError as exc:
        raise AgentPreviewError("AGENT_PREVIEW_PUBLIC_URL has an invalid port", EXIT_CONFIG) from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed_port not in (None, 443)
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise AgentPreviewError(
            "AGENT_PREVIEW_PUBLIC_URL must be an HTTPS origin without credentials, path, query, or fragment",
            EXIT_CONFIG,
        )
    try:
        ipaddress.ip_address(parsed.hostname)
    except ValueError:
        pass
    else:
        raise AgentPreviewError("AGENT_PREVIEW_PUBLIC_URL must use a hostname, not an IP address", EXIT_CONFIG)
    if not _valid_dns_hostname(parsed.hostname):
        raise AgentPreviewError("AGENT_PREVIEW_PUBLIC_URL has an invalid DNS hostname", EXIT_CONFIG)

    service = values["AGENT_PREVIEW_TUNNEL_SERVICE"]
    if not SERVICE_RE.fullmatch(service):
        raise AgentPreviewError("AGENT_PREVIEW_TUNNEL_SERVICE is not a safe systemd service name", EXIT_CONFIG)
    return Config(host, port, public_url.rstrip("/"), service, path)


def _valid_dns_hostname(hostname: str) -> bool:
    normalized = hostname.rstrip(".")
    labels = normalized.split(".")
    return bool(normalized) and len(normalized) <= 253 and all(DNS_LABEL_RE.fullmatch(label) for label in labels)


class ProcInspector:
    """Read Linux process and TCP-listener identity from procfs."""

    def __init__(self, proc_root: Path = Path("/proc")):
        self.proc_root = proc_root

    def process(self, pid: int) -> ProcessInfo | None:
        base = self.proc_root / str(pid)
        try:
            dir_stat = base.stat()
            raw_stat = (base / "stat").read_text(encoding="utf-8")
            end = raw_stat.rfind(")")
            if end < 0:
                return None
            fields = raw_stat[end + 2 :].split()
            if len(fields) <= 19 or fields[0] in {"Z", "X"}:
                # Zombie and dead tasks can retain a /proc entry until their
                # parent reaps them, but they can no longer serve traffic or
                # respond to signals. Treat them as exited.
                return None
            pgid = int(fields[2])
            start_time = int(fields[19])
            raw_command = (base / "cmdline").read_bytes().split(b"\0")
            command = tuple(x.decode("utf-8", "replace") for x in raw_command if x)
            executable = _readlink_optional(base / "exe")
            cwd = _readlink_optional(base / "cwd")
            return ProcessInfo(pid, dir_stat.st_uid, pgid, start_time, command, executable, cwd)
        except (FileNotFoundError, PermissionError, ProcessLookupError, ValueError, OSError):
            return None

    def listeners(self, port: int) -> list[Listener]:
        sockets: list[tuple[str, int]] = []
        sockets.extend(self._read_tcp(self.proc_root / "net" / "tcp", socket.AF_INET, port))
        sockets.extend(self._read_tcp(self.proc_root / "net" / "tcp6", socket.AF_INET6, port))
        if not sockets:
            return []
        inode_to_pid = self._socket_owners({inode for _, inode in sockets})
        return [
            Listener(address, port, inode, self.process(inode_to_pid[inode]) if inode in inode_to_pid else None)
            for address, inode in sockets
        ]

    def _read_tcp(self, path: Path, family: int, port: int) -> list[tuple[str, int]]:
        try:
            rows = path.read_text(encoding="ascii").splitlines()[1:]
        except (FileNotFoundError, PermissionError, OSError, UnicodeError):
            return []
        result: list[tuple[str, int]] = []
        for row in rows:
            fields = row.split()
            if len(fields) < 10 or fields[3] != "0A":
                continue
            try:
                address_hex, port_hex = fields[1].split(":", 1)
                if int(port_hex, 16) != port:
                    continue
                address = _decode_proc_address(address_hex, family)
                inode = int(fields[9])
            except (ValueError, OSError):
                continue
            result.append((address, inode))
        return result

    def _socket_owners(self, wanted: set[int]) -> dict[int, int]:
        result: dict[int, int] = {}
        try:
            entries = list(self.proc_root.iterdir())
        except OSError:
            return result
        for entry in entries:
            if not entry.name.isdigit():
                continue
            try:
                fds = (entry / "fd").iterdir()
                for fd in fds:
                    target = os.readlink(fd)
                    if target.startswith("socket:[") and target.endswith("]"):
                        inode = int(target[8:-1])
                        if inode in wanted:
                            result.setdefault(inode, int(entry.name))
            except (FileNotFoundError, PermissionError, ProcessLookupError, ValueError, OSError):
                continue
        return result


def _decode_proc_address(value: str, family: int) -> str:
    raw = bytes.fromhex(value)
    if family == socket.AF_INET:
        return socket.inet_ntop(family, raw[::-1])
    reordered = b"".join(raw[index : index + 4][::-1] for index in range(0, 16, 4))
    return socket.inet_ntop(family, reordered)


def _readlink_optional(path: Path) -> str | None:
    try:
        return os.readlink(path)
    except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
        return None


class RuntimeStore:
    """Private, per-user runtime state and locking."""

    def __init__(self, uid: int | None = None, base: Path | None = None):
        self.uid = os.getuid() if uid is None else uid
        self.directory = base if base is not None else self._choose_directory()
        self.state_path = self.directory / "active.json"
        self.lock_path = self.directory / "agent-preview.lock"

    def _choose_directory(self) -> Path:
        candidate = Path("/run/user") / str(self.uid)
        try:
            info = candidate.stat()
            if info.st_uid == self.uid and stat.S_ISDIR(info.st_mode):
                return candidate / "agent-preview"
        except OSError:
            pass
        return Path("/tmp") / f"agent-preview-{self.uid}"

    def ensure(self) -> None:
        try:
            self.directory.mkdir(mode=0o700, parents=False, exist_ok=True)
        except FileNotFoundError as exc:
            raise AgentPreviewError(f"Runtime parent does not exist: {self.directory.parent}", EXIT_SAFETY) from exc
        info = self.directory.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise AgentPreviewError(f"Unsafe runtime path: {self.directory}", EXIT_SAFETY)
        if info.st_uid != self.uid:
            raise AgentPreviewError(f"Runtime directory has the wrong owner: {self.directory}", EXIT_SAFETY)
        if stat.S_IMODE(info.st_mode) != 0o700:
            try:
                self.directory.chmod(0o700)
            except OSError as exc:
                raise AgentPreviewError(f"Could not secure runtime directory: {exc}", EXIT_SAFETY) from exc

    @contextlib.contextmanager
    def lock(self) -> Iterator[None]:
        self.ensure()
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(self.lock_path, flags, 0o600)
        except OSError as exc:
            raise AgentPreviewError(f"Could not open runtime lock: {exc}", EXIT_SAFETY) from exc
        try:
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def read(self) -> tuple[ActiveState | None, str]:
        try:
            info = self.state_path.lstat()
        except FileNotFoundError:
            return None, "absent"
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            return None, "unsafe"
        if info.st_uid != self.uid or info.st_mode & 0o077:
            return None, "unsafe"
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
            return ActiveState.from_dict(value), "present"
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
            return None, "invalid"

    def write(self, state_value: ActiveState) -> None:
        self.ensure()
        if self.state_path.is_symlink():
            raise AgentPreviewError(f"Refusing symlink state path: {self.state_path}", EXIT_SAFETY)
        fd, temporary = tempfile.mkstemp(prefix=".active.", dir=self.directory)
        temporary_path = Path(temporary)
        try:
            os.fchmod(fd, 0o600)
            payload = (json.dumps(state_value.to_dict(), sort_keys=True, indent=2) + "\n").encode()
            os.write(fd, payload)
            os.fsync(fd)
            os.close(fd)
            fd = -1
            os.replace(temporary_path, self.state_path)
        finally:
            if fd >= 0:
                os.close(fd)
            with contextlib.suppress(FileNotFoundError):
                temporary_path.unlink()

    def clear(self) -> None:
        try:
            info = self.state_path.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_uid != self.uid:
            raise AgentPreviewError(f"Refusing to remove unsafe state path: {self.state_path}", EXIT_SAFETY)
        self.state_path.unlink()

    def new_log_path(self) -> Path:
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return self.directory / f"preview-{stamp}-{os.getpid()}.log"


def process_matches_state(process: ProcessInfo | None, state_value: ActiveState) -> bool:
    return bool(
        process
        and process.pid == state_value.pid
        and process.uid == state_value.uid
        and process.pgid == state_value.pgid
        and process.start_time == state_value.process_start_time
    )


def listener_is_safe(listener: Listener, config: Config) -> bool:
    return listener.address == config.host and listener.port == config.port


def listener_ownership(
    listener: Listener, state_value: ActiveState | None, inspector: ProcInspector
) -> str:
    if not state_value or not process_matches_state(inspector.process(state_value.pid), state_value):
        return "unknown"
    process = listener.process
    if not process or process.uid != state_value.uid:
        return "unknown"
    if state_value.ownership == "registered" and process.pid == state_value.pid:
        return "registered"
    if state_value.ownership == "launched" and process.pgid == state_value.pgid:
        return "launched"
    return "unknown"


def tunnel_is_active(service: str) -> bool:
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "--quiet", service],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        return False
    return result.returncode == 0


def origin_responds(config: Config) -> bool:
    connection = http.client.HTTPConnection(config.host, config.port, timeout=1.0)
    try:
        connection.request("GET", "/", headers={"Host": urlsplit(config.public_url).hostname or ""})
        response = connection.getresponse()
        response.read(1)
        return True
    except (OSError, http.client.HTTPException):
        return False
    finally:
        connection.close()


def collect_status(
    config: Config,
    store: RuntimeStore,
    inspector: ProcInspector,
    tunnel_checker: Callable[[str], bool],
) -> dict[str, object]:
    state_value, metadata = store.read()
    state_process = inspector.process(state_value.pid) if state_value else None
    identity_valid = bool(state_value and process_matches_state(state_process, state_value))
    if state_value and not identity_valid:
        metadata = "stale"
    listeners = inspector.listeners(config.port)
    listener_rows = [
        listener.to_dict(listener_ownership(listener, state_value if identity_valid else None, inspector))
        for listener in listeners
    ]
    if not listeners:
        port_state = "free"
    elif any(row["ownership"] in {"launched", "registered"} for row in listener_rows):
        port_state = "owned"
    else:
        port_state = "occupied"
    owned_listener = any(row["ownership"] in {"launched", "registered"} for row in listener_rows)
    return {
        "version": __version__,
        "configured": True,
        "config_path": str(config.path),
        "host": config.host,
        "port": config.port,
        "origin_url": config.origin_url,
        "public_url": config.public_url,
        "tunnel_service": config.tunnel_service,
        "tunnel_active": tunnel_checker(config.tunnel_service),
        "port_state": port_state,
        "origin_listening": bool(listeners),
        "ready": owned_listener and origin_responds(config),
        "state_metadata": metadata,
        "state_identity_valid": identity_valid,
        "ownership": state_value.ownership if identity_valid and state_value else None,
        "pid": state_value.pid if identity_valid and state_value else None,
        "pgid": state_value.pgid if identity_valid and state_value else None,
        "process_start_time": state_value.process_start_time if identity_valid and state_value else None,
        "command": list(state_value.command) if identity_valid and state_value else [],
        "working_directory": state_value.working_directory if identity_valid and state_value else None,
        "started_at": state_value.started_at if identity_valid and state_value else None,
        "log_path": state_value.log_path if identity_valid and state_value else None,
        "listeners": listener_rows,
    }


def print_conflict(status_value: dict[str, object]) -> None:
    print("There is already a process being served on the agent-preview port.")
    print()
    print(f"Public URL: {status_value['public_url']}")
    print(f"State metadata: {status_value['state_metadata']}")
    for index, item in enumerate(status_value["listeners"], 1):
        assert isinstance(item, dict)
        print(f"Listener {index}:")
        print(f"  PID: {item.get('pid') if item.get('pid') is not None else 'unavailable'}")
        print(f"  UID: {item.get('uid') if item.get('uid') is not None else 'unavailable'}")
        command = item.get("command") or []
        print(f"  Command: {' '.join(command) if command else 'unavailable'}")
        print(f"  Executable: {item.get('executable') or 'unavailable'}")
        print(f"  Working directory: {item.get('cwd') or 'unavailable'}")
        print(f"  Process start time: {item.get('start_time') or 'unavailable'}")
        print(f"  Bind address: {item.get('address')}:{item.get('port')}")
        print(f"  Ownership: {item.get('ownership')}")
    print()
    print("Choose one:")
    print("1. Kill process and replace it")
    print("2. Keep existing process")
    print("No action has been taken.")


def _state_for_process(
    process: ProcessInfo,
    ownership: str,
    config: Config,
    command: Sequence[str] | None = None,
    cwd: str | None = None,
    log_path: str | None = None,
) -> ActiveState:
    return ActiveState(
        pid=process.pid,
        pgid=process.pgid,
        process_start_time=process.start_time,
        uid=process.uid,
        ownership=ownership,
        command=tuple(command if command is not None else process.command),
        working_directory=cwd or process.cwd or "",
        origin_url=config.origin_url,
        public_url=config.public_url,
        log_path=log_path,
        started_at=dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    )


def _tail(path: Path, lines: int = 30) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return "".join(handle.readlines()[-lines:])
    except OSError:
        return ""


def _safe_log_open(path: Path) -> object:
    flags = os.O_CREAT | os.O_WRONLY | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    os.fchmod(fd, 0o600)
    return os.fdopen(fd, "ab", buffering=0)


def _command_env(config: Config) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "HOST": config.host,
            "PORT": str(config.port),
            "AGENT_PREVIEW_HOST": config.host,
            "AGENT_PREVIEW_PORT": str(config.port),
            "AGENT_PREVIEW_PUBLIC_URL": config.public_url,
        }
    )
    return environment


def _validate_command_available(command: Sequence[str]) -> None:
    if not command:
        raise AgentPreviewError("A replacement command is required after --", EXIT_CONFIG)
    executable = command[0]
    if "/" in executable:
        path = Path(executable)
        if not path.is_file() or not os.access(path, os.X_OK):
            raise AgentPreviewError(
                f"Replacement executable is missing or not executable: {executable}", EXIT_CONFIG
            )
    elif shutil.which(executable) is None:
        raise AgentPreviewError(f"Replacement executable was not found on PATH: {executable}", EXIT_CONFIG)


def _listeners_owned_by_group(
    listeners: Iterable[Listener], config: Config, uid: int, pgid: int
) -> list[Listener]:
    return [
        listener
        for listener in listeners
        if listener_is_safe(listener, config)
        and listener.process is not None
        and listener.process.uid == uid
        and listener.process.pgid == pgid
    ]


def _terminate_new_process(process: subprocess.Popen[bytes], grace: float = 3.0) -> None:
    if process.poll() is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    deadline = time.monotonic() + grace
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    if process.poll() is None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)


def _run_locked(
    config: Config,
    store: RuntimeStore,
    inspector: ProcInspector,
    tunnel_checker: Callable[[str], bool],
    command: Sequence[str],
    *,
    detach: bool,
    startup_timeout: float,
) -> int | ForegroundRun:
    if not command:
        raise AgentPreviewError("A command is required after --", EXIT_CONFIG)
    listeners = inspector.listeners(config.port)
    if listeners:
        print_conflict(collect_status(config, store, inspector, tunnel_checker))
        return EXIT_CONFLICT
    state_value, metadata = store.read()
    if metadata in {"stale", "invalid"} or (
        state_value and not process_matches_state(inspector.process(state_value.pid), state_value)
    ):
        store.clear()
    elif metadata == "unsafe":
        raise AgentPreviewError("Runtime state is unsafe; refusing to continue", EXIT_SAFETY)
    elif state_value:
        raise AgentPreviewError(
            "Agent Preview state still identifies a live process even though the port is free; stop it explicitly",
            EXIT_SAFETY,
        )
    if not tunnel_checker(config.tunnel_service):
        raise AgentPreviewError(
            f"Tunnel service is inactive: systemctl is-active {config.tunnel_service}", EXIT_READINESS
        )

    log_path = store.new_log_path()
    log_handle = _safe_log_open(log_path)
    try:
        process = subprocess.Popen(
            list(command),
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            env=_command_env(config),
            cwd=os.getcwd(),
            start_new_session=True,
        )
    except (OSError, ValueError) as exc:
        log_handle.close()
        raise AgentPreviewError(f"Could not start preview command: {exc}", EXIT_RUNTIME) from exc
    process_info = inspector.process(process.pid)
    if process_info is None:
        _terminate_new_process(process)
        log_handle.close()
        raise AgentPreviewError("Could not inspect the launched process", EXIT_SAFETY)
    state_value = _state_for_process(
        process_info,
        "launched",
        config,
        command=command,
        cwd=os.getcwd(),
        log_path=str(log_path),
    )
    store.write(state_value)
    deadline = time.monotonic() + startup_timeout
    failure: AgentPreviewError | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            failure = AgentPreviewError(
                f"Preview command exited before readiness with code {process.returncode}", EXIT_RUNTIME
            )
            break
        listeners = inspector.listeners(config.port)
        if listeners and (
            any(not listener_is_safe(listener, config) for listener in listeners)
            or len(_listeners_owned_by_group(listeners, config, process_info.uid, process_info.pgid))
            != len(listeners)
        ):
            failure = AgentPreviewError(
                "The configured port has an unsafe or unowned listener", EXIT_SAFETY
            )
            break
        if _listeners_owned_by_group(listeners, config, process_info.uid, process_info.pgid) and origin_responds(config):
            break
        time.sleep(0.1)
    else:
        failure = AgentPreviewError(
            f"Preview did not become ready within {startup_timeout:g} seconds", EXIT_READINESS
        )
    if failure:
        _terminate_new_process(process)
        with contextlib.suppress(AgentPreviewError):
            store.clear()
        log_handle.close()
        recent = _tail(log_path)
        if recent:
            print("Recent preview logs:", file=sys.stderr)
            print(recent, file=sys.stderr, end="" if recent.endswith("\n") else "\n")
        raise failure

    print(f"Preview ready: {config.public_url}")
    print(f"PID: {process.pid}")
    print(f"Log: {log_path}")
    if detach:
        log_handle.close()
        return EXIT_OK
    return ForegroundRun(process, state_value, log_handle)


def _wait_foreground(
    process: subprocess.Popen[bytes],
    state_value: ActiveState,
    store: RuntimeStore,
    log_handle: object,
) -> int:
    previous: dict[int, object] = {}

    def forward(signum: int, _frame: object) -> None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(state_value.pgid, signum)

    for signum in (signal.SIGINT, signal.SIGTERM):
        previous[signum] = signal.signal(signum, forward)
    try:
        return_code = process.wait()
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        log_handle.close()
        current, _ = store.read()
        if current and current.pid == state_value.pid and current.process_start_time == state_value.process_start_time:
            store.clear()
    return EXIT_OK if return_code == 0 else EXIT_RUNTIME


def run_preview(
    config: Config,
    store: RuntimeStore,
    inspector: ProcInspector,
    tunnel_checker: Callable[[str], bool],
    command: Sequence[str],
    *,
    detach: bool = True,
    startup_timeout: float = 30.0,
) -> int:
    with store.lock():
        result = _run_locked(
            config,
            store,
            inspector,
            tunnel_checker,
            command,
            detach=detach,
            startup_timeout=startup_timeout,
        )
    if isinstance(result, ForegroundRun):
        return _wait_foreground(result.process, result.state, store, result.log_handle)
    return result


def register_process(config: Config, store: RuntimeStore, inspector: ProcInspector, pid: int) -> int:
    with store.lock():
        listeners = inspector.listeners(config.port)
        if any(
            not listener_is_safe(item, config)
            or item.process is None
            or item.process.pid != pid
            for item in listeners
        ):
            raise AgentPreviewError(
                "The preview port is not exclusively owned by the requested loopback listener",
                EXIT_SAFETY,
            )
        matching = [
            item
            for item in listeners
            if item.process and item.process.pid == pid and listener_is_safe(item, config)
        ]
        if not matching:
            raise AgentPreviewError(
                f"PID {pid} is not a directly verifiable listener on {config.origin_url}", EXIT_SAFETY
            )
        process = matching[0].process
        assert process is not None
        if process.uid != os.getuid():
            raise AgentPreviewError("Cannot register a process owned by another UID", EXIT_SAFETY)
        current, metadata = store.read()
        if current and process_matches_state(inspector.process(current.pid), current):
            if listener_ownership(matching[0], current, inspector) in {"launched", "registered"}:
                print(f"Keeping existing process: {config.public_url}")
                return EXIT_OK
            raise AgentPreviewError("Live Agent Preview state belongs to another process", EXIT_SAFETY)
        if metadata == "unsafe":
            raise AgentPreviewError("Runtime state is unsafe; refusing to replace it", EXIT_SAFETY)
        if metadata in {"present", "invalid", "stale"}:
            store.clear()
        store.write(_state_for_process(process, "registered", config))
        print(f"Keeping existing process: {config.public_url}")
        print(f"Registered PID: {pid}")
        return EXIT_OK


def _wait_for_target_exit(
    inspector: ProcInspector,
    pid: int,
    start_time: int,
    timeout: float,
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = inspector.process(pid)
        if current is None or current.start_time != start_time:
            return True
        time.sleep(0.1)
    current = inspector.process(pid)
    return current is None or current.start_time != start_time


def _launched_group_has_listener(
    inspector: ProcInspector, config: Config, state_value: ActiveState
) -> bool:
    return bool(
        _listeners_owned_by_group(
            inspector.listeners(config.port), config, state_value.uid, state_value.pgid
        )
    )


def _wait_for_launched_stop(
    inspector: ProcInspector, config: Config, state_value: ActiveState, timeout: float
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        leader = inspector.process(state_value.pid)
        leader_gone = not process_matches_state(leader, state_value)
        if leader_gone and not _launched_group_has_listener(inspector, config, state_value):
            return True
        time.sleep(0.1)
    leader = inspector.process(state_value.pid)
    return not process_matches_state(leader, state_value) and not _launched_group_has_listener(
        inspector, config, state_value
    )


def _force_launched_group_if_still_owned(
    inspector: ProcInspector, config: Config, state_value: ActiveState
) -> None:
    leader = inspector.process(state_value.pid)
    if process_matches_state(leader, state_value) or _launched_group_has_listener(
        inspector, config, state_value
    ):
        try:
            os.killpg(state_value.pgid, signal.SIGKILL)
        except ProcessLookupError:
            return
        except PermissionError as exc:
            raise AgentPreviewError(
                f"Permission denied while signaling process group {state_value.pgid}", EXIT_SAFETY
            ) from exc


def _signal_verified_target(
    inspector: ProcInspector,
    process: ProcessInfo,
    state_value: ActiveState | None,
    signum: int,
) -> None:
    current = inspector.process(process.pid)
    if not current or current.uid != process.uid or current.start_time != process.start_time:
        raise AgentPreviewError("Process identity changed before signaling; refusing", EXIT_SAFETY)
    try:
        if state_value and state_value.ownership == "launched" and process_matches_state(
            inspector.process(state_value.pid), state_value
        ):
            os.killpg(state_value.pgid, signum)
        else:
            os.kill(process.pid, signum)
    except ProcessLookupError:
        return
    except PermissionError as exc:
        raise AgentPreviewError(f"Permission denied while signaling PID {process.pid}", EXIT_SAFETY) from exc


def _listener_identity_is_current(
    inspector: ProcInspector,
    config: Config,
    pid: int,
    start_time: int,
    expected_inodes: set[int],
) -> bool:
    listeners = inspector.listeners(config.port)
    return bool(listeners) and {item.inode for item in listeners} == expected_inodes and all(
        listener_is_safe(item, config)
        and item.process is not None
        and item.process.pid == pid
        and item.process.start_time == start_time
        for item in listeners
    )


def replace_process(
    config: Config,
    store: RuntimeStore,
    inspector: ProcInspector,
    tunnel_checker: Callable[[str], bool],
    pid: int,
    start_time: int,
    command: Sequence[str],
    *,
    force: bool,
    grace_seconds: float,
    detach: bool,
    startup_timeout: float,
) -> int:
    with store.lock():
        _validate_command_available(command)
        if not tunnel_checker(config.tunnel_service):
            raise AgentPreviewError(
                f"Tunnel service is inactive: systemctl is-active {config.tunnel_service}",
                EXIT_READINESS,
            )
        listeners = inspector.listeners(config.port)
        if any(
            not listener_is_safe(item, config)
            or item.process is None
            or item.process.pid != pid
            or item.process.start_time != start_time
            for item in listeners
        ):
            raise AgentPreviewError(
                "The preview port is not exclusively owned by the reported listener identity",
                EXIT_SAFETY,
            )
        matching = [
            item
            for item in listeners
            if item.process
            and item.process.pid == pid
            and item.process.start_time == start_time
            and listener_is_safe(item, config)
        ]
        if not matching:
            raise AgentPreviewError("The reported listener identity has changed; refusing replacement", EXIT_SAFETY)
        process = matching[0].process
        assert process is not None
        if process.uid != os.getuid():
            raise AgentPreviewError("Cannot replace a process owned by another UID", EXIT_SAFETY)
        state_value, metadata = store.read()
        if metadata == "unsafe":
            raise AgentPreviewError("Runtime state is unsafe; refusing to signal anything", EXIT_SAFETY)
        valid_state = (
            state_value
            if state_value and process_matches_state(inspector.process(state_value.pid), state_value)
            else None
        )
        if valid_state and listener_ownership(matching[0], valid_state, inspector) == "unknown":
            raise AgentPreviewError(
                "Live Agent Preview state belongs to a different process; refusing replacement",
                EXIT_SAFETY,
            )
        if not _listener_identity_is_current(
            inspector, config, pid, start_time, {item.inode for item in matching}
        ):
            raise AgentPreviewError(
                "Listener identity changed immediately before signaling; refusing replacement",
                EXIT_SAFETY,
            )
        _signal_verified_target(inspector, process, valid_state, signal.SIGTERM)
        if valid_state and valid_state.ownership == "launched":
            stopped = _wait_for_launched_stop(inspector, config, valid_state, grace_seconds)
        else:
            stopped = _wait_for_target_exit(inspector, pid, start_time, grace_seconds)
        if not stopped and force:
            if valid_state and valid_state.ownership == "launched":
                _force_launched_group_if_still_owned(inspector, config, valid_state)
                stopped = _wait_for_launched_stop(inspector, config, valid_state, 3.0)
            else:
                _signal_verified_target(inspector, process, valid_state, signal.SIGKILL)
                stopped = _wait_for_target_exit(inspector, pid, start_time, 3.0)
        if not stopped:
            raise AgentPreviewError(
                "Existing process did not stop; rerun replace with --force only after explicit approval",
                EXIT_RUNTIME,
            )
        deadline = time.monotonic() + 3.0
        while inspector.listeners(config.port) and time.monotonic() < deadline:
            time.sleep(0.1)
        if inspector.listeners(config.port):
            raise AgentPreviewError("The preview port is still occupied after termination", EXIT_CONFLICT)
        if state_value:
            store.clear()
        print(f"Stopped existing PID {pid}; starting replacement.")
        result = _run_locked(
            config,
            store,
            inspector,
            tunnel_checker,
            command,
            detach=detach,
            startup_timeout=startup_timeout,
        )
    if isinstance(result, ForegroundRun):
        return _wait_foreground(result.process, result.state, store, result.log_handle)
    return result


def stop_preview(
    config: Config,
    store: RuntimeStore,
    inspector: ProcInspector,
    *,
    include_registered: bool,
    force: bool,
    grace_seconds: float,
) -> int:
    with store.lock():
        state_value, metadata = store.read()
        if metadata == "unsafe":
            raise AgentPreviewError("Runtime state is unsafe; refusing to signal anything", EXIT_SAFETY)
        if not state_value:
            raise AgentPreviewError("No Agent Preview process is registered", EXIT_RUNTIME)
        process = inspector.process(state_value.pid)
        if not process_matches_state(process, state_value):
            raise AgentPreviewError("Recorded process identity is stale; refusing to signal it", EXIT_SAFETY)
        assert process is not None
        if process.uid != os.getuid():
            raise AgentPreviewError("Recorded process belongs to another UID", EXIT_SAFETY)
        if state_value.ownership == "registered" and not include_registered:
            raise AgentPreviewError(
                "This process was registered, not launched by Agent Preview; use --include-registered after confirmation",
                EXIT_SAFETY,
            )
        if state_value.ownership == "registered":
            direct_listener = any(
                item.process
                and item.process.pid == process.pid
                and listener_is_safe(item, config)
                for item in inspector.listeners(config.port)
            )
            if not direct_listener:
                raise AgentPreviewError("Registered process no longer owns the preview listener", EXIT_SAFETY)
        else:
            listeners = inspector.listeners(config.port)
            if listeners and len(
                _listeners_owned_by_group(listeners, config, process.uid, process.pgid)
            ) != len(listeners):
                raise AgentPreviewError(
                    "The preview port includes a listener outside the launched process group",
                    EXIT_SAFETY,
                )
        _signal_verified_target(inspector, process, state_value, signal.SIGTERM)
        if state_value.ownership == "launched":
            stopped = _wait_for_launched_stop(inspector, config, state_value, grace_seconds)
        else:
            stopped = _wait_for_target_exit(inspector, process.pid, process.start_time, grace_seconds)
        if not stopped and force:
            if state_value.ownership == "launched":
                _force_launched_group_if_still_owned(inspector, config, state_value)
                stopped = _wait_for_launched_stop(inspector, config, state_value, 3.0)
            else:
                _signal_verified_target(inspector, process, state_value, signal.SIGKILL)
                stopped = _wait_for_target_exit(inspector, process.pid, process.start_time, 3.0)
        if not stopped:
            raise AgentPreviewError(
                "Preview process remains alive; use --force only after explicit approval", EXIT_RUNTIME
            )
        store.clear()
        print(f"Stopped preview PID {process.pid}. Tunnel service was not changed.")
        return EXIT_OK


def show_logs(store: RuntimeStore, *, lines: int, follow: bool) -> int:
    state_value, metadata = store.read()
    if metadata == "unsafe":
        raise AgentPreviewError("Runtime state is unsafe", EXIT_SAFETY)
    if not state_value or not state_value.log_path:
        raise AgentPreviewError("The current preview has no Agent Preview log", EXIT_RUNTIME)
    path = Path(state_value.log_path)
    try:
        safe_parent = path.parent.resolve(strict=True) == store.directory.resolve(strict=True)
    except OSError:
        safe_parent = False
    if not safe_parent or not path.name.startswith("preview-"):
        raise AgentPreviewError("Refusing a log path outside the Agent Preview runtime directory", EXIT_SAFETY)
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise AgentPreviewError(f"Preview log does not exist: {path}", EXIT_RUNTIME) from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_uid != store.uid:
        raise AgentPreviewError("Refusing to read an unsafe preview log", EXIT_SAFETY)
    initial = _tail(path, lines)
    print(initial, end="" if initial.endswith("\n") or not initial else "\n")
    if not follow:
        return EXIT_OK
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            handle.seek(0, os.SEEK_END)
            while True:
                chunk = handle.read()
                if chunk:
                    print(chunk, end="", flush=True)
                else:
                    time.sleep(0.25)
    except KeyboardInterrupt:
        return EXIT_OK


def _positive_number(value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if number <= 0 or number > 3600:
        raise argparse.ArgumentTypeError("must be greater than 0 and at most 3600")
    return number


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-preview")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    for name in ("check", "status"):
        child = subparsers.add_parser(name)
        child.add_argument("--json", action="store_true", dest="as_json")

    run = subparsers.add_parser("run")
    mode = run.add_mutually_exclusive_group()
    mode.add_argument("--detach", action="store_true", default=True)
    mode.add_argument("--foreground", action="store_true")
    run.add_argument("--startup-timeout", type=_positive_number, default=30.0)
    run.add_argument("command", nargs=argparse.REMAINDER)

    replace = subparsers.add_parser("replace")
    replace.add_argument("--pid", type=_positive_int, required=True)
    replace.add_argument("--start-time", type=_positive_int, required=True)
    replace.add_argument("--force", action="store_true")
    replace.add_argument("--grace-seconds", type=_positive_number, default=10.0)
    replace.add_argument("--startup-timeout", type=_positive_number, default=30.0)
    replace_mode = replace.add_mutually_exclusive_group()
    replace_mode.add_argument("--detach", action="store_true", default=True)
    replace_mode.add_argument("--foreground", action="store_true")
    replace.add_argument("command", nargs=argparse.REMAINDER)

    register = subparsers.add_parser("register")
    register.add_argument("--pid", type=_positive_int, required=True)

    logs = subparsers.add_parser("logs")
    logs.add_argument("--follow", "-f", action="store_true")
    logs.add_argument("--lines", type=_positive_int, default=30)

    stop = subparsers.add_parser("stop")
    stop.add_argument("--include-registered", action="store_true")
    stop.add_argument("--grace-seconds", type=_positive_number, default=10.0)
    stop.add_argument("--force", action="store_true")
    return parser


def _normalize_command(command: Sequence[str]) -> list[str]:
    result = list(command)
    if result and result[0] == "--":
        result.pop(0)
    return result


def _print_status_human(value: dict[str, object]) -> None:
    print(f"Configured: yes ({value['config_path']})")
    print(f"Public URL: {value['public_url']}")
    print(f"Origin: {value['origin_url']}")
    print(f"Tunnel active: {'yes' if value['tunnel_active'] else 'no'}")
    print(f"Port state: {value['port_state']}")
    print(f"State metadata: {value['state_metadata']}")
    if value["listeners"]:
        for item in value["listeners"]:
            assert isinstance(item, dict)
            command = item.get("command") or []
            print(
                "Listener: "
                f"pid={item.get('pid') or 'unavailable'} "
                f"uid={item.get('uid') if item.get('uid') is not None else 'unavailable'} "
                f"bind={item.get('address')}:{item.get('port')} "
                f"ownership={item.get('ownership')} "
                f"command={' '.join(command) if command else 'unavailable'}"
            )


def main(
    argv: Sequence[str] | None = None,
    *,
    config_path: Path = SYSTEM_CONFIG_PATH,
    runtime_base: Path | None = None,
    inspector: ProcInspector | None = None,
    tunnel_checker: Callable[[str], bool] = tunnel_is_active,
    require_root_config: bool = True,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = load_config(config_path, require_root_owner=require_root_config)
        store = RuntimeStore(base=runtime_base)
        proc = inspector or ProcInspector()
        if args.subcommand in {"check", "status"}:
            value = collect_status(config, store, proc, tunnel_checker)
            if args.as_json:
                print(json.dumps(value, sort_keys=True))
            else:
                _print_status_human(value)
            if args.subcommand == "check":
                if not value["tunnel_active"]:
                    return EXIT_READINESS
                if value["port_state"] == "occupied":
                    return EXIT_CONFLICT
            return EXIT_OK
        if args.subcommand == "run":
            return run_preview(
                config,
                store,
                proc,
                tunnel_checker,
                _normalize_command(args.command),
                detach=not args.foreground,
                startup_timeout=args.startup_timeout,
            )
        if args.subcommand == "replace":
            return replace_process(
                config,
                store,
                proc,
                tunnel_checker,
                args.pid,
                args.start_time,
                _normalize_command(args.command),
                force=args.force,
                grace_seconds=args.grace_seconds,
                detach=not args.foreground,
                startup_timeout=args.startup_timeout,
            )
        if args.subcommand == "register":
            return register_process(config, store, proc, args.pid)
        if args.subcommand == "logs":
            return show_logs(store, lines=args.lines, follow=args.follow)
        if args.subcommand == "stop":
            return stop_preview(
                config,
                store,
                proc,
                include_registered=args.include_registered,
                force=args.force,
                grace_seconds=args.grace_seconds,
            )
        parser.error("unknown command")
    except AgentPreviewError as exc:
        print(f"agent-preview: {exc}", file=sys.stderr)
        return exc.exit_code
    return EXIT_CONFIG


if __name__ == "__main__":
    raise SystemExit(main())
