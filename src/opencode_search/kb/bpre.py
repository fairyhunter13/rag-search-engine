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
    "testdata", "mocks", "__tests__", "test_helpers", "fixtures",
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
CREATE TABLE IF NOT EXISTS process_rules (
    process_id TEXT NOT NULL, step_order INTEGER NOT NULL,
    rule_text TEXT NOT NULL, source_sid TEXT DEFAULT '',
    PRIMARY KEY (process_id, step_order)
);
CREATE TABLE IF NOT EXISTS state_machines (
    id TEXT PRIMARY KEY, entity TEXT NOT NULL,
    states_json TEXT DEFAULT '[]', transitions_json TEXT DEFAULT '[]',
    source TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS process_artifacts (
    process_id TEXT PRIMARY KEY, narrative TEXT DEFAULT '',
    mermaid TEXT DEFAULT '', bpmn_xml TEXT DEFAULT ''
);
"""

# ─── Tiny helpers ─────────────────────────────────────────────────────────────

def _init_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path), check_same_thread=False)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript(_SCHEMA)
    con.commit()
    return con


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


def _source_files(member_path: str) -> list[Path]:
    exts = {".go", ".java", ".py", ".ts", ".js", ".kt"}
    root = Path(member_path)
    from opencode_search.core.config import IGNORED_DIRS
    out: list[Path] = []
    try:
        for dirpath, dirs, files in os.walk(str(root)):
            dirs[:] = [d for d in dirs if d not in IGNORED_DIRS]
            for f in files:
                if Path(f).suffix in exts:
                    out.append(Path(dirpath) / f)
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

def _build_service_registry(members: list[str]) -> dict[str, str]:
    """Map {ServiceLabel → member_path} by mining registrar names from generated *.pb.go."""
    from opencode_search.kb.bpre_ast import federation_discover
    registry: dict[str, str] = {}
    for member in members:
        for _fn, svc in federation_discover([member]).registrars.items():
            registry.setdefault(svc, member)
    return registry


def _build_pubsub_registry(
    members: list[str], surf: ApiSurface,
) -> dict[str, tuple[str, str]]:
    from opencode_search.index.discover import detect_language
    from opencode_search.kb.bpre_ast import scan_file
    publishers: dict[str, list[str]] = {}
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
            for tk, _ln in ff.proto_marshal_types:
                publishers.setdefault(tk, []).append(member)
            for tk, _ln in ff.pubsub_consumes:
                consumers.setdefault(tk, []).append(member)
    result: dict[str, tuple[str, str]] = {}
    for key in set(publishers) & set(consumers):
        pub, sub = publishers[key][0], consumers[key][0]
        if pub != sub:
            result[key] = (pub, sub)
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
            edge_id = _hid(caller_svc, callee_svc, "grpc", svc_name)
            rows.append((edge_id, caller_svc, rel, ln, callee_svc,
                         f"{svc_name}Service", "grpc", 1.0, f"{ctor} @ {rel}:{ln}"))
    con.executemany("INSERT OR IGNORE INTO cross_service_edges VALUES (?,?,?,?,?,?,?,?,?)", rows)
    con.commit()


def _resolve_pubsub_edges(
    con: sqlite3.Connection, pubsub_registry: dict[str, tuple[str, str]],
) -> None:
    rows = [
        (_hid(_service_label(pub), _service_label(sub), "pubsub", msg),
         _service_label(pub), "", 0, _service_label(sub), msg, "pubsub", 1.0,
         f"proto type {msg}")
        for msg, (pub, sub) in pubsub_registry.items()
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


# ─── D4 Process tracing ───────────────────────────────────────────────────────

def _trace_processes(con: sqlite3.Connection, members: list[str]) -> int:
    eps = con.execute(
        "SELECT ep_id, service, file, line, kind, trigger FROM entry_points WHERE kind IN ('http','grpc')"
    ).fetchall()
    edges_raw = con.execute(
        "SELECT caller_service, callee_service, callee_endpoint, kind FROM cross_service_edges"
    ).fetchall()
    adj: dict[str, list[tuple[str, str, str]]] = {}
    for row in edges_raw:
        adj.setdefault(row[0], []).append((row[1], row[2], row[3]))
    count = 0
    seen_step_seqs: set[tuple] = set()
    for ep_id, entry_svc, ep_file, ep_line, ep_kind, trigger in eps:
        if entry_svc not in adj:
            continue
        visited: set[str] = {entry_svc}
        steps: list[tuple] = [(ep_id, 0, f"{ep_file}:{ep_line}", entry_svc, "entry", "")]
        order = 1
        queue: list[tuple[str, int]] = [(entry_svc, 0)]
        while queue:
            svc, depth = queue.pop(0)
            if depth >= 6:
                continue
            for callee_svc, endpoint, kind in adj.get(svc, []):
                if callee_svc in visited:
                    continue
                visited.add(callee_svc)
                steps.append((ep_id, order, endpoint, callee_svc, kind, ""))
                order += 1
                queue.append((callee_svc, depth + 1))
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


# ─── D5 Rule / state-machine extraction ──────────────────────────────────────

def _extract_rules(con: sqlite3.Connection, members: list[str]) -> None:
    if not _bpre_llm_on():
        return
    try:
        from opencode_search.core.config import project_graph_db
        from opencode_search.graph.llm import deepseek_chat, deepseek_key
        if not deepseek_key():
            return
    except Exception:
        return
    for member in members:
        gdb = project_graph_db(member)
        if not gdb.exists():
            continue
        import sqlite3 as _sq
        mcon = _sq.connect(str(gdb))
        try:
            rows = mcon.execute(
                "SELECT id, title, summary FROM communities "
                "WHERE semantic_type='business_rule' AND summary IS NOT NULL AND summary!='' LIMIT 30"
            ).fetchall()
        finally:
            mcon.close()
        for cid, title, summary in rows:
            try:
                rule_text = deepseek_chat(
                    f"Extract the business rule in one concise sentence.\n"
                    f"Title: {title}\nSummary: {summary[:600]}\n\nRule:",
                    max_tokens=120,
                ).strip()
            except Exception:
                rule_text = summary[:120]
            con.execute("INSERT OR IGNORE INTO process_rules VALUES ('',0,?,?)",
                        (rule_text, _hid(member, str(cid))))
    con.commit()


def _extract_state_machines(
    con: sqlite3.Connection, members: list[str], surf: ApiSurface,
) -> None:
    if not _bpre_llm_on():
        return
    try:
        from opencode_search.graph.llm import deepseek_chat, deepseek_key
        if not deepseek_key():
            return
    except Exception:
        return
    from opencode_search.index.discover import detect_language
    from opencode_search.kb.bpre_ast import scan_file
    for member in members:
        for src in _source_files(member):
            try:
                content = src.read_text(errors="replace")
            except OSError:
                continue
            ff = scan_file(str(src), content, detect_language(src), surf)
            if not ff or len(ff.status_enums) < 3:
                continue
            statuses = list(dict.fromkeys(ff.status_enums))
            rel = _rel_path(str(src), member)
            entity = Path(src).stem
            sm_id = _hid(member, rel, entity)
            if con.execute("SELECT 1 FROM state_machines WHERE id=?", (sm_id,)).fetchone():
                continue
            prompt = (
                f"From the status values, infer the state machine for entity '{entity}'.\n"
                f"Return JSON: {{\"states\":[...],\"transitions\":[{{\"from\":\"A\",\"to\":\"B\",\"event\":\"X\"}}]}}\n\n"
                f"Statuses: {', '.join(statuses)[:400]}"
            )
            try:
                raw = deepseek_chat(prompt, max_tokens=400)
                raw = raw.replace("```json", "").replace("```", "").strip().rstrip("`").strip()
                data = json.loads(raw)
                states = json.dumps(data.get("states", []))
                transitions = json.dumps(data.get("transitions", []))
            except Exception:
                states = json.dumps(statuses)
                transitions = "[]"
            con.execute("INSERT OR IGNORE INTO state_machines VALUES (?,?,?,?,?)",
                        (sm_id, entity, states, transitions, rel))
    con.commit()


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


def _synthesize_artifacts(con: sqlite3.Connection) -> None:
    procs = con.execute(
        "SELECT id, name, entry_service, services_json FROM processes"
    ).fetchall()
    try:
        from opencode_search.graph.llm import deepseek_chat, deepseek_key
        has_llm = _bpre_llm_on() and bool(deepseek_key())
    except Exception:
        has_llm = False
    for proc_id, name, _entry_svc, services_json in procs:
        steps_raw = con.execute(
            "SELECT sid_or_endpoint, service, kind, guard FROM process_steps "
            "WHERE process_id=? ORDER BY order_index", (proc_id,),
        ).fetchall()
        steps = [(r[0], r[1], r[2], r[3]) for r in steps_raw]
        bpmn = _bpmn_xml(proc_id, name, steps)
        mermaid = _mermaid_sequence(name, steps)
        narrative = ""
        if has_llm:
            try:
                services_list = ", ".join(json.loads(services_json))
                step_summary = "; ".join(f"{s[1]}:{s[0]}" for s in steps[:12])
                narrative = deepseek_chat(
                    f"Describe this business process in 3-5 sentences using ONLY the facts below.\n"
                    f"Process: {name}\nServices: {services_list}\nSteps: {step_summary}\n\n"
                    f"Do not invent service names or steps. Name real services.",
                    max_tokens=300,
                ).strip()
            except Exception:
                narrative = ""
        con.execute("INSERT OR REPLACE INTO process_artifacts VALUES (?,?,?,?)",
                    (proc_id, narrative, mermaid, bpmn))
    con.commit()


# ─── Master entry point ───────────────────────────────────────────────────────

def reconstruct_processes(root_path: str) -> int:
    """D2→D6 federation-level BPRE pass.  Returns number of reconstructed processes."""
    from opencode_search.core.config import root_process_db
    from opencode_search.daemon.federation import expand_federation
    members = expand_federation(root_path)
    if len(members) < 2:
        log.debug("bpre: skip %s — fewer than 2 federation members", root_path)
        return 0
    db_path = root_process_db(root_path)
    con = _init_db(db_path)
    try:
        for tbl in ("entry_points", "cross_service_edges", "processes",
                    "process_steps", "process_rules", "process_artifacts"):
            con.execute(f"DELETE FROM {tbl}")
        con.commit()
        from opencode_search.kb.bpre_ast import federation_discover
        surf = federation_discover(members)
        for member in members:
            _detect_entry_points(con, member, surf)
        svc_registry = _build_service_registry(members)
        pubsub_registry = _build_pubsub_registry(members, surf)
        http_routes = _build_http_route_map(members, surf)
        for member in members:
            _resolve_grpc_edges(con, member, svc_registry, surf)
        _resolve_pubsub_edges(con, pubsub_registry)
        for member in members:
            _resolve_http_edges(con, member, http_routes, surf)
        count = _trace_processes(con, members)
        if count == 0:
            log.info("bpre: no multi-service processes found for %s", root_path)
            return 0
        _extract_rules(con, members)
        _extract_state_machines(con, members, surf)
        _synthesize_artifacts(con)
        log.info("bpre: reconstructed %d processes for %s", count, root_path)
        return count
    finally:
        con.close()
