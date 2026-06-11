"""Global singleton MCP daemon and client integration helpers."""

from __future__ import annotations

# Configure CUDA library paths before any CUDA-linked imports. Must run early
# so dlopen finds libcurand/libcublas/libcudnn when ONNX loads its CUDA plugin.
from opencode_search.cuda_setup import configure_cuda_paths as _configure_cuda_paths
_configure_cuda_paths()

import json
import logging
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

# Shared event loop reference for background sweep threads (Python 3.12 asyncio.get_event_loop()
# raises RuntimeError in non-main threads; the server lifespan sets this once on startup).
_DAEMON_LOOP: object = None
_DAEMON_LOOP_READY = threading.Event()
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


def _wait_for_port_free(host: str, port: int, *, timeout_s: float = 5.0, poll_s: float = 0.1) -> bool:
    """Poll until host:port stops accepting connections (predecessor released it) or timeout."""
    import time
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not _tcp_port_open(host, port, timeout=0.1):
            return True
        time.sleep(poll_s)
    return False


def _broadcast_reload_notice() -> None:
    """Signal all open SSE generators to emit a reload event and exit.

    Called from _spawn_daemon_restart_thread (dashboard.py) before os.kill(SIGTERM)
    so clients receive a {"type":"reload"} frame within ~100 ms instead of having
    their TCP connection severed abruptly.  Best-effort: never raises.
    """
    try:
        from opencode_search.daemon_runtime import reload_pending
        reload_pending.set()
    except Exception:
        pass


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
    # Persistent ONNX cache: /tmp is tmpfs and cleared on boot; ensure daemon always
    # uses the persistent path regardless of whether the shell has the var set
    # (the reload path spawns a new subprocess without the systemd env vars).
    env.setdefault(
        "FASTEMBED_CACHE_PATH",
        str(Path.home() / ".cache" / "opencode" / "fastembed"),
    )
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
        "    scope=\"feature\": feature trace — entry points, call chain, algorithm, design rationale (WHY)\n"
        "    scope=\"business\": answer from business-classified communities (features, processes, rules)\n"
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
        "    what=\"feature_map\"                  — business knowledge map: all communities by semantic type\n"
        "    what=\"business_rules\"               — communities classified as constraints/policies/validations\n"
        "    what=\"process_flows\"                — communities classified as workflows/business processes\n"
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
        "    action=\"remove_project\"              — remove project from registry; delete_index=True also removes on-disk index\n"
        "\n"
        "QUICK DECISION GUIDE:\n"
        "  'find the payment handler'           → search('payment handler')\n"
        "  'how does auth work?'                → ask('how does auth work', project_path)\n"
        "  'what is the overall architecture?'  → ask('describe architecture', project_path, scope='global')\n"
        "  'how does checkout work end-to-end?' → ask('checkout feature', project_path, scope='feature')\n"
        "  'why is this code designed this way?' → ask('why is X designed this way', project_path, scope='feature')\n"
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
        "  'what business features exist?'      → overview(project_path, what='feature_map')\n"
        "  'what business rules govern X?'      → ask('rules for X', project_path, scope='business')\n"
        "  'what are the checkout workflows?'   → overview(project_path, what='process_flows')\n"
        "  'list all business constraints'      → overview(project_path, what='business_rules')\n"
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
            "StartLimitIntervalSec=300",
            "StartLimitBurst=20",
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
            "Environment=OPENCODE_QUERY_LLM_PROVIDER=ollama",
            "Environment=OPENCODE_QUERY_LLM_MODEL=qwen3-query:8b",
            # Persistent ONNX model cache — survives reboots (/tmp is tmpfs and is cleared on boot)
            f"Environment=FASTEMBED_CACHE_PATH={Path.home()}/.cache/opencode/fastembed",
            *env_lines,
            "Nice=10",
            "IOSchedulingClass=best-effort",
            "IOSchedulingPriority=7",
            "OOMScoreAdj=200",
            # Memory caps prevent runaway swap thrash when indexing large repos.
            # MemoryHigh is the soft throttle (kernel reclaims pages above this);
            # MemoryMax is the hard ceiling; MemorySwapMax limits swap usage.
            # Daemon RSS is ~6.5 GB legit; these leave ample headroom on 62 GB.
            "MemoryHigh=16G",
            "MemoryMax=24G",
            "MemorySwapMax=2G",
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


