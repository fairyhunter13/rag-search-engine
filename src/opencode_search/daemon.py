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
import tomllib
import urllib.error
import urllib.request
from collections.abc import Generator, Iterable
from contextlib import contextmanager
from pathlib import Path

import psutil
import yaml

from opencode_search.daemon_runtime import runtime_state

DEFAULT_DAEMON_HOST = os.environ.get("OPENCODE_MCP_DAEMON_HOST", "127.0.0.1")
DEFAULT_DAEMON_PORT = int(os.environ.get("OPENCODE_MCP_DAEMON_PORT", "8765"))
DEFAULT_IDLE_SHUTDOWN_S = int(os.environ.get("OPENCODE_MCP_IDLE_SHUTDOWN_S", "900"))
DEFAULT_CLIENT_STALE_S = int(os.environ.get("OPENCODE_MCP_CLIENT_STALE_S", "60"))
# Unload embedding/reranker models after this many seconds of no inference.
# Set to 0 to disable. Models reload on next search (~2-5s warm-up).
DEFAULT_MODEL_IDLE_UNLOAD_S = int(os.environ.get("OPENCODE_MODEL_IDLE_UNLOAD_S", "300"))

# ---------------------------------------------------------------------------
# systemd sd_notify integration
# ---------------------------------------------------------------------------

def _sd_notify(message: str) -> None:
    """Send a notification to systemd via the sd_notify protocol.

    No-op when not running under systemd (NOTIFY_SOCKET not set).
    Uses abstract namespace sockets when the path starts with '@'.
    """
    notify_socket = os.environ.get("NOTIFY_SOCKET")
    if not notify_socket:
        return
    try:
        addr: str | bytes = notify_socket
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        if notify_socket.startswith("@"):
            addr = b"\0" + notify_socket[1:].encode()
        sock.connect(addr)
        sock.sendall(message.encode())
        sock.close()
    except Exception:
        pass  # best-effort: never crash because of notify failure



