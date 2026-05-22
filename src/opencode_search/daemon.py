"""Global singleton MCP daemon and client integration helpers."""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterable
from contextlib import contextmanager
from pathlib import Path

import psutil
import yaml

from opencode_search.daemon_runtime import runtime_state

DEFAULT_DAEMON_HOST = os.environ.get("OPENCODE_MCP_DAEMON_HOST", "127.0.0.1")
DEFAULT_DAEMON_PORT = int(os.environ.get("OPENCODE_MCP_DAEMON_PORT", "8765"))
DEFAULT_IDLE_SHUTDOWN_S = int(os.environ.get("OPENCODE_MCP_IDLE_SHUTDOWN_S", "900"))
DEFAULT_CLIENT_STALE_S = int(os.environ.get("OPENCODE_MCP_CLIENT_STALE_S", "60"))
_STATE_DIR = Path.home() / ".local" / "state" / "opencode-search"
_LOCK_PATH = _STATE_DIR / "daemon.lock"
_PID_PATH = _STATE_DIR / "daemon.pid"
_META_PATH = _STATE_DIR / "daemon.json"
_LOG_PATH = _STATE_DIR / "daemon.log"
_HELPER_PATH = Path.home() / ".local" / "bin" / "opencode-search-global-mcp-ensure"
_ALIASES_PATH = Path.home() / ".bash_aliases"
_ALIAS_BLOCK_START = "# >>> opencode-search global singleton MCP >>>"
_ALIAS_BLOCK_END = "# <<< opencode-search global singleton MCP <<<"
_SYSTEMD_USER_DIR = Path.home() / ".config" / "systemd" / "user"
_SYSTEMD_SERVICE_NAME = "opencode-search-mcp-daemon.service"
_SYSTEMD_SERVICE_PATH = _SYSTEMD_USER_DIR / _SYSTEMD_SERVICE_NAME


def _state_dir() -> Path:
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    return _STATE_DIR


def daemon_url(host: str = DEFAULT_DAEMON_HOST, port: int = DEFAULT_DAEMON_PORT) -> str:
    return f"http://{host}:{port}/mcp"


def health_url(host: str = DEFAULT_DAEMON_HOST, port: int = DEFAULT_DAEMON_PORT) -> str:
    return f"http://{host}:{port}/healthz"


def _write_pidfile(host: str = DEFAULT_DAEMON_HOST, port: int = DEFAULT_DAEMON_PORT) -> None:
    _state_dir()
    _PID_PATH.write_text(f"{os.getpid()}\n", encoding="utf-8")
    _META_PATH.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "host": host,
                "port": port,
                "url": daemon_url(host, port),
                "started_at": time.time(),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _clear_pidfile() -> None:
    for path in (_PID_PATH, _META_PATH):
        try:
            path.unlink()
        except FileNotFoundError:
            continue


def _read_pid() -> int | None:
    try:
        return int(_PID_PATH.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError):
        return None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    else:
        return True


def _tcp_port_open(host: str, port: int, timeout: float = 0.3) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


def _find_pid_by_port(host: str, port: int) -> int | None:
    try:
        connections = psutil.net_connections(kind="tcp")
    except Exception:
        return None

    for conn in connections:
        if not conn.laddr:
            continue
        ip = getattr(conn.laddr, "ip", None)
        conn_port = getattr(conn.laddr, "port", None)
        if ip == host and conn_port == port and conn.status == psutil.CONN_LISTEN:
            return conn.pid
    return None