# ── KB self-healing maintenance sweep ─────────────────────────────────────────
# Runs in the background; finishes hierarchy enrichment for any project where
# level-2+ communities are unenriched.  This is the "enforced by default" path
# that recovers from interrupted auto-pipeline runs (thermal throttle, OOM,
# daemon restart) without waiting for a new index event.

_KB_SWEEP_INTERVAL_S: int = int(os.environ.get("OPENCODE_KB_SWEEP_INTERVAL_S", "600"))
_KB_SWEEP_ENABLED: bool = os.environ.get("OPENCODE_KB_SWEEP_ENABLED", "1").strip().lower() not in {
    "0", "false", "no", "off",
}
_kb_sweep_log = logging.getLogger(__name__ + ".kb_sweep")


def _kb_sweep_monitor() -> None:
    """Daemon thread: periodically sweep all registered projects for incomplete KB.

    - Waits _KB_SWEEP_INTERVAL_S between sweeps.
    - Each sweep: calls _run_kb_sweep() as a coroutine in the daemon's event loop.
    - The sweep itself gates on: GPU temperature, active client count, and
      _project_needs_hierarchy_enrich() from _autopipeline.
    """
    import asyncio as _asyncio
    # Wait for the server's event loop to be published (set by mcp.py lifespan on startup).
    # Then wait one full interval before the first sweep so startup indexing finishes first.
    _DAEMON_LOOP_READY.wait(timeout=300)
    time.sleep(min(_KB_SWEEP_INTERVAL_S, 120))
    while True:
        try:
            loop = _DAEMON_LOOP
            if loop is None:
                raise RuntimeError("daemon event loop not yet available")
            future = _asyncio.run_coroutine_threadsafe(_run_kb_sweep(), loop)
            future.result(timeout=3600)  # max 1h per sweep
        except Exception as exc:
            _kb_sweep_log.warning("kb_sweep: error in sweep cycle: %s", exc)
        time.sleep(_KB_SWEEP_INTERVAL_S)


