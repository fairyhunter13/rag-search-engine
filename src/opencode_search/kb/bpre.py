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
import re
import sqlite3
from pathlib import Path

log = logging.getLogger(__name__)

# ─── Patterns ─────────────────────────────────────────────────────────────────
_GRPC_CLIENT = re.compile(r"(\w+)\.New(\w+)Client\s*\(")
_GRPC_SERVER = re.compile(r"\bRegister(\w+)Server\s*\(")
_PROTO_IMPORT = re.compile(r'^\s+(\w+)\s+"([^"]*astro-proto[^"]*)"', re.MULTILINE)
_GO_IMPORT_BLOCK = re.compile(r"import\s*\(([^)]+)\)", re.DOTALL)
# Pub/Sub publisher: detect any publish call shape (no inline-type assumption)
_PUBSUB_PUBLISH_CALL = re.compile(
    r"\.(?:Publish|PublishWithSchema|PublishMessageV2|BatchPublish\w*)\s*\("
)
# Pub/Sub proto type references on publisher side
_PROTO_MARSHAL = re.compile(r"proto\.Marshal\s*\(\s*[&*]?(\w+)\.(\w+)\b")
_PROTO_LITERAL = re.compile(r"[&*]?(\w+)\.(\w+)\{")
_PROTO_UNMARSHAL = re.compile(r"proto\.Unmarshal\s*\([^,]+,\s*[&*]?(\w+)\.(\w+)\b")
_PUBSUB_RECEIVE = re.compile(r"\.Receive\s*\(\s*\w+\s*,\s*func\b")
_HTTP_ROUTE_GO = re.compile(r"\.(GET|POST|PUT|DELETE|PATCH)\s*\(\s*\"(/[^\"]+)\"")
_SPRING_MAPPING = re.compile(
    r'@(?:Get|Post|Put|Delete|Patch|Request)Mapping\s*\(?(?:value\s*=\s*)?\"([^\"]+)\"'
)
_HTTP_CLIENT_GO = re.compile(
    r'(?:http|client)\.(Get|Post|Put|Delete|Patch)\s*\(\s*(?:[^,]+\+\s*)?\"([^\"]+)\"'
)
_STATUS_ENUM = re.compile(r"Status\s*=\s*[\"'](\w+)[\"']|\.(\w+Status)\s*=")

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

def _detect_entry_points(con: sqlite3.Connection, member_path: str) -> None:
    service = _service_label(member_path)
    rows: list[tuple] = []
    for src in _source_files(member_path):
        if _is_test_file(str(src)):
            continue
        try:
            text = src.read_text(errors="replace")
        except OSError:
            continue
        rel = _rel_path(str(src), member_path)
        for m in _HTTP_ROUTE_GO.finditer(text):
            verb, path = m.group(1), m.group(2)
            ep_id = _hid(service, rel, verb, path)
            rows.append((ep_id, service, rel, text[:m.start()].count("\n") + 1, "http", f"{verb} {path}"))
        for m in _GRPC_SERVER.finditer(text):
            ep_id = _hid(service, rel, "grpc", m.group(1))
            rows.append((ep_id, service, rel, text[:m.start()].count("\n") + 1, "grpc", m.group(1)))
        for m in _SPRING_MAPPING.finditer(text):
            ep_id = _hid(service, rel, "spring", m.group(1))
            rows.append((ep_id, service, rel, text[:m.start()].count("\n") + 1, "http", m.group(1)))
        for m in _PUBSUB_RECEIVE.finditer(text):
            ep_id = _hid(service, rel, "pubsub", str(m.start()))
            rows.append((ep_id, service, rel, text[:m.start()].count("\n") + 1, "pubsub", "subscriber"))
    con.executemany("INSERT OR IGNORE INTO entry_points VALUES (?,?,?,?,?,?)", rows)
    con.commit()


# ─── D3 Proto helpers ─────────────────────────────────────────────────────────

def _parse_proto_aliases(src: str) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for block in _GO_IMPORT_BLOCK.finditer(src):
        for m in _PROTO_IMPORT.finditer(block.group(1)):
            aliases[m.group(1)] = m.group(2)
    return aliases


def _build_service_registry(members: list[str]) -> dict[str, str]:
    """Map {ServiceName → member_path} from RegisterXxxServer CALL sites (not proto definitions)."""
    registry: dict[str, str] = {}
    for member in members:
        for src in _source_files(member):
            # Skip protobuf-generated files — they define RegisterXxxServer, not call it
            if src.name.endswith((".pb.go", "_grpc.pb.go")):
                continue
            try:
                text = src.read_text(errors="replace")
            except OSError:
                continue
            # Only look for calls (alias.RegisterXxxServer or dot-qualified forms)
            for m in re.finditer(r"\bRegister(\w+)Server\s*\(\s*\w+\s*,", text):
                svc_name = m.group(1)
                # Avoid overwriting with a later member if already found elsewhere
                if svc_name not in registry:
                    registry[svc_name] = member
    return registry


