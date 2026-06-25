"""Business Process Reverse Engineering (BPRE) — federation-level pass.

D2 entry-point detection · D3 cross-service edge resolution (gRPC/Pub-Sub/HTTP)
D4 process tracing (ordered steps spanning services) · D5 rule/state extraction
D6 synthesis (BPMN 2.0 XML + sequenceDiagram + DeepSeek narrative)

Writes *only* to process_graph.db at the federation root (root_process_db).
Never writes to any per-member graph.db (HR4).  Deterministic pass (D2-D4,
BPMN, mermaid) = GPU-free.  Cloud DeepSeek only for D5 rules + D6 narrative;
suppressed when OSE_WIKI_LLM=0 or key absent.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from opencode_search.kb.bpre_ast import ApiSurface

log = logging.getLogger(__name__)

# ─── Test-file exclusion ──────────────────────────────────────────────────────

_TEST_FILE_SUFFIXES: frozenset[str] = frozenset({
    "_test.go", "_test.py", ".test.ts", ".spec.ts", ".test.js", ".spec.js",
    "_test.js", "_test.ts",
})
_TEST_DIRS: frozenset[str] = frozenset({
    "testdata", "mocks", "__tests__", "test_helpers", "fixtures", "test",
})


def _is_test_file(path: str) -> bool:
    p = Path(path)
    for part in p.parts:
        if part in _TEST_DIRS:
            return True
    name = p.name
    return any(name.endswith(suf) for suf in _TEST_FILE_SUFFIXES)

# ─── Schema ───────────────────────────────────────────────────────────────────
_SCHEMA = """
CREATE TABLE IF NOT EXISTS entry_points (
    ep_id TEXT PRIMARY KEY, service TEXT NOT NULL, file TEXT NOT NULL,
    line INTEGER DEFAULT 0, kind TEXT NOT NULL, trigger TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS cross_service_edges (
    id TEXT PRIMARY KEY, caller_service TEXT NOT NULL, caller_file TEXT DEFAULT '',
    caller_line INTEGER DEFAULT 0, callee_service TEXT NOT NULL,
    callee_endpoint TEXT NOT NULL, kind TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 1.0, evidence TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS processes (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, entry_ep_id TEXT DEFAULT '',
    entry_service TEXT NOT NULL, services_json TEXT DEFAULT '[]',
    step_count INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS process_steps (
    process_id TEXT NOT NULL, order_index INTEGER NOT NULL,
    sid_or_endpoint TEXT DEFAULT '', service TEXT NOT NULL,
    kind TEXT NOT NULL, guard TEXT DEFAULT '',
    PRIMARY KEY (process_id, order_index)
);
CREATE TABLE IF NOT EXISTS process_artifacts (
    process_id TEXT PRIMARY KEY, narrative TEXT DEFAULT '',
    mermaid TEXT DEFAULT '', bpmn_xml TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

# ─── Tiny helpers ─────────────────────────────────────────────────────────────

def _init_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path), check_same_thread=False)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript(_SCHEMA)
    # Migration F-H/F-I: drop write-only tables removed in Phase 2a.
    for _dead_tbl in ("process_rules", "state_machines"):
        con.execute(f"DROP TABLE IF EXISTS {_dead_tbl}")
    con.commit()
    return con


def _bpre_get_meta(con: sqlite3.Connection, key: str) -> str | None:
    row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def _bpre_set_meta(con: sqlite3.Connection, key: str, value: str) -> None:
    con.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, value))


def _bpre_algo_version() -> str:
    """SHA-4 over source bytes of modules that determine BPRE output (definition fingerprint)."""
    import contextlib
    root = Path(__file__).resolve().parent
    modules = [
        root / "bpre.py", root / "bpre_ast.py",
        root / "valueflow.py", root / "resolve_rerank.py",
    ]
    h = hashlib.sha1()
    for p in modules:
        with contextlib.suppress(OSError):
            h.update(p.read_bytes())
    return h.hexdigest()[:4]


def _bpre_source_sig(members: list[str]) -> str:
    """sha1 over sorted per-member source fingerprints — stat-only, GPU-free."""
    from opencode_search.daemon.sweeps import _source_fingerprint
    parts = sorted(_source_fingerprint(m) for m in members)
    return hashlib.sha1("\n".join(parts).encode()).hexdigest()