async def _run_kb_sweep() -> None:
    """Async coroutine: drain every project's KB enrichment to ~100% on all levels.

    Order per project:
      1. L1 drain (loop-until-dry): call handle_enrich_project(scope="communities")
         repeatedly until 0 unenriched level-1 communities remain or no progress
         is made (breaks infinite spin on un-summarisable communities).
      2. L2+ hierarchy enrich: handle_enrich_hierarchy synthesises parent titles
         from children — only effective once L1 is complete.

    Detection (F2):
      - Acts if _project_needs_community_enrich (L1 has unenriched communities)
        OR _project_needs_hierarchy_enrich (L2+ has unenriched communities).

    De-dup (F3):
      - Keys a per-project "_kb_sweep_enrich" lock so this sweep cannot overlap
        with handle_auto_pipeline's enrichment step on the same project.

    Runs inside the daemon's event loop (dispatched from _kb_sweep_monitor thread).
    Respects thermal guard and active-client gate throughout.
    """
    if not _KB_SWEEP_ENABLED:
        return

    # Gate: skip while GPU is hot to avoid thermal throttling
    try:
        from opencode_search.embeddings import _get_gpu_temp_c
        from opencode_search.indexer import _MAX_GPU_TEMP
        temp = _get_gpu_temp_c()
        if temp is not None and temp > _MAX_GPU_TEMP:
            _kb_sweep_log.info("kb_sweep: GPU %d°C > threshold %d°C — skipping sweep", temp, _MAX_GPU_TEMP)
            return
    except Exception:
        pass  # if temp check fails, proceed cautiously

    # Gate: skip while clients are actively querying (don't compete with live requests)
    with runtime_state.lock:
        active = len(runtime_state.active_clients)
    if active > 0:
        _kb_sweep_log.debug("kb_sweep: %d active clients — deferring sweep", active)
        return

    try:
        from opencode_search.config import load_registry
        from opencode_search.daemon_runtime import yield_while_busy
        from opencode_search.handlers._autopipeline import (
            _project_needs_community_enrich,
            _project_needs_hierarchy_enrich,
        )
        from opencode_search.handlers._enrichment import (
            handle_enrich_hierarchy,
            handle_enrich_project,
        )
        from opencode_search.handlers._federation import _expand_with_federation

        registry = load_registry()
        root_paths = list(registry.keys())
        # Include all federated members too
        all_paths = _expand_with_federation(root_paths, registry)
        _kb_sweep_log.info("kb_sweep: scanning %d projects", len(all_paths))

        # Per-project in-flight lock: prevents overlap with handle_auto_pipeline Step 3.
        # Simple dict keyed by project_path; lock is an asyncio.Lock created on demand.
        # Stored as a module-level dict so it survives across sweep invocations.
        global _kb_sweep_project_locks
        if "_kb_sweep_project_locks" not in globals():
            _kb_sweep_project_locks = {}

        swept = 0
        for project_path in all_paths:
            try:
                needs_l1 = _project_needs_community_enrich(project_path)
                needs_l2 = _project_needs_hierarchy_enrich(project_path)
                if not needs_l1 and not needs_l2:
                    continue

                # Acquire per-project lock (non-blocking): skip if already enriching
                if project_path not in _kb_sweep_project_locks:
                    import asyncio as _asyncio
                    _kb_sweep_project_locks[project_path] = _asyncio.Lock()
                lock = _kb_sweep_project_locks[project_path]
                if lock.locked():
                    _kb_sweep_log.debug("kb_sweep: %s — enrich already in-flight, skipping", project_path)
                    continue

                async with lock:
                    # Re-check GPU and query activity before each project.
                    await yield_while_busy()
                    temp = _get_gpu_temp_c()
                    if temp is not None and temp > _MAX_GPU_TEMP:
                        _kb_sweep_log.info("kb_sweep: GPU %d°C — pausing sweep", temp)
                        return

                    # ── Step 1: L1 loop-until-dry ───────────────────────────────────
                    if needs_l1:
                        _kb_sweep_log.info("kb_sweep: draining L1 communities for %s", project_path)
                        prev_unenriched = -1
                        l1_total_enriched = 0
                        while True:
                            await yield_while_busy()
                            temp = _get_gpu_temp_c()
                            if temp is not None and temp > _MAX_GPU_TEMP:
                                _kb_sweep_log.info("kb_sweep: GPU %d°C — pausing L1 drain for %s", temp, project_path)
                                break
                            # Count unenriched L1 before this batch
                            try:
                                from opencode_search.config import get_project_graph_db_path
                                from opencode_search.graph.storage import GraphStorage
                                db_path = get_project_graph_db_path(project_path)
                                gs_check = GraphStorage(db_path)
                                gs_check.open()
                                try:
                                    cur_unenriched = sum(
                                        1 for c in gs_check.get_communities(level=1, min_node_count=2)
                                        if not c.title
                                    )
                                finally:
                                    gs_check.close()
                            except Exception:
                                break
                            if cur_unenriched == 0:
                                _kb_sweep_log.info("kb_sweep: L1 fully enriched for %s", project_path)
                                break
                            if cur_unenriched == prev_unenriched:
                                _kb_sweep_log.warning(
                                    "kb_sweep: L1 no progress for %s (%d unenriched remain) — stopping",
                                    project_path, cur_unenriched,
                                )
                                break
                            prev_unenriched = cur_unenriched
                            result = await handle_enrich_project(
                                project_path,
                                scope="communities",
                                max_communities=10000,
                                level=1,
                            )
                            batch_enriched = result.get("enriched_communities", 0)
                            l1_total_enriched += batch_enriched
                            _kb_sweep_log.info(
                                "kb_sweep: L1 batch +%d (still unenriched=%d) for %s",
                                batch_enriched, cur_unenriched - batch_enriched, project_path,
                            )

                        _kb_sweep_log.info(
                            "kb_sweep: L1 drain complete — total enriched=%d for %s",
                            l1_total_enriched, project_path,
                        )

                    # ── Step 2: L2+ hierarchy enrich ─────────────────────────────────
                    # Always run after an L1 pass (parents need refreshed children),
                    # and also when only L2+ was flagged.
                    await yield_while_busy()
                    temp = _get_gpu_temp_c()
                    if temp is not None and temp > _MAX_GPU_TEMP:
                        _kb_sweep_log.info("kb_sweep: GPU %d°C — skipping L2+ for %s", temp, project_path)
                    else:
                        if _project_needs_hierarchy_enrich(project_path):
                            _kb_sweep_log.info("kb_sweep: enriching L2+ hierarchy for %s", project_path)
                            h_result = await handle_enrich_hierarchy(project_path=project_path)
                            h_enriched = h_result.get("enriched", 0)
                            _kb_sweep_log.info("kb_sweep: L2+ enriched=%d for %s", h_enriched, project_path)

                swept += 1
            except Exception as exc:
                _kb_sweep_log.warning("kb_sweep: skipping %s: %s", project_path, exc)

        _kb_sweep_log.info("kb_sweep: done — %d projects updated", swept)
    except Exception as exc:
        _kb_sweep_log.warning("kb_sweep: unexpected error: %s", exc)