def daemon_is_healthy(
    host: str = DEFAULT_DAEMON_HOST,
    port: int = DEFAULT_DAEMON_PORT,
    timeout_s: float = 0.8,
) -> bool:
    request = urllib.request.Request(health_url(host, port), method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            if response.status != 200:
                return False
            data = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return False
    return bool(data.get("ok")) and data.get("service") == "opencode-search"


@contextmanager
def _file_lock():
    import fcntl

    _state_dir()
    with _LOCK_PATH.open("a+", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _wait_for_healthy(host: str, port: int, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if daemon_is_healthy(host, port):
            return True
        time.sleep(0.25)
    return False


def _spawn_daemon(host: str, port: int) -> int:
    _state_dir()
    python_bin = Path(sys.executable)
    env = os.environ.copy()
    env.setdefault("PYTHONUNBUFFERED", "1")
    with _LOG_PATH.open("a", encoding="utf-8") as log_fh:
        proc = subprocess.Popen(  # noqa: S603
            [
                str(python_bin),
                "-m",
                "opencode_search",
                "daemon",
                "serve",
                "--host",
                host,
                "--port",
                str(port),
            ],
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=log_fh,
            start_new_session=True,
            close_fds=True,
            env=env,
        )
    return proc.pid


def ensure_daemon_running(
    host: str = DEFAULT_DAEMON_HOST,
    port: int = DEFAULT_DAEMON_PORT,
    timeout_s: float = 20.0,
) -> dict[str, object]:
    with _file_lock():
        if daemon_is_healthy(host, port):
            return {"status": "already_running", "url": daemon_url(host, port)}

        existing_pid = _read_pid()
        if existing_pid is not None and not _pid_alive(existing_pid):
            _clear_pidfile()

        if _tcp_port_open(host, port) and not daemon_is_healthy(host, port):
            raise RuntimeError(
                f"Port {host}:{port} is already in use by a non-opencode-search process"
            )

        pid = _spawn_daemon(host, port)
        if not _wait_for_healthy(host, port, timeout_s):
            raise RuntimeError(
                f"Daemon failed to become healthy within {timeout_s:.0f}s; see {_LOG_PATH}"
            )
        return {"status": "started", "pid": pid, "url": daemon_url(host, port)}


def stop_daemon(
    host: str = DEFAULT_DAEMON_HOST,
    port: int = DEFAULT_DAEMON_PORT,
) -> dict[str, object]:
    with _file_lock():
        pid = _read_pid()
        if pid is None and daemon_is_healthy(host, port):
            pid = _find_pid_by_port(host, port)
        if pid is None:
            return {"status": "not_running"}

        if not _pid_alive(pid):
            _clear_pidfile()
            return {"status": "not_running"}

        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            if not _pid_alive(pid):
                _clear_pidfile()
                return {"status": "stopped", "pid": pid}
            time.sleep(0.2)

        raise RuntimeError(f"Timed out waiting for daemon pid {pid} to stop")


def daemon_status(host: str = DEFAULT_DAEMON_HOST, port: int = DEFAULT_DAEMON_PORT) -> dict[str, object]:
    pid = _read_pid()
    if pid is not None and not _pid_alive(pid):
        _clear_pidfile()
        pid = None
    healthy = daemon_is_healthy(host, port)
    if pid is None and healthy:
        pid = _find_pid_by_port(host, port)
    return {
        "running": healthy,
        "healthy": healthy,
        "pid": pid,
        "url": daemon_url(host, port),
        "log_path": str(_LOG_PATH),
        "idle_shutdown_seconds": DEFAULT_IDLE_SHUTDOWN_S,
        **runtime_state.snapshot(),
    }


def parse_alias_map(text: str) -> dict[str, str]:
    aliases: dict[str, str] = {}
    pattern = re.compile(r"""^alias\s+([A-Za-z0-9_-]+)=(['"])(.*)\2$""")
    for line in text.splitlines():
        match = pattern.match(line.strip())
        if match:
            aliases[match.group(1)] = match.group(3)
    return aliases


def discover_claude_config_dirs(alias_text: str, home: Path | None = None) -> list[Path]:
    home = home or Path.home()
    dirs: list[Path] = []
    for command in parse_alias_map(alias_text).values():
        match = re.search(r"CLAUDE_CONFIG_DIR=([^\s]+)", command)
        if not match:
            continue
        raw_path = match.group(1).strip("\"'")
        expanded = Path(raw_path.replace("~", str(home), 1)).expanduser()
        dirs.append(expanded)
    unique: list[Path] = []
    seen: set[str] = set()
    for path in dirs:
        key = str(path)
        if key not in seen:
            unique.append(path)
            seen.add(key)
    return unique


def _wrap_command(command_text: str, base_command: str) -> str:
    pattern = re.compile(rf"(?<!\S){re.escape(base_command)}(?!\S)")
    return pattern.sub(f"command {base_command}", command_text, count=1)


def render_shell_wrapper_block(helper_script: Path, alias_text: str) -> str:
    aliases = parse_alias_map(alias_text)
    claude_cmd = _wrap_command(
        aliases.get("claude", "claude --dangerously-skip-permissions --model claude-opus-4-6"),
        "claude",
    )
    claude1_cmd = _wrap_command(
        aliases.get("claude1", "CLAUDE_CONFIG_DIR=~/.claude-account1 claude"),
        "claude",
    )
    claude2_cmd = _wrap_command(
        aliases.get("claude2", "CLAUDE_CONFIG_DIR=~/.claude-account2 claude"),
        "claude",
    )
    claude3_cmd = _wrap_command(
        aliases.get("claude3", "CLAUDE_CONFIG_DIR=~/.claude-account3 claude"),
        "claude",
    )
    codex_cmd = _wrap_command(aliases.get("codex", "codex --yolo"), "codex")

    return "\n".join(
        [
            _ALIAS_BLOCK_START,
            "unset -f _opencode_search_ensure_global_mcp 2>/dev/null || true",
            "_opencode_search_ensure_global_mcp() {",
            f"  '{helper_script}' >/dev/null 2>&1 || true",
            "}",
            "unalias claude 2>/dev/null || true",
            "claude() {",
            "  _opencode_search_ensure_global_mcp",
            f"  {claude_cmd} \"$@\"",
            "}",
            "unalias claude1 2>/dev/null || true",
            "claude1() {",
            "  _opencode_search_ensure_global_mcp",
            f"  {claude1_cmd} \"$@\"",
            "}",
            "unalias claude2 2>/dev/null || true",
            "claude2() {",
            "  _opencode_search_ensure_global_mcp",
            f"  {claude2_cmd} \"$@\"",
            "}",
            "unalias claude3 2>/dev/null || true",
            "claude3() {",
            "  _opencode_search_ensure_global_mcp",
            f"  {claude3_cmd} \"$@\"",
            "}",
            "unalias codex 2>/dev/null || true",
            "codex() {",
            "  _opencode_search_ensure_global_mcp",
            f"  {codex_cmd} \"$@\"",
            "}",
            "unset -f hermes 2>/dev/null || true",
            "hermes() {",
            "  _opencode_search_ensure_global_mcp",
            "  command hermes \"$@\"",
            "}",
            _ALIAS_BLOCK_END,
        ]
    )


def upsert_shell_wrapper_block(existing_text: str, block: str) -> str:
    if _ALIAS_BLOCK_START in existing_text and _ALIAS_BLOCK_END in existing_text:
        pattern = re.compile(
            rf"{re.escape(_ALIAS_BLOCK_START)}.*?{re.escape(_ALIAS_BLOCK_END)}",
            flags=re.DOTALL,
        )
        return pattern.sub(block, existing_text).rstrip() + "\n"
    stripped = existing_text.rstrip()
    if stripped:
        return stripped + "\n\n" + block + "\n"
    return block + "\n"


def _run_command(command: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        command,
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def _remove_if_present(command: list[str], env: dict[str, str] | None = None) -> None:
    _run_command(command, env=env)


def remove_shell_wrapper_block(existing_text: str) -> str:
    if _ALIAS_BLOCK_START not in existing_text or _ALIAS_BLOCK_END not in existing_text:
        return existing_text
    pattern = re.compile(
        rf"\n?{re.escape(_ALIAS_BLOCK_START)}.*?{re.escape(_ALIAS_BLOCK_END)}\n?",
        flags=re.DOTALL,
    )
    return pattern.sub("\n", existing_text).strip() + ("\n" if existing_text.strip() else "")


def _bridge_command(python_bin: Path | None = None) -> list[str]:
    python_bin = python_bin or Path(sys.executable)
    return [str(python_bin), "-m", "opencode_search", "daemon", "bridge-stdio"]


def _install_claude(bridge_command: list[str], config_dirs: Iterable[Path]) -> list[str]:
    claude_bin = shutil.which("claude")
    if not claude_bin:
        return []
    installed: list[str] = ["default"]
    _remove_if_present([claude_bin, "mcp", "remove", "opencode-search", "--scope", "user"])
    result = _run_command(
        [
            claude_bin,
            "mcp",
            "add",
            "--scope",
            "user",
            "opencode-search",
            "--",
            *bridge_command,
        ]
    )
    if result.returncode != 0:
        raise RuntimeError(f"Claude MCP install failed for default profile: {result.stderr.strip()}")

    for config_dir in config_dirs:
        env = os.environ.copy()
        env["CLAUDE_CONFIG_DIR"] = str(config_dir)
        _remove_if_present([claude_bin, "mcp", "remove", "opencode-search", "--scope", "user"], env=env)
        result = _run_command(
            [
                claude_bin,
                "mcp",
                "add",
                "--scope",
                "user",
                "opencode-search",
                "--",
                *bridge_command,
            ],
            env=env,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Claude MCP install failed for {config_dir}: {result.stderr.strip()}")
        installed.append(str(config_dir))
    return installed


def _install_codex(bridge_command: list[str]) -> bool:
    codex_bin = shutil.which("codex")
    if not codex_bin:
        return False
    _remove_if_present([codex_bin, "mcp", "remove", "opencode-search"])
    result = _run_command([codex_bin, "mcp", "add", "opencode-search", "--", *bridge_command])
    if result.returncode != 0:
        raise RuntimeError(f"Codex MCP install failed: {result.stderr.strip()}")
    return True


def _install_hermes(_bridge_command: list[str]) -> bool:
    hermes_bin = shutil.which("hermes")
    return bool(hermes_bin)


def _update_hermes_config_for_global_servers(bridge_command: list[str]) -> None:
    config_path = Path.home() / ".hermes" / "config.yaml"
    if not config_path.exists():
        return
    data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    legacy_mcp = data.get("mcp")
    if isinstance(legacy_mcp, dict):
        legacy_servers = legacy_mcp.get("servers")
        if isinstance(legacy_servers, dict):
            legacy_servers.pop("opencode-search", None)
            if not legacy_servers:
                legacy_mcp.pop("servers", None)
        if not legacy_mcp:
            data.pop("mcp", None)
    servers = data.setdefault("mcp_servers", {})
    servers["opencode-search"] = {
        "command": bridge_command[0],
        "args": bridge_command[1:],
        "enabled": True,
    }
    config_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _render_systemd_service(python_bin: Path, host: str, port: int) -> str:
    return "\n".join(
        [
            "[Unit]",
            "Description=opencode-search singleton MCP daemon",
            "After=default.target",
            "",
            "[Service]",
            "Type=oneshot",
            "RemainAfterExit=yes",
            f"ExecStart={python_bin} -m opencode_search daemon ensure --host {host} --port {port}",
            f"ExecStop={python_bin} -m opencode_search daemon stop",
            "TimeoutStopSec=15",
            "Environment=PYTHONUNBUFFERED=1",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ]
    )


def install_systemd_user_service(
    host: str = DEFAULT_DAEMON_HOST,
    port: int = DEFAULT_DAEMON_PORT,
) -> dict[str, object]:
    systemctl_bin = shutil.which("systemctl")
    if not systemctl_bin:
        return {"installed": False, "reason": "systemctl not found"}

    python_bin = Path(sys.executable)
    _SYSTEMD_USER_DIR.mkdir(parents=True, exist_ok=True)
    _SYSTEMD_SERVICE_PATH.write_text(
        _render_systemd_service(python_bin, host=host, port=port),
        encoding="utf-8",
    )
    _run_command([systemctl_bin, "--user", "daemon-reload"])
    existing = daemon_status(host=host, port=port)
    if existing["running"]:
        stop_daemon()
    enable = _run_command([systemctl_bin, "--user", "enable", "--now", _SYSTEMD_SERVICE_NAME])
    if enable.returncode != 0:
        return {
            "installed": False,
            "reason": enable.stderr.strip() or enable.stdout.strip(),
            "service_path": str(_SYSTEMD_SERVICE_PATH),
        }
    return {"installed": True, "service_path": str(_SYSTEMD_SERVICE_PATH)}


def install_global_integration(
    aliases_path: Path = _ALIASES_PATH,
    helper_path: Path = _HELPER_PATH,
    host: str = DEFAULT_DAEMON_HOST,
    port: int = DEFAULT_DAEMON_PORT,
) -> dict[str, object]:
    alias_text = aliases_path.read_text(encoding="utf-8") if aliases_path.exists() else ""
    helper_python = Path(sys.executable)
    if aliases_path.exists():
        cleaned_alias_text = remove_shell_wrapper_block(alias_text)
        if cleaned_alias_text != alias_text:
            aliases_path.write_text(cleaned_alias_text, encoding="utf-8")
    try:
        helper_path.unlink()
    except FileNotFoundError:
        pass

    bridge_command = _bridge_command(helper_python)
    claude_dirs = discover_claude_config_dirs(alias_text)
    installed_claude = _install_claude(bridge_command, claude_dirs)
    codex_installed = _install_codex(bridge_command)
    hermes_installed = _install_hermes(bridge_command)
    _update_hermes_config_for_global_servers(bridge_command)
    systemd_result = install_systemd_user_service(host=host, port=port)

    return {
        "status": "ok",
        "url": daemon_url(host, port),
        "bridge_command": bridge_command,
        "claude_config_dirs": installed_claude,
        "codex_installed": codex_installed,
        "hermes_installed": hermes_installed,
        "aliases_path": str(aliases_path),
        "systemd": systemd_result,
    }


def _shutdown_monitor(idle_timeout_s: int, stale_after_s: int) -> None:
    while True:
        time.sleep(5.0)
        if idle_timeout_s <= 0:
            continue
        if runtime_state.should_shutdown(idle_timeout_s, stale_after_s):
            os.kill(os.getpid(), signal.SIGTERM)
            return


def _start_shutdown_monitor() -> None:
    monitor = threading.Thread(
        target=_shutdown_monitor,
        args=(DEFAULT_IDLE_SHUTDOWN_S, DEFAULT_CLIENT_STALE_S),
        daemon=True,
        name="opencode-search-daemon-monitor",
    )
    monitor.start()


def run_http_daemon_server(host: str = DEFAULT_DAEMON_HOST, port: int = DEFAULT_DAEMON_PORT) -> None:
    from opencode_search.mcp import run_mcp_http_server

    if _tcp_port_open(host, port):
        raise RuntimeError(f"Cannot start daemon on {host}:{port}; port already in use")
    _write_pidfile(host=host, port=port)
    try:
        _start_shutdown_monitor()
        run_mcp_http_server(host=host, port=port)
    finally:
        _clear_pidfile()