def _hid(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def _service_label(member_path: str) -> str:
    return Path(member_path).name


def _rel_path(file: str, root: str) -> str:
    if not file or not root:
        return os.path.basename(file) if file else ""
    try:
        return os.path.relpath(file, root)
    except ValueError:
        return os.path.basename(file)


def _bpre_llm_on() -> bool:
    return os.environ.get("OSE_WIKI_LLM", "1") != "0"


def _proc_sig(name: str, services_json: str, steps: list) -> str:
    """Content hash over (process name, sorted services, ordered step tuples)."""
    try:
        svcs = sorted(json.loads(services_json))
    except Exception:
        svcs = []
    step_tuples = [(s[2], s[0], s[3]) for s in steps]  # (kind, sid_or_endpoint, guard)
    payload = json.dumps([name, svcs, step_tuples], sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(payload.encode()).hexdigest()


def _narrative_incomplete(con: sqlite3.Connection) -> bool:
    """True iff LLM is on + key present + any process_artifact has an empty narrative."""
    if not _bpre_llm_on():
        return False
    try:
        from opencode_search.graph.llm import deepseek_key
        if not deepseek_key():
            return False
    except Exception:
        return False
    n = con.execute(
        "SELECT COUNT(*) FROM process_artifacts WHERE narrative=''"
    ).fetchone()[0]
    return n > 0


def _source_files(member_path: str) -> list[Path]:
    exts = {".go", ".java", ".py", ".ts", ".js", ".kt"}
    root = Path(member_path)
    from opencode_search.core.config import IGNORED_DIRS
    from opencode_search.core.index_config import effective_config, is_excluded
    cfg = effective_config(root)
    out: list[Path] = []
    try:
        for dirpath, dirs, files in os.walk(str(root)):
            dirs[:] = [d for d in dirs if d not in IGNORED_DIRS]
            for f in files:
                p = Path(dirpath) / f
                if (p.suffix in exts and not _is_test_file(str(p))
                        and not (cfg.exclude and is_excluded(p, cfg.exclude, root))):
                    out.append(p)
    except OSError:
        pass
    return out


# ─── D2 Entry-point detection ─────────────────────────────────────────────────

def _detect_entry_points(
    con: sqlite3.Connection, member_path: str, surf: ApiSurface,
) -> None:
    from opencode_search.index.discover import detect_language
    from opencode_search.kb.bpre_ast import scan_file
    service = _service_label(member_path)
    rows: list[tuple] = []
    for src in _source_files(member_path):
        if _is_test_file(str(src)):
            continue
        try:
            content = src.read_text(errors="replace")
        except OSError:
            continue
        ff = scan_file(str(src), content, detect_language(src), surf)
        if not ff:
            continue
        rel = _rel_path(str(src), member_path)
        for verb, path, ln in ff.http_routes:
            rows.append((_hid(service, rel, verb, path), service, rel, ln, "http", f"{verb} {path}"))
        for svc_name, ln in ff.grpc_servers:
            rows.append((_hid(service, rel, "grpc", svc_name), service, rel, ln, "grpc", svc_name))
        if ff.has_receive_call:
            rows.append((_hid(service, rel, "pubsub", "recv"), service, rel, 0, "pubsub", "subscriber"))
    con.executemany("INSERT OR IGNORE INTO entry_points VALUES (?,?,?,?,?,?)", rows)
    con.commit()


# ─── D3 Service/pub-sub/HTTP registries ───────────────────────────────────────

def _build_service_registry(members: list[str], surf: ApiSurface) -> dict[str, str]:
    """Map {ServiceLabel → member_path} by scanning non-proto source files for Register*Server calls."""
    from opencode_search.index.discover import detect_language
    from opencode_search.kb.bpre_ast import scan_file
    registry: dict[str, str] = {}
    for member in members:
        for src in _source_files(member):
            if str(src).endswith(".pb.go"):
                continue
            try:
                content = src.read_text(errors="replace")
            except OSError:
                continue
            ff = scan_file(str(src), content, detect_language(src), surf)
            if not ff:
                continue
            for svc_name, _ln in ff.grpc_servers:
                registry.setdefault(svc_name, member)
    return registry


def _build_pubsub_registry(
    members: list[str], surf: ApiSurface,
) -> dict[str, tuple[str, str, str, int]]:
    from opencode_search.index.discover import detect_language
    from opencode_search.kb.bpre_ast import scan_file
    publishers: dict[str, list[tuple[str, str, int]]] = {}
    consumers: dict[str, list[str]] = {}
    for member in members:
        for src in _source_files(member):
            try:
                content = src.read_text(errors="replace")
            except OSError:
                continue
            ff = scan_file(str(src), content, detect_language(src), surf)
            if not ff:
                continue
            rel = _rel_path(str(src), member)
            for tk, ln in ff.proto_marshal_types:
                publishers.setdefault(tk, []).append((member, rel, ln))
            for tk, _ln in ff.pubsub_consumes:
                consumers.setdefault(tk, []).append(member)
    result: dict[str, tuple[str, str, str, int]] = {}
    for key in set(publishers) & set(consumers):
        pub_member, pub_file, pub_line = publishers[key][0]
        sub = consumers[key][0]
        if pub_member != sub:
            result[key] = (pub_member, sub, pub_file, pub_line)
    return result


def _build_http_route_map(
    members: list[str], surf: ApiSurface,
) -> dict[str, str]:
    from opencode_search.index.discover import detect_language
    from opencode_search.kb.bpre_ast import scan_file
    route_map: dict[str, str] = {}
    for member in members:
        svc = _service_label(member)
        for src in _source_files(member):
            try:
                content = src.read_text(errors="replace")
            except OSError:
                continue
            ff = scan_file(str(src), content, detect_language(src), surf)
            if not ff:
                continue
            for verb, path, _ln in ff.http_routes:
                route_map[f"{verb} {path}"] = svc
    return route_map


# ─── D3 Edge resolution ───────────────────────────────────────────────────────

def _resolve_grpc_edges(
    con: sqlite3.Connection, member: str, service_registry: dict[str, str],
    surf: ApiSurface,
) -> None:
    from opencode_search.index.discover import detect_language
    from opencode_search.kb.bpre_ast import scan_file
    caller_svc = _service_label(member)
    rows: list[tuple] = []
    for src in _source_files(member):
        try:
            content = src.read_text(errors="replace")
        except OSError:
            continue
        ff = scan_file(str(src), content, detect_language(src), surf)
        if not ff:
            continue
        rel = _rel_path(str(src), member)
        for _alias, svc_name, ctor, ln in ff.grpc_clients:
            callee_member = service_registry.get(svc_name)
            if not callee_member or callee_member == member:
                continue
            callee_svc = _service_label(callee_member)
            edge_id = _hid(caller_svc, callee_svc, "grpc", svc_name, rel, str(ln))
            rows.append((edge_id, caller_svc, rel, ln, callee_svc,
                         f"{svc_name}Service", "grpc", 1.0, f"{ctor} @ {rel}:{ln}"))
    con.executemany("INSERT OR IGNORE INTO cross_service_edges VALUES (?,?,?,?,?,?,?,?,?)", rows)
    con.commit()


def _resolve_pubsub_edges(
    con: sqlite3.Connection, pubsub_registry: dict[str, tuple[str, str, str, int]],
) -> None:
    rows = [
        (_hid(_service_label(pub), _service_label(sub), "pubsub", msg),
         _service_label(pub), caller_file, caller_line, _service_label(sub), msg, "pubsub", 1.0,
         f"proto type {msg}")
        for msg, (pub, sub, caller_file, caller_line) in pubsub_registry.items()
    ]
    con.executemany("INSERT OR IGNORE INTO cross_service_edges VALUES (?,?,?,?,?,?,?,?,?)", rows)
    con.commit()


def _resolve_http_edges(
    con: sqlite3.Connection, member: str, http_routes: dict[str, str],
    surf: ApiSurface,
) -> None:
    from opencode_search.index.discover import detect_language
    from opencode_search.kb.bpre_ast import scan_file
    caller_svc = _service_label(member)
    rows: list[tuple] = []
    for src in _source_files(member):
        try:
            content = src.read_text(errors="replace")
        except OSError:
            continue
        ff = scan_file(str(src), content, detect_language(src), surf)
        if not ff:
            continue
        rel = _rel_path(str(src), member)
        for verb, path, ln in ff.http_clients:
            callee_svc = http_routes.get(f"{verb} {path}")
            if not callee_svc or callee_svc == caller_svc:
                continue
            edge_id = _hid(caller_svc, callee_svc, "http", verb, path)
            rows.append((edge_id, caller_svc, rel, ln, callee_svc,
                         f"{verb} {path}", "http", 0.8, f"HTTP @ {rel}:{ln}"))
    con.executemany("INSERT OR IGNORE INTO cross_service_edges VALUES (?,?,?,?,?,?,?,?,?)", rows)
    con.commit()


def _handler_reachable_set(
    member_path: str, ep_file: str, ep_line: int,
) -> tuple[int | None, set[int]]:
    from opencode_search.core.config import project_graph_db
    gdb = project_graph_db(member_path)
    if not gdb.exists():
        return None, set()
    try:
        con = sqlite3.connect(str(gdb), check_same_thread=False)
        con.execute("PRAGMA query_only=ON")
        try:
            abs_ep = str((Path(member_path) / ep_file).resolve())
            fname = Path(ep_file).name
            rows = con.execute(
                "SELECT sid FROM symbols WHERE (file=? OR file LIKE ?) "
                "AND start_line<=? AND end_line>=? ORDER BY (end_line - start_line) ASC LIMIT 1",
                (abs_ep, f"%/{fname}", ep_line, ep_line),
            ).fetchall()
            if not rows:
                return None, set()
            handler_sid: int = rows[0][0]
            reachable: set[int] = {handler_sid}
            frontier: list[int] = [handler_sid]
            for _ in range(8):
                if not frontier:
                    break
                ph = ",".join("?" * len(frontier))
                nexts = [r[0] for r in con.execute(
                    f"SELECT callee_sid FROM edges WHERE caller_sid IN ({ph})", frontier,
                ).fetchall() if r[0] not in reachable]
                reachable.update(nexts)
                frontier = nexts
            return handler_sid, reachable
        finally:
            con.close()
    except Exception:
        return None, set()


def _precompute_caller_sids(
    adj: dict[str, list[tuple]], svc_to_member: dict[str, str],
) -> dict[tuple, object]:
    """Batch-precompute (member, rel_file, line) → sid for all call sites in adj."""
    from opencode_search.core.config import project_graph_db
    cache: dict[tuple, object] = {}
    locs_by_member: dict[str, set[tuple[str, int]]] = {}
    for svc, edges in adj.items():
        m = svc_to_member.get(svc)
        if not m:
            continue
        for _, _, _, cf, cl in edges:
            if cf and cl > 0:
                locs_by_member.setdefault(m, set()).add((cf, cl))
    for member, locs in locs_by_member.items():
        gdb = project_graph_db(member)
        if not gdb.exists():
            continue
        try:
            mcon = sqlite3.connect(str(gdb), check_same_thread=False)
            mcon.execute("PRAGMA query_only=ON")
            try:
                for cf, cl in locs:
                    abs_file = str((Path(member) / cf).resolve())
                    fname = Path(cf).name
                    rows = mcon.execute(
                        "SELECT sid FROM symbols WHERE (file=? OR file LIKE ?) "
                        "AND start_line<=? AND end_line>=? "
                        "ORDER BY (end_line - start_line) ASC LIMIT 1",
                        (abs_file, f"%/{fname}", cl, cl),
                    ).fetchall()
                    cache[(member, cf, cl)] = rows[0][0] if rows else None
            finally:
                mcon.close()
        except Exception:
            pass
    return cache


def _call_in_reachable(
    caller_file: str, caller_line: int, member_path: str, reachable: set[int],
    _sid_cache: dict | None = None,
) -> bool:
    if not reachable:
        return True  # no handler found — honest service-level fallback
    if _sid_cache is not None:
        sid = _sid_cache.get((member_path, caller_file, caller_line))
        return sid is None or sid in reachable
    from opencode_search.core.config import project_graph_db
    gdb = project_graph_db(member_path)
    if not gdb.exists():
        return True
    try:
        con = sqlite3.connect(str(gdb), check_same_thread=False)
        con.execute("PRAGMA query_only=ON")
        try:
            abs_file = str((Path(member_path) / caller_file).resolve())
            fname = Path(caller_file).name
            rows = con.execute(
                "SELECT sid FROM symbols WHERE (file=? OR file LIKE ?) "
                "AND start_line<=? AND end_line>=? ORDER BY (end_line - start_line) ASC LIMIT 1",
                (abs_file, f"%/{fname}", caller_line, caller_line),
            ).fetchall()
            return bool(rows) and rows[0][0] in reachable
        finally:
            con.close()
    except Exception:
        return True  # DB error: conservative inclusion fallback


def _callee_ep(
    con: sqlite3.Connection, callee_svc: str, endpoint: str, kind: str,
) -> tuple[str, int] | None:
    rows = con.execute(
        "SELECT file, line FROM entry_points "
        "WHERE service=? AND kind=? AND trigger LIKE ? LIMIT 1",
        (callee_svc, kind, f"%{endpoint}%"),
    ).fetchall()
    if not rows:
        rows = con.execute(
            "SELECT file, line FROM entry_points WHERE service=? AND kind=? LIMIT 1",
            (callee_svc, kind),
        ).fetchall()
    return (rows[0][0], int(rows[0][1])) if rows else None


# ─── D4 Handler-anchored process tracing ─────────────────────────────────────

def _trace_processes(con: sqlite3.Connection, members: list[str]) -> int:
    eps = con.execute(
        "SELECT ep_id, service, file, line, kind, trigger FROM entry_points "
        "WHERE kind IN ('http','grpc')"
    ).fetchall()
    edges_raw = con.execute(
        "SELECT caller_service, callee_service, callee_endpoint, kind, "
        "caller_file, caller_line FROM cross_service_edges"
    ).fetchall()
    svc_to_member: dict[str, str] = {_service_label(m): m for m in members}
    adj: dict[str, list[tuple]] = {}
    for caller_svc, callee_svc, ep, kind, cf, cl in edges_raw:
        adj.setdefault(caller_svc, []).append((callee_svc, ep, kind, cf or "", int(cl or 0)))
    caller_sids = _precompute_caller_sids(adj, svc_to_member)
    reach_cache: dict[tuple, tuple] = {}
    count = 0
    seen_step_seqs: set[tuple] = set()
    for ep_id, entry_svc, ep_file, ep_line, ep_kind, trigger in eps:
        outgoing = adj.get(entry_svc)
        if not outgoing:
            continue
        member_path = svc_to_member.get(entry_svc)
        rk = (member_path or "", ep_file, int(ep_line))
        if rk not in reach_cache:
            reach_cache[rk] = _handler_reachable_set(member_path or "", ep_file, int(ep_line))
        _, reachable = reach_cache[rk]
        fired: list[tuple[str, str, str]] = []
        for callee_svc, endpoint, kind, caller_file, caller_line in outgoing:
            if (not caller_file or caller_line == 0) or (
                member_path and _call_in_reachable(caller_file, caller_line, member_path, reachable, caller_sids)
            ):
                fired.append((callee_svc, endpoint, kind))
        if not fired:
            continue
        visited: set[str] = {entry_svc}
        steps: list[tuple] = [(ep_id, 0, f"{ep_file}:{ep_line}", entry_svc, "entry", "")]
        order = 1
        for callee_svc, endpoint, kind in fired:
            if callee_svc in visited:
                continue
            visited.add(callee_svc)
            steps.append((ep_id, order, endpoint, callee_svc, kind, ""))
            order += 1
            callee_out = adj.get(callee_svc)
            callee_member = svc_to_member.get(callee_svc)
            if not callee_out or not callee_member:
                continue
            callee_loc = _callee_ep(con, callee_svc, endpoint, kind)
            if not callee_loc:
                continue
            crk = (callee_member, callee_loc[0], callee_loc[1])
            if crk not in reach_cache:
                reach_cache[crk] = _handler_reachable_set(callee_member, callee_loc[0], callee_loc[1])
            _, c_reach = reach_cache[crk]
            for c2_svc, ep2, k2, cf2, cl2 in callee_out:
                if c2_svc in visited:
                    continue
                if not cf2 or cl2 == 0 or _call_in_reachable(cf2, cl2, callee_member, c_reach, caller_sids):
                    visited.add(c2_svc)
                    steps.append((ep_id, order, ep2, c2_svc, k2, ""))
                    order += 1
        if len(visited) < 2:
            continue
        step_seq = tuple((s[3], s[2], s[4]) for s in steps[1:])
        if step_seq in seen_step_seqs:
            continue
        seen_step_seqs.add(step_seq)
        name = f"{entry_svc}: {trigger or ep_kind}"
        proc_id = _hid(ep_id, entry_svc, name)
        services = sorted(visited)
        con.execute("INSERT OR REPLACE INTO processes VALUES (?,?,?,?,?,?)",
                    (proc_id, name, ep_id, entry_svc, json.dumps(services), len(steps)))
        con.executemany("INSERT OR REPLACE INTO process_steps VALUES (?,?,?,?,?,?)",
                        [(proc_id, *s[1:]) for s in steps])
        count += 1
    con.commit()
    return count


def _bpre_llm_link_on() -> bool:
    return os.environ.get("OSE_BPRE_LLM_LINK", "0") == "1"


def _bpre_llm_file_on() -> bool:
    """Gate for Tier-3 whole-file LLM (parse-error files only)."""
    return os.environ.get("OSE_BPRE_LLM_FILE", "1") != "0" and _bpre_llm_on()


def _content_hash(content: str) -> str:
    """XXH3-compatible hash via hashlib (stable key for LLM verdict caching)."""
    return hashlib.sha256(content.encode("utf-8", "replace")).hexdigest()[:20]


def _llm_link_scan(
    members: list[str], surf: ApiSurface,
    known_routes: set[str], known_topics: set[str],
) -> list[dict]:
    from opencode_search.index.discover import detect_language
    from opencode_search.kb.bpre_ast import scan_file
    seen: set[str] = set()
    items: list[dict] = []
    for member in members:
        caller_svc = _service_label(member)
        for src in _source_files(member):
            try:
                content = src.read_text(errors="replace")
            except OSError:
                continue
            ff = scan_file(str(src), content, detect_language(src), surf)
            if not ff:
                continue
            for tk, _ln in ff.proto_marshal_types:
                key = f"pubsub:{caller_svc}:{tk}"
                if tk not in known_topics and key not in seen:
                    seen.add(key)
                    items.append({"kind": "pubsub", "caller": caller_svc, "topic_or_route": tk})
            for verb, path, _ln in ff.http_clients:
                route = f"{verb} {path}"
                key = f"http:{caller_svc}:{route}"
                if route not in known_routes and key not in seen:
                    seen.add(key)
                    items.append({"kind": "http", "caller": caller_svc, "topic_or_route": route})
    return items


def _llm_link_resolve(
    con: sqlite3.Connection, items: list[dict], svcs: list[str],
) -> None:
    """Tier-2 SEA-style LLM resolution: SELECT from admitted candidate set only.

    The LLM is given the exact list of admitted service names and may only
    return callee values from that list (SEA arXiv:2408.04344: +8.1 F1).
    Verification gate: drops any edge where caller==callee.
    """
    from opencode_search.graph.llm import deepseek_extract, deepseek_key
    if not deepseek_key():
        return
    svc_set = set(svcs)
    # Stable prefix (byte-identical → high cache hit rate)
    stable_prefix = (
        "You are a microservice dependency resolver. "
        "Given a list of unresolved edges, return a JSON array where each element "
        "has: kind, caller, topic_or_route, callee. "
        "IMPORTANT: callee MUST be one of the admitted services listed by the user. "
        "Use null callee if you cannot determine it confidently. "
        "Return ONLY valid JSON — no markdown, no explanation."
    )
    hints = "\n".join(
        f"- {u['kind']} '{u['topic_or_route']}' from '{u['caller']}'" for u in items[:30]
    )
    if len(items) > 30:
        log.warning("bpre llm_link_resolve: cap 30 hit (had %d items)", len(items))
    dynamic_tail = f"Admitted services: {json.dumps(svcs)}\n\nEdges to resolve:\n{hints}"
    try:
        raw, usage = deepseek_extract(stable_prefix, dynamic_tail, max_tokens=512)
    except Exception as exc:
        log.warning("bpre _llm_link_resolve: %s", exc)
        return
    if usage.get("prompt_cache_hit_tokens", 0) > 0:
        log.debug("bpre llm_link: cache hit %d tokens", usage["prompt_cache_hit_tokens"])
    s, e = raw.find("["), raw.rfind("]")
    if s == -1 or e <= s:
        return
    try:
        parsed = json.loads(raw[s:e + 1])
    except Exception:
        return
    rows = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        callee, caller = item.get("callee"), item.get("caller")
        # SEA invariant + structural verification: callee must be in admitted set
        if not callee or callee not in svc_set or not caller or caller == callee:
            continue
        tor = item.get("topic_or_route", "")
        kind = f"{item.get('kind', 'llm')}_llm"
        rows.append((_hid(caller, callee, kind, tor), caller, "", 0, callee,
                     tor, kind, 0.7, f"llm-inferred:{tor}"))
    if rows:
        con.executemany("INSERT OR IGNORE INTO cross_service_edges VALUES (?,?,?,?,?,?,?,?,?)", rows)
        con.commit()


def _llm_link_edges(con: sqlite3.Connection, members: list[str], surf: ApiSurface) -> None:
    if not (_bpre_llm_link_on() and _bpre_llm_on()):
        return
    from opencode_search.graph.llm import deepseek_key
    if not deepseek_key():
        return
    known_routes_map: dict[str, str] = {r[0]: r[1] for r in con.execute(
        "SELECT callee_endpoint, callee_service FROM cross_service_edges WHERE kind='http'"
    ).fetchall()}
    known_topics_map: dict[str, str] = {r[0]: r[1] for r in con.execute(
        "SELECT callee_endpoint, callee_service FROM cross_service_edges WHERE kind='pubsub'"
    ).fetchall()}
    known_routes: set[str] = set(known_routes_map)
    known_topics: set[str] = set(known_topics_map)
    items = _llm_link_scan(members, surf, known_routes, known_topics)
    if not items:
        return
    svcs = [_service_label(m) for m in members]
    # Tier-1.75: try GPU rerank before calling the cloud LLM
    try:
        from opencode_search.kb.resolve_rerank import rerank_residue
        all_known = {**known_routes_map, **known_topics_map}
        resolved, items = rerank_residue(items, all_known)
        rows_175 = [
            (_hid(r["caller"], r["callee"], f"{r['kind']}_reranked", r["topic_or_route"]),
             r["caller"], "", 0, r["callee"], r["topic_or_route"],
             f"{r['kind']}_reranked", 0.8, "rerank-resolved")
            for r in resolved if r.get("callee") and r["callee"] != r["caller"]
        ]
        if rows_175:
            con.executemany("INSERT OR IGNORE INTO cross_service_edges VALUES (?,?,?,?,?,?,?,?,?)", rows_175)
            con.commit()
    except Exception as exc:
        log.debug("bpre Tier-1.75 rerank skipped: %s", exc)
    if items:
        _llm_link_resolve(con, items, svcs)


# ─── D6 Synthesis ─────────────────────────────────────────────────────────────

def _bpmn_xml(process_id: str, process_name: str,
              steps: list[tuple[str, str, str, str]]) -> str:
    id8 = process_id[:8]
    services = list(dict.fromkeys(s[1] for s in steps))
    lanes = "\n".join(
        f'      <bpmn:lane id="lane_{i}" name="{svc}"/>' for i, svc in enumerate(services)
    )
    start = f'    <bpmn:startEvent id="start_{id8}" name="Start"/>'
    end_id = f"end_{id8}"
    end = f'    <bpmn:endEvent id="{end_id}" name="End"/>'
    tasks: list[str] = []
    flows: list[str] = []
    prev_id = f"start_{id8}"
    for i, (endpoint, _svc, kind, _guard) in enumerate(steps):
        elem_id = f"elem_{id8}_{i}"
        label = (endpoint.split("/")[-1] or endpoint)[:40].replace("&", "&amp;")
        tag = "bpmn:exclusiveGateway" if kind == "decision" else "bpmn:task"
        tasks.append(f'    <{tag} id="{elem_id}" name="{label}"/>')
        flows.append(f'    <bpmn:sequenceFlow id="flow_{id8}_{i}" '
                     f'sourceRef="{prev_id}" targetRef="{elem_id}"/>')
        prev_id = elem_id
    flows.append(f'    <bpmn:sequenceFlow id="flow_{id8}_end" '
                 f'sourceRef="{prev_id}" targetRef="{end_id}"/>')
    proc_body = "\n".join([
        f'  <bpmn:process id="proc_{id8}" name="{process_name[:80]}" isExecutable="false">',
        f'    <bpmn:laneSet id="lanes_{id8}">', lanes, "    </bpmn:laneSet>",
        start, *tasks, end, *flows,
        "  </bpmn:process>",
    ])
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL"\n'
        f'    targetNamespace="http://opencode-search/bpre" id="defs_{id8}">\n'
        f'{proc_body}\n</bpmn:definitions>'
    )


def _mermaid_sequence(process_name: str,
                      steps: list[tuple[str, str, str, str]]) -> str:
    services = list(dict.fromkeys(s[1] for s in steps))
    cap = 40
    lines = ["sequenceDiagram"]
    for svc in services[:cap]:
        lines.append(f"    participant {svc}")
    prev_svc = services[0] if services else ""
    open_alts = 0
    for endpoint, svc, kind, guard in steps[:cap]:
        if kind == "decision" and guard:
            lines.append(f"    alt {guard}")
            open_alts += 1
        label = (endpoint.split("/")[-1] or endpoint)[:40]
        if svc != prev_svc:
            lines.append(f"    {prev_svc}->>{svc}: {label}")
        else:
            lines.append(f"    Note over {svc}: {label}")
        prev_svc = svc
    for _ in range(open_alts):
        lines.append("    end")
    return "\n".join(lines)


_BPRE_NARRATIVE_SYSTEM = (
    "Describe each business process in 3-5 sentences using ONLY the facts provided. "
    "Do not invent service names or steps. Name real services only. "
    'Reply with JSON: [{"id": <N>, "narrative": "<3-5 sentences>"}]'
)


def _generate_narratives_batch(procs_data: list[tuple]) -> dict[int, str]:
    """Batch BPRE narrative generation via deepseek_extract (≤20/call). Returns {proc_id: narrative}."""
    try:
        from opencode_search.graph.llm import _accumulate_llm_tokens, deepseek_extract, deepseek_key
        if not deepseek_key():
            return {}
    except Exception:
        return {}
    results: dict[int, str] = {}
    for i in range(0, len(procs_data), 20):
        batch = procs_data[i : i + 20]
        items = []
        for proc_id, name, services_json, steps in batch:
            try:
                services = ", ".join(json.loads(services_json))
                items.append({"id": proc_id, "process": name, "services": services,
                    "steps": "; ".join(f"{s[1]}:{s[0]}" for s in steps[:12])})
            except Exception:
                continue
        if not items:
            continue
        try:
            raw, u = deepseek_extract(
                _BPRE_NARRATIVE_SYSTEM,
                "Processes:\n" + json.dumps(items, ensure_ascii=False),
                max_tokens=min(len(items) * 400 + 50, 8192),
            )
            _accumulate_llm_tokens(u, "bpre")
        except Exception:
            continue
        t = raw.split("</think>")[-1] if "</think>" in raw else raw
        t = t.replace("```json", "").replace("```", "")
        if (s := t.find("[")) == -1 or (e := t.rfind("]")) <= s:
            continue
        try:
            for it in json.loads(t[s : e + 1]):
                results[int(it["id"])] = str(it.get("narrative", "")).strip()
        except Exception:
            pass
    return results


def _synthesize_artifacts(con: sqlite3.Connection,
                          old_narr: dict[str, str] | None = None) -> None:
    """Build BPMN/mermaid/narrative for every process.

    old_narr: {_proc_sig → narrative} snapshot from the previous build.  When provided,
    narratives for processes whose content is unchanged are carried over byte-identically;
    only new/changed/empty processes are sent to DeepSeek (delta narration).
    """
    procs = con.execute(
        "SELECT id, name, entry_service, services_json FROM processes"
    ).fetchall()
    procs_data = []
    for proc_id, name, _entry_svc, services_json in procs:
        steps = [(r[0], r[1], r[2], r[3]) for r in con.execute(
            "SELECT sid_or_endpoint, service, kind, guard FROM process_steps "
            "WHERE process_id=? ORDER BY order_index", (proc_id,),
        ).fetchall()]
        procs_data.append((proc_id, name, services_json, steps))
    # Delta narration: carry over unchanged narratives; only narrate the rest.
    if old_narr and _bpre_llm_on():
        carried: dict[int, str] = {}
        need_narr: list[tuple] = []
        for item in procs_data:
            proc_id, name, services_json, steps = item
            sig = _proc_sig(name, services_json, steps)
            if sig in old_narr:
                carried[proc_id] = old_narr[sig]
            else:
                need_narr.append(item)
        new_narr = _generate_narratives_batch(need_narr) if need_narr else {}
        narratives: dict = {**carried, **new_narr}
    else:
        narratives = _generate_narratives_batch(procs_data) if _bpre_llm_on() else {}
    for proc_id, name, _sjson, steps in procs_data:
        con.execute("INSERT OR REPLACE INTO process_artifacts VALUES (?,?,?,?)",
                    (proc_id, narratives.get(proc_id, ""),
                     _mermaid_sequence(name, steps), _bpmn_xml(proc_id, name, steps)))
    con.commit()


# ─── Master entry point ───────────────────────────────────────────────────────

def reconstruct_processes(root_path: str) -> int:
    """D2→D6 federation-level BPRE pass.  Returns number of reconstructed processes.

    File-level mutex (process_graph.lock) serializes concurrent callers.
    Reuse guard: stamp-based (algo + source signature), not time-based — drift decides,
    not age.  Burst fan-out collapses: first caller rebuilds+stamps; subsequent callers
    in the same burst hit matching stamps → reuse.  Watcher-triggered calls always land
    when member source actually changed (_bpre_source_sig detects it).
    """
    import fcntl

    from opencode_search.core.config import root_process_db
    from opencode_search.daemon.federation import expand_federation
    members = expand_federation(root_path)
    if len(members) < 2:
        log.debug("bpre: skip %s — fewer than 2 federation members", root_path)
        return 0
    db_path = root_process_db(root_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = db_path.with_suffix(".bpre.lock")
    _lf = open(str(lock_path), "w")  # noqa: SIM115
    try:
        fcntl.flock(_lf.fileno(), fcntl.LOCK_EX)
        con = _init_db(db_path)
        try:
            # Stamp-based full-skip: reuse iff definition + source unchanged + narratives complete.
            # A 0-process result is also reusable when stamped (stable no-process federation).
            algo = _bpre_algo_version()
            src_sig = _bpre_source_sig(members)
            _n = con.execute("SELECT COUNT(*) FROM processes").fetchone()[0]
            stamps_match = (
                _bpre_get_meta(con, "bpre_algo") == algo
                and _bpre_get_meta(con, "bpre_source_sig") == src_sig
            )
            if stamps_match and (_n == 0 or not _narrative_incomplete(con)):
                log.info("bpre: reusing stamp-matched result (%d processes) for %s", _n, root_path)
                con.close()
                return _n
            # Snapshot existing narratives keyed by process content (delta narration).
            old_narr: dict[str, str] = {}
            try:
                rows = con.execute(
                    "SELECT p.name, p.services_json, pa.narrative "
                    "FROM processes p JOIN process_artifacts pa ON pa.process_id=p.id "
                    "WHERE pa.narrative!=''"
                ).fetchall()
                for pname, svc_json, narr in rows:
                    steps = con.execute(
                        "SELECT sid_or_endpoint, service, kind, guard FROM process_steps "
                        "WHERE process_id=(SELECT id FROM processes WHERE name=? LIMIT 1) "
                        "ORDER BY order_index", (pname,)
                    ).fetchall()
                    old_narr[_proc_sig(pname, svc_json, steps)] = narr
            except Exception:
                old_narr = {}
            for tbl in ("entry_points", "cross_service_edges", "processes",
                        "process_steps", "process_artifacts"):
                con.execute(f"DELETE FROM {tbl}")
            con.commit()
            from opencode_search.kb.bpre_ast import federation_discover
            surf = federation_discover(members)
            for member in members:
                _detect_entry_points(con, member, surf)
            svc_registry = _build_service_registry(members, surf)
            pubsub_registry = _build_pubsub_registry(members, surf)
            http_routes = _build_http_route_map(members, surf)
            for member in members:
                _resolve_grpc_edges(con, member, svc_registry, surf)
            _resolve_pubsub_edges(con, pubsub_registry)
            for member in members:
                _resolve_http_edges(con, member, http_routes, surf)
            _llm_link_edges(con, members, surf)
            count = _trace_processes(con, members)
            if count == 0:
                log.info("bpre: no multi-service processes found for %s", root_path)
                # Still stamp so reconcile doesn't rebuild a stable no-process federation.
                _bpre_set_meta(con, "bpre_algo", algo)
                _bpre_set_meta(con, "bpre_source_sig", src_sig)
                con.commit()
                con.close()
                return 0
            _synthesize_artifacts(con, old_narr)
            _bpre_set_meta(con, "bpre_algo", algo)
            _bpre_set_meta(con, "bpre_source_sig", src_sig)
            con.commit()
            log.info("bpre: reconstructed %d processes for %s", count, root_path)
            return count
        finally:
            con.close()
    finally:
        fcntl.flock(_lf.fileno(), fcntl.LOCK_UN)
        _lf.close()