def _start_kb_sweep_monitor() -> None:
    if not _KB_SWEEP_ENABLED:
        return
    sweep = threading.Thread(
        target=_kb_sweep_monitor,
        daemon=True,
        name="opencode-search-kb-sweep",
    )
    sweep.start()
    _kb_sweep_log.info(
        "kb_sweep: monitor started (interval=%ds, enabled=%s)",
        _KB_SWEEP_INTERVAL_S, _KB_SWEEP_ENABLED,
    )


# ---------------------------------------------------------------------------
# Auto-index loop: pick up registered-but-unindexed projects automatically
# ---------------------------------------------------------------------------

_AUTO_INDEX_INTERVAL_S: int = int(os.environ.get("OPENCODE_AUTO_INDEX_INTERVAL_S", "120"))
_AUTO_INDEX_ENABLED: bool = os.environ.get("OPENCODE_AUTO_INDEX_ENABLED", "1").strip().lower() not in {
    "0", "false", "no", "off",
}
_auto_index_log = logging.getLogger(__name__ + ".auto_index")


def _auto_index_monitor() -> None:
    """Daemon thread: periodically pick up registered-but-not-yet-indexed projects.

    Fills the gap left by /api/projects/register and _auto_update_federation:
    both pre-register projects without indexing them. Once the MCP `build` tool
    is removed (Phase 100), this loop is the only way a flagged project gets indexed.
    """
    import asyncio as _asyncio
    _DAEMON_LOOP_READY.wait(timeout=300)
    # Short initial wait so startup watchers/pipeline resumes go first
    time.sleep(min(_AUTO_INDEX_INTERVAL_S, 30))
    while True:
        try:
            loop = _DAEMON_LOOP
            if loop is None:
                raise RuntimeError("daemon event loop not yet available")
            future = _asyncio.run_coroutine_threadsafe(_run_auto_index_sweep(), loop)
            future.result(timeout=3600)
        except Exception as exc:
            _auto_index_log.warning("auto_index: error in sweep cycle: %s", exc)
        time.sleep(_AUTO_INDEX_INTERVAL_S)