def _build_pubsub_registry(members: list[str]) -> dict[str, tuple[str, str]]:
    publishers: dict[str, list[str]] = {}
    consumers: dict[str, list[str]] = {}
    for member in members:
        for src in _source_files(member):
            try:
                text = src.read_text(errors="replace")
            except OSError:
                continue
            aliases = _parse_proto_aliases(text)
            if not aliases:
                continue
            # Publisher: file has a Publish call AND imports an astro-proto alias.
            # Collect proto types via proto.Marshal() and alias-gated struct literals.
            if _PUBSUB_PUBLISH_CALL.search(text):
                proto_types: set[str] = set()
                for m in _PROTO_MARSHAL.finditer(text):
                    alias, msg = m.group(1), m.group(2)
                    if alias in aliases:
                        proto_types.add(f"{alias}.{msg}")
                for m in _PROTO_LITERAL.finditer(text):
                    alias, msg = m.group(1), m.group(2)
                    if alias in aliases:
                        proto_types.add(f"{alias}.{msg}")
                for key in proto_types:
                    publishers.setdefault(key, []).append(member)
            # Consumer detection (unchanged — correct: proto.Unmarshal inside sub.Receive)
            if _PUBSUB_RECEIVE.search(text):
                for m in _PROTO_UNMARSHAL.finditer(text):
                    alias, msg = m.group(1), m.group(2)
                    if alias in aliases:
                        consumers.setdefault(f"{alias}.{msg}", []).append(member)
    result: dict[str, tuple[str, str]] = {}
    for key in set(publishers) & set(consumers):
        pub, sub = publishers[key][0], consumers[key][0]
        if pub != sub:
            result[key] = (pub, sub)
    return result


def _build_http_route_map(members: list[str]) -> dict[str, str]:
    route_map: dict[str, str] = {}
    for member in members:
        svc = _service_label(member)
        for src in _source_files(member):
            try:
                text = src.read_text(errors="replace")
            except OSError:
                continue
            for m in _HTTP_ROUTE_GO.finditer(text):
                route_map[f"{m.group(1)} {m.group(2)}"] = svc
            for m in _SPRING_MAPPING.finditer(text):
                route_map[f"GET {m.group(1)}"] = svc
    return route_map


# ─── D3 Edge resolution ───────────────────────────────────────────────────────

def _resolve_grpc_edges(
    con: sqlite3.Connection, member: str, service_registry: dict[str, str],
) -> None:
    caller_svc = _service_label(member)
    rows: list[tuple] = []
    for src in _source_files(member):
        try:
            text = src.read_text(errors="replace")
        except OSError:
            continue
        aliases = _parse_proto_aliases(text)
        rel = _rel_path(str(src), member)
        for m in _GRPC_CLIENT.finditer(text):
            alias, svc_name = m.group(1), m.group(2)
            if alias not in aliases:
                continue
            callee_member = service_registry.get(svc_name)
            if not callee_member or callee_member == member:
                continue
            callee_svc = _service_label(callee_member)
            edge_id = _hid(caller_svc, callee_svc, "grpc", svc_name)
            line = text[:m.start()].count("\n") + 1
            rows.append((edge_id, caller_svc, rel, line, callee_svc,
                         f"{svc_name}Service", "grpc", 1.0,
                         f"New{svc_name}Client @ {rel}:{line}"))
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
) -> None:
    caller_svc = _service_label(member)
    rows: list[tuple] = []
    for src in _source_files(member):
        try:
            text = src.read_text(errors="replace")
        except OSError:
            continue
        rel = _rel_path(str(src), member)
        for m in _HTTP_CLIENT_GO.finditer(text):
            verb, path = m.group(1).upper(), m.group(2)
            callee_svc = http_routes.get(f"{verb} {path}")
            if not callee_svc or callee_svc == caller_svc:
                continue
            edge_id = _hid(caller_svc, callee_svc, "http", verb, path)
            line = text[:m.start()].count("\n") + 1
            rows.append((edge_id, caller_svc, rel, line, callee_svc,
                         f"{verb} {path}", "http", 0.8, f"HTTP @ {rel}:{line}"))
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


def _extract_state_machines(con: sqlite3.Connection, members: list[str]) -> None:
    if not _bpre_llm_on():
        return
    try:
        from opencode_search.graph.llm import deepseek_chat, deepseek_key
        if not deepseek_key():
            return
    except Exception:
        return
    for member in members:
        for src in _source_files(member):
            try:
                text = src.read_text(errors="replace")
            except OSError:
                continue
            statuses = list(dict.fromkeys(
                m.group(1) or m.group(2)
                for m in _STATUS_ENUM.finditer(text)
                if m.group(1) or m.group(2)
            ))
            if len(statuses) < 3:
                continue
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
                raw = re.sub(r"```[a-z]*\n?", "", raw).strip().rstrip("`").strip()
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
        for member in members:
            _detect_entry_points(con, member)
        svc_registry = _build_service_registry(members)
        pubsub_registry = _build_pubsub_registry(members)
        http_routes = _build_http_route_map(members)
        for member in members:
            _resolve_grpc_edges(con, member, svc_registry)
        _resolve_pubsub_edges(con, pubsub_registry)
        for member in members:
            _resolve_http_edges(con, member, http_routes)
        count = _trace_processes(con, members)
        if count == 0:
            log.info("bpre: no multi-service processes found for %s", root_path)
            return 0
        _extract_rules(con, members)
        _extract_state_machines(con, members)
        _synthesize_artifacts(con)
        log.info("bpre: reconstructed %d processes for %s", count, root_path)
        return count
    finally:
        con.close()