# Allow isolating daemon state for tests/CI (prevents interference with any
# existing user daemon).
_STATE_DIR = Path(
    os.environ.get(
        "OPENCODE_MCP_STATE_DIR",
        str(Path.home() / ".local" / "state" / "opencode-search"),
    )
).expanduser()
_LOCK_PATH = _STATE_DIR / "daemon.lock"
_PID_PATH = _STATE_DIR / "daemon.pid"
_META_PATH = _STATE_DIR / "daemon.json"
_LOG_PATH = _STATE_DIR / "daemon.log"
_BIN_DIR = Path.home() / ".opencode" / "bin"
_INIT_WRAPPER_PATH = _BIN_DIR / "opencode-search-init"
_SYSTEMD_USER_DIR = Path.home() / ".config" / "systemd" / "user"
_SYSTEMD_SERVICE_NAME = "opencode-search-mcp-daemon.service"
_SYSTEMD_SERVICE_PATH = _SYSTEMD_USER_DIR / _SYSTEMD_SERVICE_NAME
_SYSTEMD_NOTIFY_SERVICE_NAME = "opencode-search-mcp-failure-notify.service"
_SYSTEMD_NOTIFY_SERVICE_PATH = _SYSTEMD_USER_DIR / _SYSTEMD_NOTIFY_SERVICE_NAME
_GLOBAL_PROMPT_DIR = Path.home() / ".config" / "opencode-search"
_CLAUDE_GLOBAL_MD = Path.home() / "CLAUDE.md"
_CODEX_BLOCK_START = "# >>> opencode-search developer instructions >>>"
_CODEX_BLOCK_END = "# <<< opencode-search developer instructions <<<"
_CLAUDE_BLOCK_START = "<!-- >>> opencode-search global instructions >>> -->"
_CLAUDE_BLOCK_END = "<!-- <<< opencode-search global instructions <<< -->"
_HERMES_MARKER_START = "[opencode-search-global-instructions:start]"
_HERMES_MARKER_END = "[opencode-search-global-instructions:end]"


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
def _file_lock() -> Generator[None, None, None]:
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
        proc = subprocess.Popen(
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
        # Enforce Codex global config invariants on every helper invocation.
        _enforce_codex_fast_mode_disabled()

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


def discover_claude_config_dirs(home: Path | None = None) -> list[Path]:
    """Discover additional Claude Code profile config dirs.

    We intentionally do not install or rely on shell wrappers (e.g. in
    `~/.bash_aliases`). Instead, we discover profile config directories by
    scanning the user's home directory for folders matching `.claude-*` or
    `.claude_*` (excluding the default `~/.claude`).
    """
    home = home or Path.home()
    candidates: list[Path] = []
    for pattern in (".claude-*", ".claude_*"):
        for path in home.glob(pattern):
            if not path.is_dir():
                continue
            if path.name == ".claude":
                continue
            candidates.append(path)

    unique: list[Path] = []
    seen: set[str] = set()
    for path in sorted(candidates, key=lambda p: p.name):
        key = str(path.resolve())
        if key in seen:
            continue
        unique.append(path)
        seen.add(key)
    return unique


def _run_command(command: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def _remove_if_present(command: list[str], env: dict[str, str] | None = None) -> None:
    _run_command(command, env=env)


def _bridge_command(python_bin: Path | None = None) -> list[str]:
    python_bin = python_bin or Path(sys.executable)
    return [str(python_bin), "-m", "opencode_search", "daemon", "bridge-stdio"]


def _global_prompt_text() -> str:
    return (
        "opencode-search: GPU-accelerated code intelligence. 7 tools — pick the right one:\n"
        "\n"
        "WHICH TOOL TO CALL:\n"
        "  search(query, scope, project_paths)   → find specific code, files, functions. scope: code|docs|all\n"
        "  ask(query, project_path, scope)       → 'how does X work?', architecture, conventions, business logic\n"
        "    scope=\"global\": GraphRAG map-reduce synthesis across ALL community summaries\n"
        "  graph(symbol, project_path, relation) → call graph analysis\n"
        "    relation=\"callers|callees|impact|path\" — standard\n"
        "    relation=\"impact_narrative\"          — LLM summary: risk level, affected domains\n"
        "    relation=\"semantic_trace\" (+to_symbol=) — natural language trace between two symbols\n"
        "  overview(project_path, what)          → project structure, communities, dependencies, status\n"
        "    what=\"structure|communities|status|projects|patterns\" — standard\n"
        "    what=\"architecture_domains\"          — top-level Leiden hierarchy (architecture domains)\n"
        "    what=\"hierarchy\"                     — full recursive Leiden hierarchy (all levels)\n"
        "    what=\"service_mesh\"                  — detected inter-service gRPC/HTTP/MQ topology\n"
        "    what=\"import_cycles\"                 — circular import dependencies (Tarjan SCC)\n"
        "    what=\"suggested_questions\"           — questions the graph is uniquely positioned to answer\n"
        "    what=\"graph_diff\"                    — symbols added/removed recently\n"
        "    what=\"surprising_connections\"        — edges spanning architectural community boundaries\n"
        "    what=\"pr_impact\"                     — PR risk: changed files → communities + risk level\n"
        "  build(project_path, action)           → async index/enrich/wiki — returns {job_id} immediately\n"
        "    action=\"pipeline\"                    — full KB build (recommended first-run)\n"
        "    action=\"hierarchy\"                   — build recursive community hierarchy (GraphRAG-like)\n"
        "    action=\"analyze_patterns\"            — LLM-powered deep pattern analysis\n"
        "    action=\"enrich\"                      — enrich unannotated communities\n"
        "    action=\"wiki\"                        — generate/refresh wiki pages\n"
        "  federation(root_path)                 → list/manage sub-repositories\n"
        "  manage(project_path, action)          → project lifecycle operations\n"
        "    action=\"wiki_lint\"                   — health-check the wiki\n"
        "    action=\"stop_watching\"               — stop file watcher\n"
        "    action=\"install_hooks\"               — install git post-commit hook for auto-reindex\n"
        "    action=\"uninstall_hooks\"             — remove git post-commit hook\n"
        "    action=\"dedup\"                       — deduplicate graph nodes (dry_run=True to preview)\n"
        "    action=\"vacuum\"                      — remove orphan index tier dirs; free disk space\n"
        "    action=\"jobs\"                        — list background build jobs; job_id= for one job\n"
        "\n"
        "QUICK DECISION GUIDE:\n"
        "  'find the payment handler'           → search('payment handler')\n"
        "  'how does auth work?'                → ask('how does auth work', project_path)\n"
        "  'what is the overall architecture?'  → ask('describe architecture', project_path, scope='global')\n"
        "  'what calls ProcessOrder?'           → graph('ProcessOrder', project_path, relation='callers')\n"
        "  'what breaks if I change X?'         → graph('X', project_path, relation='impact_narrative')\n"
        "  'trace login to database'            → graph('login', project_path, relation='semantic_trace', to_symbol='database write')\n"
        "  'what services call each other?'     → overview(project_path, what='service_mesh')\n"
        "  'top-level architecture domains?'    → overview(project_path, what='architecture_domains')\n"
        "  'are there circular imports?'        → overview(project_path, what='import_cycles')\n"
        "  'what changed in the graph?'         → overview(project_path, what='graph_diff')\n"
        "  'unusual cross-layer dependencies?'  → overview(project_path, what='surprising_connections')\n"
        "  'what should I explore first?'       → overview(project_path, what='suggested_questions')\n"
        "  'tell me about this project'         → overview(project_path, what='structure')\n"
        "  'what packages/dependencies?'        → overview(project_path, what='patterns')\n"
        "  'list all indexed projects'          → overview(what='projects')\n"
        "  'index this project' [explicit ask]  → build(project_path, action='pipeline')\n"
        "\n"
        "RULES:\n"
        "- Call search BEFORE grep/find/Read for any code lookup. Only fall back to bash if search returns nothing.\n"
        "- Use ask for 'how does X work' questions; use search to find specific code.\n"
        "- Use ask(scope=\"global\") for holistic questions about the entire codebase.\n"
        "- Use graph(relation=\"impact_narrative\") instead of raw impact for human-readable analysis.\n"
        "- overview(what='structure') returns the project tree, language breakdown, graph stats, and top communities.\n"
        "- overview(what='patterns') returns languages, dependencies, package versions, coding conventions, frameworks, architecture, and module structure.\n"
        "- NEVER auto-index. Only call build when the user explicitly asks.\n"
        "- If the project is not indexed, say so and ask before indexing.\n"
        "- Do NOT delegate codebase questions to sub-agents — they don't inherit these instructions.\n"
        "- After indexing, the daemon watches files automatically — no need to re-index on every change.\n"
    )


def _global_prompt_with_markers(start: str, end: str) -> str:
    return f"{start}\n{_global_prompt_text().rstrip()}\n{end}"


def _replace_managed_block(existing: str, start: str, end: str, block: str) -> str:
    if start in existing and end in existing:
        pattern = re.compile(rf"{re.escape(start)}.*?{re.escape(end)}", flags=re.DOTALL)
        return pattern.sub(block, existing)
    stripped = existing.rstrip()
    if stripped:
        return stripped + "\n\n" + block + "\n"
    return block + "\n"


def _install_claude_global_prompt(claude_dirs: list[Path], home: Path | None = None) -> list[str]:
    home = home or Path.home()
    block = _global_prompt_with_markers(_CLAUDE_BLOCK_START, _CLAUDE_BLOCK_END)
    all_dirs = [home / ".claude", *claude_dirs]
    written: list[str] = []
    for config_dir in all_dirs:
        if not config_dir.exists():
            continue
        target = config_dir / "CLAUDE.md"
        existing = target.read_text(encoding="utf-8") if target.exists() else ""
        updated = _replace_managed_block(existing, _CLAUDE_BLOCK_START, _CLAUDE_BLOCK_END, block)
        target.write_text(updated, encoding="utf-8")
        written.append(str(target))
    return written


def _remove_managed_block(existing: str, start: str, end: str) -> str:
    updated = existing
    if start in updated and end in updated:
        pattern = re.compile(rf"\n?{re.escape(start)}.*?{re.escape(end)}\n?", flags=re.DOTALL)
        updated = pattern.sub("\n", updated)
    marker_pattern = re.compile(
        rf"(?m)^[ \t]*(?:{re.escape(start)}|{re.escape(end)})[ \t]*\n?"
    )
    return marker_pattern.sub("", updated)


def _split_toml_root_preamble(existing: str) -> tuple[str, str]:
    match = re.search(r"(?m)^\s*\[", existing)
    if match is None:
        return existing, ""
    return existing[: match.start()], existing[match.start() :]


def _strip_root_toml_assignment(preamble: str, key: str) -> str:
    pattern = re.compile(
        rf"""(?ms)
        ^[ \t]*{re.escape(key)}[ \t]*=[ \t]*
        (?:
            \"\"\".*?\"\"\"
            |'''.*?'''
            |"(?:\\.|[^"\\])*"
            |'(?:\\.|[^'\\])*'
        )
        [ \t]*\n?
        """,
        flags=re.VERBOSE,
    )
    return pattern.sub("", preamble)


def _render_root_toml_string_assignment(key: str, value: str) -> str:
    return f"{key} = {json.dumps(value)}"


def _update_codex_config_text(existing: str) -> str:
    managed = _global_prompt_with_markers(_HERMES_MARKER_START, _HERMES_MARKER_END)
    unmanaged = ""
    try:
        parsed = tomllib.loads(existing)
    except tomllib.TOMLDecodeError:
        parsed = {}
    existing_prompt = parsed.get("developer_instructions")
    if isinstance(existing_prompt, str):
        unmanaged = _strip_marker_block(existing_prompt, _HERMES_MARKER_START, _HERMES_MARKER_END).strip()
    prompt = f"{unmanaged}\n\n{managed}".strip() if unmanaged else managed

    cleaned = _remove_managed_block(existing, _CODEX_BLOCK_START, _CODEX_BLOCK_END)
    preamble, remainder = _split_toml_root_preamble(cleaned)
    preamble = _strip_root_toml_assignment(preamble, "developer_instructions").rstrip()
    block = "\n".join(
        [
            _CODEX_BLOCK_START,
            _render_root_toml_string_assignment("developer_instructions", prompt),
            _CODEX_BLOCK_END,
        ]
    )

    parts: list[str] = []
    if preamble:
        parts.append(preamble)
    parts.append(block)
    if remainder:
        parts.append(remainder.lstrip("\n"))
    updated = "\n\n".join(parts).rstrip() + "\n"
    tomllib.loads(updated)
    return updated


def _disable_codex_fast_mode(config_text: str) -> str:
    if re.search(r"(?m)^fast_mode\s*=\s*false\s*$", config_text):
        return config_text
    updated, n = re.subn(r"(?m)^fast_mode\s*=\s*.*$", "fast_mode = false", config_text)
    if n > 0:
        return updated
    updated, n = re.subn(r"(\[features\]\n)", r"\1fast_mode = false\n", config_text, count=1)
    if n > 0:
        return updated
    return config_text.rstrip() + "\n\n[features]\nfast_mode = false\n"


def _enforce_codex_fast_mode_disabled() -> None:
    """Force Codex fast_mode off in the global config (idempotent)."""
    config_path = Path.home() / ".codex" / "config.toml"
    if not config_path.exists():
        return
    try:
        existing = config_path.read_text(encoding="utf-8")
    except OSError:
        return
    updated = _disable_codex_fast_mode(existing)
    if updated != existing:
        try:
            config_path.write_text(updated, encoding="utf-8")
        except OSError:
            return


def _install_codex_global_prompt() -> str:
    config_path = Path.home() / ".codex" / "config.toml"
    if not config_path.exists():
        return str(config_path)
    existing = config_path.read_text(encoding="utf-8")
    updated = _update_codex_config_text(existing)
    updated = _disable_codex_fast_mode(updated)
    config_path.write_text(updated, encoding="utf-8")
    return str(config_path)


def _strip_marker_block(text: str, start: str, end: str) -> str:
    if start not in text or end not in text:
        return text
    pattern = re.compile(rf"\n?{re.escape(start)}.*?{re.escape(end)}\n?", flags=re.DOTALL)
    return pattern.sub("\n", text).strip()


def _yaml_dump_literal(data: dict) -> str:
    """Dump YAML using literal block scalars (|) for multiline strings to prevent parse errors."""
    class _LiteralStr(str):
        pass

    def _literal_representer(dumper: yaml.Dumper, data: str) -> yaml.ScalarNode:
        if "\n" in data:
            return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
        return dumper.represent_scalar("tag:yaml.org,2002:str", data)

    _dumper = yaml.Dumper
    _dumper.add_representer(_LiteralStr, _literal_representer)

    def _convert(obj: object) -> object:
        if isinstance(obj, dict):
            return {k: _convert(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_convert(v) for v in obj]
        if isinstance(obj, str) and "\n" in obj:
            return _LiteralStr(obj)
        return obj

    return yaml.dump(_convert(data), Dumper=_dumper, sort_keys=False, allow_unicode=True)


def _install_hermes_global_prompt() -> str:
    config_path = Path.home() / ".hermes" / "config.yaml"
    if not config_path.exists():
        return str(config_path)

    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        # Recover from a malformed config written by a previous round-trip.
        # Attempt a lenient re-parse by stripping the system_prompt field first.
        raw = config_path.read_text(encoding="utf-8")
        try:
            # Remove the broken system_prompt line and everything indented under it
            import re as _re
            cleaned = _re.sub(r"\nagent:\s*\n  system_prompt:.*?(?=\n\S|\Z)", "\nagent: {}", raw, flags=_re.DOTALL)
            data = yaml.safe_load(cleaned) or {}
        except Exception:
            data = {}

    agent = data.setdefault("agent", {})
    if not isinstance(agent, dict):
        agent = {}
        data["agent"] = agent
    existing = str(agent.get("system_prompt", "") or "")
    unmanaged = _strip_marker_block(existing, _HERMES_MARKER_START, _HERMES_MARKER_END).strip()
    managed = _global_prompt_with_markers(_HERMES_MARKER_START, _HERMES_MARKER_END)
    agent["system_prompt"] = f"{unmanaged}\n\n{managed}".strip() if unmanaged else managed
    config_path.write_text(_yaml_dump_literal(data), encoding="utf-8")
    return str(config_path)


def _install_opencode_global_prompt() -> list[str]:
    """Write the 7-tool intent prompt into AGENTS.md for every opencode profile."""
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    candidates: list[Path] = []
    candidates.append(config_home / "opencode" / "AGENTS.md")
    for entry in sorted(config_home.iterdir()) if config_home.exists() else []:
        if entry.is_dir() and entry.name.startswith("opencode-"):
            candidates.append(entry / "opencode" / "AGENTS.md")

    block = _global_prompt_with_markers(_HERMES_MARKER_START, _HERMES_MARKER_END)
    written: list[str] = []
    for target in candidates:
        if not target.parent.exists():
            continue
        existing = target.read_text(encoding="utf-8") if target.exists() else ""
        updated = _replace_managed_block(existing, _HERMES_MARKER_START, _HERMES_MARKER_END, block)
        if updated != existing:
            target.write_text(updated, encoding="utf-8")
        written.append(str(target))
    return written


def _install_init_wrapper(python_bin: Path) -> str:
    _INIT_WRAPPER_PATH.parent.mkdir(parents=True, exist_ok=True)
    script = "\n".join(
        [
            "#!/usr/bin/env bash",
            "set -euo pipefail",
            f'exec "{python_bin}" -m opencode_search init "$@"',
            "",
        ]
    )
    _INIT_WRAPPER_PATH.write_text(script, encoding="utf-8")
    _INIT_WRAPPER_PATH.chmod(0o755)
    return str(_INIT_WRAPPER_PATH)


def _install_claude(
    bridge_command: list[str],
    config_dirs: Iterable[Path],
    *,
    transport: str,
    host: str,
    port: int,
) -> list[str]:
    claude_bin = shutil.which("claude")
    if not claude_bin:
        return []
    installed: list[str] = ["default"]
    _remove_if_present([claude_bin, "mcp", "remove", "opencode-search", "--scope", "user"])
    transport = (transport or "http").strip().lower()
    if transport == "http":
        result = _run_command(
            [
                claude_bin,
                "mcp",
                "add",
                "--scope",
                "user",
                "--transport",
                "http",
                "opencode-search",
                daemon_url(host, port),
            ]
        )
    else:
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
        if transport == "http":
            result = _run_command(
                [
                    claude_bin,
                    "mcp",
                    "add",
                    "--scope",
                    "user",
                    "--transport",
                    "http",
                    "opencode-search",
                    daemon_url(host, port),
                ],
                env=env,
            )
        else:
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


def _enforce_codex_config() -> None:
    """Ensure opinionated defaults in ~/.codex/config.toml.

    Currently enforces:
    - features.fast_mode = false
    """
    config_path = Path.home() / ".codex" / "config.toml"
    if not config_path.exists():
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text("[features]\nfast_mode = false\n", encoding="utf-8")
        return

    text = config_path.read_text(encoding="utf-8")
    data = tomllib.loads(text)
    if data.get("features", {}).get("fast_mode") is False:
        return

    lines = text.splitlines(keepends=True)
    found_features = False
    found_fast_mode = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped == "[features]":
            found_features = True
            continue
        if found_features and stripped.startswith("fast_mode"):
            lines[i] = "fast_mode = false\n"
            found_fast_mode = True
            break
        if found_features and stripped.startswith("["):
            lines.insert(i, "fast_mode = false\n")
            found_fast_mode = True
            break

    if not found_fast_mode:
        if not found_features:
            lines.append("\n[features]\n")
        lines.append("fast_mode = false\n")

    config_path.write_text("".join(lines), encoding="utf-8")


def _install_codex(
    bridge_command: list[str],
    *,
    transport: str,
    host: str,
    port: int,
) -> bool:
    codex_bin = shutil.which("codex")
    if not codex_bin:
        return False
    _remove_if_present([codex_bin, "mcp", "remove", "opencode-search"])
    transport = (transport or "http").strip().lower()
    if transport == "http":
        result = _run_command([codex_bin, "mcp", "add", "opencode-search", "--url", daemon_url(host, port)])
    else:
        result = _run_command([codex_bin, "mcp", "add", "opencode-search", "--", *bridge_command])
    if result.returncode != 0:
        raise RuntimeError(f"Codex MCP install failed: {result.stderr.strip()}")
    _enforce_codex_config()
    return True


def _install_hermes(_bridge_command: list[str]) -> bool:
    hermes_bin = shutil.which("hermes")
    return bool(hermes_bin)


def _update_hermes_config_for_global_servers(
    bridge_command: list[str],
    *,
    transport: str,
    host: str,
    port: int,
) -> None:
    config_path = Path.home() / ".hermes" / "config.yaml"
    if not config_path.exists():
        return
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        data = {}
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
    # Hermes MCP support varies by build; keep a stdio entry (bridge-stdio)
    # for maximum compatibility. The bridge talks to the singleton daemon.
    #
    # If Hermes later supports native streamable-http MCP config, we can add a
    # url-based entry behind an opt-in flag; for now keep behavior stable.
    _ = (transport, host, port)  # reserved for future url transport support
    servers["opencode-search"] = {"command": bridge_command[0], "args": bridge_command[1:], "enabled": True}
    config_path.write_text(_yaml_dump_literal(data), encoding="utf-8")


def _detect_cuda_env_lines() -> list[str]:
    """Return extra Environment= lines needed for CUDA to be discoverable.

    If CUDA libraries are already registered in ldconfig (via /etc/ld.so.conf.d/),
    no LD_LIBRARY_PATH is needed — systemd user services inherit the ldconfig
    cache. Otherwise, fall back to injecting the common CUDA lib paths.
    """
    cuda_conf_dir = Path("/etc/ld.so.conf.d")
    cuda_in_ldconfig = any(
        p.name.startswith("cuda") or "cuda" in p.read_text(encoding="utf-8", errors="ignore")
        for p in cuda_conf_dir.glob("*.conf")
        if p.is_file()
    ) if cuda_conf_dir.is_dir() else False

    lines = ["Environment=CUDA_VISIBLE_DEVICES=0"]
    if not cuda_in_ldconfig:
        fallback_paths = [
            "/usr/local/cuda/lib64",
            "/usr/local/cuda/targets/x86_64-linux/lib",
            "/usr/lib/x86_64-linux-gnu",
        ]
        existing = [p for p in fallback_paths if Path(p).exists()]
        if existing:
            lines.append("Environment=LD_LIBRARY_PATH=" + ":".join(existing))
    return lines


def _render_systemd_service(
    python_bin: Path,
    host: str,
    port: int,
    extra_env: list[str] | None = None,
) -> str:
    env_lines = list(extra_env) if extra_env else []
    return "\n".join(
        [
            "[Unit]",
            "Description=opencode-search singleton MCP daemon (GPU-enforced)",
            "After=default.target",
            f"OnFailure={_SYSTEMD_NOTIFY_SERVICE_NAME}",
            "StartLimitIntervalSec=120",
            "StartLimitBurst=5",
            "",
            "[Service]",
            "Type=notify",
            "NotifyAccess=main",
            f"ExecStart={python_bin} -m opencode_search daemon serve --host {host} --port {port}",
            f"ExecStop={python_bin} -m opencode_search daemon stop",
            "Restart=always",
            "RestartSec=5",
            "TimeoutStartSec=60",
            "TimeoutStopSec=15",
            "Environment=PYTHONUNBUFFERED=1",
            "Environment=OPENCODE_MCP_IDLE_SHUTDOWN_S=0",
            "Environment=OPENCODE_AUTO_PIPELINE=1",
            *env_lines,
            "Nice=10",
            "IOSchedulingClass=best-effort",
            "IOSchedulingPriority=7",
            "OOMScoreAdj=200",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ]
    )


def _render_systemd_notify_failure_service() -> str:
    """Render a oneshot service that fires a desktop notification on hard failure.

    Triggered via OnFailure= in the main daemon unit when StartLimitBurst is
    exceeded (i.e. GPU guard crashed 5× in 120s and systemd stops retrying).
    Uses notify-send if available; silently succeeds otherwise so the oneshot
    itself never blocks recovery.
    """
    title = "opencode-search: HARD FAIL — daemon stopped"
    body = (
        "GPU guard failed 5x in 120s. Automatic restarts exhausted.\\n"
        "Fix the GPU/CUDA issue then run:\\n"
        "  journalctl --user -u opencode-search-mcp-daemon -n 30\\n"
        "  systemctl --user reset-failed opencode-search-mcp-daemon\\n"
        "  systemctl --user start opencode-search-mcp-daemon"
    )
    exec_start = (
        "/bin/sh -c '"
        "command -v notify-send >/dev/null 2>&1 "
        f"&& notify-send -u critical -a opencode-search \"{title}\" \"{body}\" "
        "|| true'"
    )
    return "\n".join(
        [
            "[Unit]",
            "Description=opencode-search MCP daemon hard-fail desktop notification",
            "",
            "[Service]",
            "Type=oneshot",
            f"ExecStart={exec_start}",
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
    cuda_env = _detect_cuda_env_lines()
    _SYSTEMD_USER_DIR.mkdir(parents=True, exist_ok=True)
    _SYSTEMD_SERVICE_PATH.write_text(
        _render_systemd_service(python_bin, host=host, port=port, extra_env=cuda_env),
        encoding="utf-8",
    )
    _SYSTEMD_NOTIFY_SERVICE_PATH.write_text(
        _render_systemd_notify_failure_service(),
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
            "notify_service_path": str(_SYSTEMD_NOTIFY_SERVICE_PATH),
        }
    return {
        "installed": True,
        "service_path": str(_SYSTEMD_SERVICE_PATH),
        "notify_service_path": str(_SYSTEMD_NOTIFY_SERVICE_PATH),
    }


def _strip_jsonc_comments(text: str) -> str:
    """Remove // and /* */ comments from JSONC text without touching string literals."""
    result: list[str] = []
    i = 0
    n = len(text)
    in_string = False
    while i < n:
        ch = text[i]
        if in_string:
            result.append(ch)
            if ch == "\\" and i + 1 < n:
                i += 1
                result.append(text[i])
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
            result.append(ch)
        elif ch == "/" and i + 1 < n:
            nxt = text[i + 1]
            if nxt == "/":
                # single-line comment: skip to end of line
                while i < n and text[i] != "\n":
                    i += 1
                continue
            elif nxt == "*":
                # block comment: skip to */
                i += 2
                while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                    i += 1
                i += 2  # skip closing */
                continue
            else:
                result.append(ch)
        else:
            result.append(ch)
        i += 1
    return "".join(result)


def _install_opencode_configs(bridge_command: list[str]) -> list[str]:
    """Write the opencode-search MCP entry into every opencode.jsonc found under ~/.config."""
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    candidates: list[Path] = []
    # ~/.config/opencode/opencode.jsonc  (default profile)
    candidates.append(config_home / "opencode" / "opencode.jsonc")
    # ~/.config/opencode-*/opencode/opencode.jsonc  (named profiles, e.g. opencode-personal)
    for entry in sorted(config_home.iterdir()) if config_home.exists() else []:
        if entry.is_dir() and entry.name.startswith("opencode-"):
            candidates.append(entry / "opencode" / "opencode.jsonc")

    mcp_entry = {
        "type": "local",
        "command": bridge_command,
        "timeout": 30000,
    }

    updated: list[str] = []
    for path in candidates:
        if not path.exists():
            continue
        raw = path.read_text(encoding="utf-8")
        try:
            data = json.loads(_strip_jsonc_comments(raw))
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        mcp = data.setdefault("mcp", {})
        if not isinstance(mcp, dict):
            continue
        existing = mcp.get("opencode-search", {})
        if existing == mcp_entry:
            continue
        mcp["opencode-search"] = mcp_entry
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        updated.append(str(path))

    return updated


def install_global_integration(
    host: str = DEFAULT_DAEMON_HOST,
    port: int = DEFAULT_DAEMON_PORT,
    *,
    transport: str = "stdio",
) -> dict[str, object]:
    helper_python = Path(sys.executable)

    transport = (transport or "stdio").strip().lower()
    if transport not in {"http", "stdio"}:
        raise ValueError("transport must be 'http' or 'stdio'")

    bridge_command = _bridge_command(helper_python)
    claude_dirs = discover_claude_config_dirs()
    installed_claude = _install_claude(bridge_command, claude_dirs, transport=transport, host=host, port=port)
    codex_installed = _install_codex(bridge_command, transport=transport, host=host, port=port)
    hermes_installed = _install_hermes(bridge_command)
    _update_hermes_config_for_global_servers(bridge_command, transport=transport, host=host, port=port)
    opencode_configs = _install_opencode_configs(bridge_command)
    init_wrapper_path = _install_init_wrapper(helper_python)
    claude_prompt_paths = _install_claude_global_prompt(claude_dirs)
    codex_prompt_path = _install_codex_global_prompt()
    hermes_prompt_path = _install_hermes_global_prompt()
    opencode_prompt_paths = _install_opencode_global_prompt()
    systemd_result = install_systemd_user_service(host=host, port=port)

    return {
        "status": "ok",
        "url": daemon_url(host, port),
        "bridge_command": bridge_command,
        "claude_config_dirs": installed_claude,
        "codex_installed": codex_installed,
        "hermes_installed": hermes_installed,
        "opencode_configs": opencode_configs,
        "init_wrapper_path": init_wrapper_path,
        "claude_prompt_paths": claude_prompt_paths,
        "codex_prompt_path": codex_prompt_path,
        "hermes_prompt_path": hermes_prompt_path,
        "opencode_prompt_paths": opencode_prompt_paths,
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