async def _run_auto_index_sweep() -> None:
    """Async coroutine: find and start indexing any registered-but-unindexed projects."""
    if not _AUTO_INDEX_ENABLED:
        return

    try:
        from opencode_search.config import load_registry
        from opencode_search.daemon_runtime import yield_while_busy
        from opencode_search.handlers._common import _indexing_status
        from opencode_search.handlers._federation import _expand_with_federation

        registry = load_registry()
        root_paths = list(registry.keys())
        all_paths = _expand_with_federation(root_paths, registry)

        queued = 0
        for project_path in all_paths:
            entry = registry.get(project_path)
            # Check if already indexed: has file_count > 0 means it has been indexed before
            already_indexed = entry is not None and getattr(entry, "file_count", 0) > 0
            if already_indexed:
                continue
            # Skip if currently indexing
            if _indexing_status.get(project_path):
                _auto_index_log.debug("auto_index: %s — already indexing, skipping", project_path)
                continue

            await yield_while_busy()
            _auto_index_log.info("auto_index: starting index for unindexed project %s", project_path)
            try:
                from opencode_search.handlers import handle_index_project
                await handle_index_project(path=project_path, watch=True)
                queued += 1
            except Exception as exc:
                _auto_index_log.warning("auto_index: failed to start index for %s: %s", project_path, exc)

        if queued:
            _auto_index_log.info("auto_index: queued %d project(s) for indexing", queued)
    except Exception as exc:
        _auto_index_log.warning("auto_index: unexpected error: %s", exc)


def _start_auto_index_monitor() -> None:
    if not _AUTO_INDEX_ENABLED:
        return
    t = threading.Thread(
        target=_auto_index_monitor,
        daemon=True,
        name="opencode-search-auto-index",
    )
    t.start()
    _auto_index_log.info(
        "auto_index: monitor started (interval=%ds)",
        _AUTO_INDEX_INTERVAL_S,
    )


# ---------------------------------------------------------------------------
# Maintenance loop: vacuum + dedup + graph VACUUM + wiki self-heal (every 6h)
# ---------------------------------------------------------------------------

_MAINTENANCE_INTERVAL_S: int = int(os.environ.get("OPENCODE_MAINTENANCE_INTERVAL_S", "21600"))
_MAINTENANCE_ENABLED: bool = os.environ.get("OPENCODE_MAINTENANCE_ENABLED", "1").strip().lower() not in {
    "0", "false", "no", "off",
}
_maintenance_log = logging.getLogger(__name__ + ".maintenance")


def _maintenance_monitor() -> None:
    """Daemon thread: run periodic maintenance (vacuum/dedup/graph-VACUUM/wiki-lint).

    Interval defaults to 6h (OPENCODE_MAINTENANCE_INTERVAL_S). Tests override to seconds.
    Each pass is gated by yield_while_busy() so it never competes with live queries.
    """
    import asyncio as _asyncio
    _DAEMON_LOOP_READY.wait(timeout=300)
    # Start after one full interval so the first runs aren't during startup
    time.sleep(min(_MAINTENANCE_INTERVAL_S, 60))
    while True:
        try:
            loop = _DAEMON_LOOP
            if loop is None:
                raise RuntimeError("daemon event loop not yet available")
            future = _asyncio.run_coroutine_threadsafe(_run_maintenance_sweep(), loop)
            future.result(timeout=7200)  # max 2h per sweep
        except Exception as exc:
            _maintenance_log.warning("maintenance: error in sweep cycle: %s", exc)
        time.sleep(_MAINTENANCE_INTERVAL_S)


async def _run_maintenance_sweep() -> None:
    """Run vacuum + dedup + graph VACUUM + wiki self-heal for every indexed project."""
    if not _MAINTENANCE_ENABLED:
        return

    try:
        from opencode_search.config import load_registry
        from opencode_search.daemon_runtime import yield_while_busy
        from opencode_search.handlers._federation import _expand_with_federation

        registry = load_registry()
        root_paths = list(registry.keys())
        all_paths = _expand_with_federation(root_paths, registry)

        indexed = [p for p in all_paths if registry.get(p) is not None and getattr(registry.get(p), "file_count", 0) > 0]
        if not indexed:
            _maintenance_log.debug("maintenance: no indexed projects — skipping")
            return

        _maintenance_log.info("maintenance: starting sweep for %d indexed projects", len(indexed))
        swept = 0
        for project_path in indexed:
            try:
                await yield_while_busy()

                freed_mb = 0.0
                merged = 0
                wiki_status = "ok"

                # 1. Vacuum orphan tier dirs
                try:
                    from opencode_search.handlers._vacuum import handle_vacuum
                    vac = await handle_vacuum(project_path=project_path, dry_run=False)
                    freed_mb = vac.get("freed_mb", 0.0) or 0.0
                except Exception as exc:
                    _maintenance_log.debug("maintenance: vacuum failed for %s: %s", project_path, exc)

                await yield_while_busy()

                # 1b. Deep-vacuum: reclaim stale LanceDB _indices/ dirs and old dataset versions.
                # Every watch-triggered re-index calls ensure_ivf_pq_index(replace=True) which
                # writes a NEW _indices/<uuid>/ dir; without this step they accumulate indefinitely.
                # Storage.vacuum() calls table.optimize(cleanup_older_than=0) which prunes them.
                try:
                    from opencode_search.config import load_registry as _load_registry_deep
                    from opencode_search.storage import Storage as _Storage
                    _reg_deep = _load_registry_deep()
                    _entry_deep = _reg_deep.get(project_path)
                    if _entry_deep is not None:
                        _db_path = str(getattr(_entry_deep, "db_path", "") or "")
                        _dims = int(getattr(_entry_deep, "dims", 768) or 768)
                        if _db_path:
                            from pathlib import Path as _PathDeep
                            if _PathDeep(_db_path).exists():
                                _storage_deep = _Storage(db_path=_db_path, dims=_dims)
                                await _storage_deep.open()
                                try:
                                    ldb_vac = await _storage_deep.vacuum()
                                    freed_mb += ldb_vac.get("saved_mb", 0.0) or 0.0
                                    _maintenance_log.debug(
                                        "maintenance: LanceDB deep-vacuum %s: saved=%.1fMB",
                                        project_path, ldb_vac.get("saved_mb", 0.0),
                                    )
                                finally:
                                    await _storage_deep.close()
                except Exception as exc:
                    _maintenance_log.debug("maintenance: deep-vacuum failed for %s: %s", project_path, exc)

                await yield_while_busy()

                # 2. Dedup graph nodes (MinHash/LSH)
                try:
                    from opencode_search.handlers._graph import handle_dedup_nodes
                    ded = await handle_dedup_nodes(project_path=project_path, dry_run=False)
                    merged = ded.get("merged", 0) or 0
                except Exception as exc:
                    _maintenance_log.debug("maintenance: dedup failed for %s: %s", project_path, exc)

                await yield_while_busy()

                # 3. GraphStorage SQLite VACUUM + prune singletons
                try:
                    from opencode_search.config import get_project_graph_db_path
                    from opencode_search.graph.storage import GraphStorage
                    import asyncio as _aio
                    db_path = get_project_graph_db_path(project_path)
                    def _graph_vacuum(_db=db_path) -> None:
                        gs = GraphStorage(_db)
                        gs.open()
                        try:
                            gs.vacuum()
                        finally:
                            gs.close()
                    await _aio.to_thread(_graph_vacuum)
                except Exception as exc:
                    _maintenance_log.debug("maintenance: graph vacuum failed for %s: %s", project_path, exc)

                await yield_while_busy()

                # 4. Wiki lint → auto-regenerate if broken/stale
                try:
                    from opencode_search.handlers._wiki import handle_wiki_lint
                    lint = await handle_wiki_lint(project_path=project_path)
                    wiki_status = lint.get("status", "ok")
                    if wiki_status in ("broken", "stale", "empty", "missing"):
                        _maintenance_log.info("maintenance: wiki %s for %s — regenerating", wiki_status, project_path)
                        from opencode_search.handlers._autopipeline import schedule_auto_pipeline
                        schedule_auto_pipeline(project_path)
                        wiki_status = f"{wiki_status}→regenerating"
                except Exception as exc:
                    _maintenance_log.debug("maintenance: wiki lint failed for %s: %s", project_path, exc)

                await yield_while_busy()

                # 5. Warm service_mesh cache (full scan + LLM description)
                sm_services = 0
                sm_edges = 0
                sm_truncated = False
                try:
                    from opencode_search.handlers._service_mesh import handle_detect_service_mesh
                    sm = await handle_detect_service_mesh(project_path=project_path, force=True)
                    sm_services = sm.get("service_count", 0)
                    sm_edges = sm.get("edge_count", 0)
                    sm_truncated = sm.get("truncated", False)
                except Exception as exc:
                    _maintenance_log.debug("maintenance: service_mesh warm failed for %s: %s", project_path, exc)

                _maintenance_log.info(
                    "maintenance: %s — freed=%.1fMB merged=%d wiki=%s mesh=%ds/%de trunc=%s",
                    project_path, freed_mb, merged, wiki_status,
                    sm_services, sm_edges, sm_truncated,
                )
                swept += 1
            except Exception as exc:
                _maintenance_log.warning("maintenance: skipping %s: %s", project_path, exc)

        _maintenance_log.info("maintenance: done — %d projects swept", swept)
    except Exception as exc:
        _maintenance_log.warning("maintenance: unexpected error: %s", exc)


def _start_maintenance_monitor() -> None:
    if not _MAINTENANCE_ENABLED:
        return
    t = threading.Thread(
        target=_maintenance_monitor,
        daemon=True,
        name="opencode-search-maintenance",
    )
    t.start()
    _maintenance_log.info(
        "maintenance: monitor started (interval=%ds)",
        _MAINTENANCE_INTERVAL_S,
    )


def _assert_ollama_gpu_placement() -> None:
    """Startup check: warn if any currently-loaded ollama model has CPU layers.

    This is best-effort (ollama may have no models loaded at cold start —
    the hard per-chat gate is _assert_gpu_only() in enricher/client.py).
    Logs a WARNING rather than crashing so a cold start isn't blocked.
    """
    import json as _json
    import urllib.request as _ureq

    ollama_url = os.environ.get("OPENCODE_OLLAMA_BASE_URL", "http://127.0.0.1:11434")
    try:
        req = _ureq.Request(f"{ollama_url}/api/ps")
        with _ureq.urlopen(req, timeout=3) as resp:
            ps = _json.loads(resp.read().decode("utf-8"))
        for entry in ps.get("models", []):
            size_total = entry.get("size", 0)
            size_vram = entry.get("size_vram", 0)
            cpu_bytes = size_total - size_vram
            if cpu_bytes > 10_000_000:
                cpu_gb = cpu_bytes / 1e9
                logging.getLogger(__name__).warning(
                    "[GPU-REQUIRED] Startup check: ollama model '%s' has %.2f GB offloaded to CPU. "
                    "CPU inference is FORBIDDEN. Free up VRAM or use a smaller model. "
                    "This will raise a fatal error on the first inference call.",
                    entry.get("name", "?"), cpu_gb,
                )
    except Exception as exc:
        logging.getLogger(__name__).debug("Startup ollama GPU placement check skipped: %s", exc)


def run_http_daemon_server(host: str = DEFAULT_DAEMON_HOST, port: int = DEFAULT_DAEMON_PORT) -> None:
    from opencode_search.embeddings import assert_gpu_available
    from opencode_search.mcp import run_mcp_http_server

    # GPU-only enforcement (see CLAUDE.md). Fail fast before binding the HTTP
    # socket so the daemon never silently runs inference on CPU. Skippable only
    # by tests via OPENCODE_SKIP_GPU_ASSERT=1, never in production.
    if os.environ.get("OPENCODE_SKIP_GPU_ASSERT") != "1":
        assert_gpu_available()
        _assert_ollama_gpu_placement()

    if _tcp_port_open(host, port):
        grace = float(os.environ.get("OPENCODE_DAEMON_BIND_WAIT_S", "5"))
        if not _wait_for_port_free(host, port, timeout_s=grace):
            raise RuntimeError(f"Cannot start daemon on {host}:{port}; still in use after {grace}s wait")
    _write_pidfile(host=host, port=port)
    try:
        _start_shutdown_monitor()
        _start_kb_sweep_monitor()
        _start_auto_index_monitor()
        _start_maintenance_monitor()
        run_mcp_http_server(host=host, port=port)
    finally:
        _clear_pidfile()
