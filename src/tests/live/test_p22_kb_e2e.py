"""P22: KB e2e behavior + federation/symlink-repo invariants (S5-S6)."""
from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path

import pytest
import requests

pytestmark = pytest.mark.live

_BASE = "http://127.0.0.1:8765"
_HDR = {"Content-Type": "application/json"}

from tests.live._projects import federation_root as _federation_root
from tests.live._projects import standalone_project as _standalone_project

# Derive OSE root from this file's location; resolve other projects by capability.
_PROJECTS = {
    "ose": str(Path(__file__).resolve().parents[3]),
    "federation": _federation_root(),
    "standalone": _standalone_project(),
}


def _overview(what: str, project: str, timeout: int = 20) -> dict:
    r = requests.post(f"{_BASE}/api/overview",
                      json={"what": what, "project_path": project},
                      headers=_HDR, timeout=timeout)
    assert r.status_code == 200, f"overview({what}, {project}): HTTP {r.status_code}"
    return json.loads(r.text)


def _graph_db(project: str) -> Path:
    from opencode_search.core.config import project_graph_db
    return project_graph_db(project)


def _converge_ready(project: str, timeout: int = 240) -> None:
    """Index (if needed) + enrich all members, poll until kb_state==ready, l2==100."""
    import time

    from opencode_search.daemon.sweeps import _enrich_project, _index_project
    _enrich_project(project)
    s = _overview("status", project)
    for m in s.get("members", []):
        ks = m.get("kb_state")
        if ks == "indexing":
            _index_project(m["path"])  # never-indexed members need full index first
        if ks != "ready":
            _enrich_project(m["path"])
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        s = _overview("status", project)
        if (s.get("kb_state") == "ready"
                and s.get("l1_enriched_pct") == 100.0):
            return
        time.sleep(3)
    s = _overview("status", project)
    assert (s.get("kb_state") == "ready"
            and s.get("l1_enriched_pct") == 100.0), (
        f"{project!r} did not reach ready in {timeout}s — "
        f"kb_state={s.get('kb_state')!r}, "
        f"l1={s.get('l1_enriched_pct')}"
    )


# ---------------------------------------------------------------------------
# S5: E2E MCP round-trip for 3 named projects
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("proj_key", ["ose", "federation", "standalone"])
def test_e2e_status_has_required_fields(live_client, proj_key):
    """S5a: overview(status) returns required fields for each named project."""
    project = _PROJECTS[proj_key]
    status = _overview("status", project)
    for field in ("kb_state", "enriched_pct", "l1_enriched_pct",
                  "symbols", "communities"):
        assert field in status, f"overview(status) missing {field!r} for {proj_key}"
    assert status["kb_state"] in ("indexing", "searchable", "enriching", "ready"), (
        f"unexpected kb_state={status['kb_state']!r} for {proj_key}"
    )


@pytest.mark.parametrize("proj_key", ["ose", "federation", "standalone"])
def test_e2e_no_l1_placeholders(live_client, proj_key):
    """S5b: L1 communities must not contain 'Domain N' placeholder titles."""
    project = _PROJECTS[proj_key]
    data = _overview("communities", project)
    communities = data.get("communities", [])
    placeholder = re.compile(r"^Domain\s+\d+$", re.IGNORECASE)
    bad = [c.get("title", "") for c in communities if c.get("level") == 1
           and placeholder.match(c.get("title", "") or "")]
    assert not bad, f"{proj_key}: placeholder community titles found: {bad}"


@pytest.mark.parametrize("proj_key", ["ose", "federation", "standalone"])
def test_e2e_ask_global_non_empty(live_client, proj_key):
    """S5c: MCP ask scope=global returns a non-empty assembled context."""
    import asyncio

    from opencode_search.server.mcp import ask as _mcp_ask
    project = _PROJECTS[proj_key]
    ctx = asyncio.run(_mcp_ask("What is the overall architecture?", project, "global"))
    assert len(ctx.strip()) > 20, f"ask(global, {proj_key}): context too short: {ctx[:80]}"


# ---------------------------------------------------------------------------
# S6: Federation / symlink-repo invariants
# ---------------------------------------------------------------------------

def _external_symlink_targets(root: Path) -> set[str]:
    """Resolved targets of symlinked dirs under root pointing outside it (mirrors iter_files federation prune)."""
    from opencode_search.core.config import IGNORED_DIRS
    root = root.resolve()
    out: set[str] = set()
    for dp, dirs, _ in os.walk(root):
        dirs[:] = [d for d in dirs if d not in IGNORED_DIRS]
        for d in dirs:
            p = Path(dp) / d
            if p.is_symlink() and not p.resolve().is_relative_to(root):
                out.add(str(p.resolve()))
    return out


def _symlinked_project() -> str | None:
    r = requests.post(f"{_BASE}/api/overview", json={"what": "projects"}, timeout=10)
    if r.status_code != 200:
        return None
    for p in json.loads(r.text).get("projects", []):
        path = Path(p["path"])
        if path.exists() and _external_symlink_targets(path):
            return p["path"]
    return None


def test_federation_indexing_prunes_symlink_targets(live_client):
    """S6a: Indexing a root with symlinked sub-repos must not wildly inflate file_count."""
    root = _symlinked_project()
    assert root is not None, "no indexed project with external symlinked sub-dirs — register a project that has symlinked sub-repos"
    root_path = Path(root)
    own_files = sum(
        1 for f in root_path.rglob("*")
        if f.is_file() and not any(p.is_symlink() for p in f.parents)
    )
    status = _overview("status", root)
    reported = status.get("file_count", 0)
    if own_files > 0:
        assert reported <= own_files * 2, (
            f"file_count {reported} > 2× own_files {own_files} "
            f"— symlinked targets may be inlined"
        )


def test_federation_kb_reflects_root_only(live_client):
    """S6b: graph.db symbols must not include files from symlinked member paths."""
    root = _symlinked_project()
    assert root is not None, "no indexed project with external symlinked sub-dirs — register a project that has symlinked sub-repos"
    gdb = _graph_db(root)
    assert gdb.exists(), f"graph.db not found for symlinked root {root}"
    symlinked = _external_symlink_targets(Path(root))
    con = sqlite3.connect(str(gdb))
    try:
        outsiders = [
            r[0] for r in con.execute("SELECT DISTINCT file FROM symbols").fetchall()
            if r[0] and not r[0].startswith(root)
            and any(r[0].startswith(sd) for sd in symlinked)
        ]
    finally:
        con.close()
    assert not outsiders, (
        f"graph.db for {root!r} contains {len(outsiders)} symbol files "
        f"from symlinked member paths: {outsiders[:3]}"
    )



# ---------------------------------------------------------------------------
# T1/HR7: all 3 projects must reach kb_state='ready'
# ---------------------------------------------------------------------------

@pytest.mark.slow
@pytest.mark.parametrize("proj_key", ["ose", "standalone", "federation"])
def test_kb_state_ready_all_projects(live_client, proj_key):
    """T1/TC1/HR7: converge+assert kb_state='ready' and l1_enriched_pct==100 for all 3 projects."""
    _converge_ready(_PROJECTS[proj_key])


# ---------------------------------------------------------------------------
# T3/HR3: overview(status) must return identical counts between reads (no churn)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("proj_key", ["ose", "payment"])
def test_kb_state_no_churn(live_client, proj_key):
    """T3/HR3: symbols/communities/enriched_pct must be stable between back-to-back reads."""
    import time
    s1 = _overview("status", _PROJECTS[proj_key])
    time.sleep(2)
    s2 = _overview("status", _PROJECTS[proj_key])
    for field in ("symbols", "communities", "enriched_pct"):
        assert s1.get(field) == s2.get(field), (
            f"{proj_key}: {field} changed between reads: "
            f"{s1.get(field)} → {s2.get(field)} (churn detected, HR3 violation)"
        )


# ---------------------------------------------------------------------------
# T6/HR4: federation fan-out on the REAL federation root
# ---------------------------------------------------------------------------

# Fix #1: _needs_enrich detects stalled L2 summaries

def test_needs_enrich_detects_null_summaries(safe_tmp_path):
    """Fix #1: _needs_enrich returns True when any community has a NULL summary."""
    import sqlite3

    from opencode_search.core.config import project_graph_db
    from opencode_search.core.registry import ProjectEntry, remove_project, upsert_project
    from opencode_search.daemon.sweeps import _needs_enrich

    upsert_project(ProjectEntry(path=str(safe_tmp_path), enabled=True))
    try:
        gdb = project_graph_db(str(safe_tmp_path))
        gdb.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(gdb))
        con.execute("CREATE TABLE communities (id INTEGER PRIMARY KEY, level INTEGER, title TEXT, summary TEXT)")
        con.execute("INSERT INTO communities VALUES (1, 1, 'A', NULL)")
        con.commit()
        con.close()
        assert _needs_enrich(str(safe_tmp_path)) is True
        con = sqlite3.connect(str(gdb))
        con.execute("UPDATE communities SET summary='done'")
        con.commit()
        con.close()
        assert _needs_enrich(str(safe_tmp_path)) is False
    finally:
        remove_project(str(safe_tmp_path))


def test_real_federation_fanout(live_client):
    """T6/HR4: federation root members list has ≥1 member with real symbols; search fans out."""
    status = _overview("status", _PROJECTS["federation"])
    members = status.get("members", [])
    assert members, "federation root overview(status) returned no members (federation broken)"
    with_syms = [m for m in members if m.get("symbols", 0) > 0]
    assert with_syms, (
        f"no federation members have symbols > 0 (fan-out not working); "
        f"sample: {[(m['path'].split('/')[-1], m['symbols']) for m in members[:3]]}"
    )
    import asyncio

    from opencode_search.server.mcp import search as _mcp_search
    data = json.loads(asyncio.run(_mcp_search("function", project_paths=[_PROJECTS["federation"]])))
    results = data.get("results", [])
    assert results, "search(project_paths=[federation root]) returned no results (fan-out broken)"


# Fix #2a: _needs_index keys on indexed_at, not stray chunks

def test_needs_index_keys_on_indexed_at(safe_tmp_path):
    """Fix #2a: _needs_index returns True for indexed_at=None even with stray chunks present."""
    import sqlite3

    from opencode_search.core.config import project_vector_db
    from opencode_search.core.registry import ProjectEntry, remove_project, upsert_project
    from opencode_search.daemon.sweeps import _needs_index

    upsert_project(ProjectEntry(path=str(safe_tmp_path), enabled=True, indexed_at=None))
    try:
        vdb = project_vector_db(str(safe_tmp_path))
        vdb.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(vdb))
        con.execute("CREATE TABLE chunks (rowid INTEGER, path TEXT, content TEXT)")
        con.execute("INSERT INTO chunks VALUES (1,'a.py','stray')")
        con.commit()
        con.close()
        assert _needs_index(str(safe_tmp_path)) is True, (
            "_needs_index must be True for indexed_at=None regardless of stray chunks"
        )
    finally:
        remove_project(str(safe_tmp_path))


# Fix #2c: _index_project is idempotent (vec0 UNIQUE regression guard)

@pytest.mark.slow
def test_index_project_idempotent(safe_tmp_path):
    """Fix #2c: calling _index_project twice must not raise UNIQUE constraint on vec_chunks."""
    from opencode_search.core.config import ProjectEntry
    from opencode_search.core.registry import get_project, remove_project, upsert_project
    from opencode_search.daemon.sweeps import _index_project

    (safe_tmp_path / "a.py").write_text("def hello(): return 1\n")
    upsert_project(ProjectEntry(path=str(safe_tmp_path), enabled=True))
    try:
        _index_project(str(safe_tmp_path))  # first run
        _index_project(str(safe_tmp_path))  # second run — must not raise UNIQUE constraint
        entry = get_project(str(safe_tmp_path))
        assert entry is not None and entry.indexed_at is not None, (
            "indexed_at must be set after idempotent _index_project"
        )
    finally:
        remove_project(str(safe_tmp_path))


# Fix #2b: overview(status) shows "indexing" for never-indexed member

def test_overview_status_shows_indexing_for_never_indexed(safe_tmp_path):
    """Fix #2b: overview(status) kb_state must be 'indexing' when indexed_at is None."""
    from opencode_search.core.config import project_graph_db, project_vector_db
    from opencode_search.core.registry import ProjectEntry, remove_project, upsert_project
    from opencode_search.graph.store import GraphStore
    from opencode_search.server._overview import handle_overview

    upsert_project(ProjectEntry(path=str(safe_tmp_path), enabled=True, indexed_at=None))
    try:
        # Create a vectors.db with stray chunks (reproduces astro-loyalty-be state)
        vdb = project_vector_db(str(safe_tmp_path))
        vdb.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(vdb))
        con.execute("CREATE TABLE chunks (rowid INTEGER, path TEXT, content TEXT)")
        con.execute("INSERT INTO chunks VALUES (1,'a.py','stray')")
        con.commit()
        con.close()
        # Create a minimal valid graph.db so handle_overview's _paths filter passes
        gdb = project_graph_db(str(safe_tmp_path))
        gs = GraphStore(gdb)
        gs.close()  # creates the schema; graph.db now exists
        result = json.loads(handle_overview(str(safe_tmp_path), "status"))
        members_ks = [m.get("kb_state") for m in result.get("members", [])
                      if m.get("path") == str(safe_tmp_path)]
        reported = members_ks[0] if members_ks else result.get("kb_state")
        assert reported == "indexing", (
            f"never-indexed member must report 'indexing', got {reported!r}"
        )
    finally:
        remove_project(str(safe_tmp_path))


# ---------------------------------------------------------------------------
# Logical-entity e2e: all 3 real federations must have all members indexed
# ---------------------------------------------------------------------------

@pytest.mark.slow
@pytest.mark.parametrize("proj_key", ["ose", "federation", "standalone"])
def test_federation_no_member_stuck_indexing(live_client, proj_key):
    """Fix #2: after reconcile completes, no federation member may have kb_state='indexing'.

    Marked slow: reconcile must finish indexing all never-indexed members before this passes.
    Run after `reconcile_projects()` has had time to complete for all members.
    """
    from opencode_search.daemon.sweeps import reconcile_projects
    reconcile_projects()  # ensure local state is converged before asserting
    status = _overview("status", _PROJECTS[proj_key])
    stuck = [m for m in status.get("members", []) if m.get("kb_state") == "indexing"]
    assert not stuck, (
        f"{proj_key}: {len(stuck)} member(s) still 'indexing' after reconcile — "
        f"{[m['path'].split('/')[-1] for m in stuck[:3]]}"
    )


def test_upsert_project_rejects_forbidden_root():
    """Fix D1: upsert_project must raise ValueError for /tmp and ~/.cache paths."""
    import pytest

    from opencode_search.core.config import ProjectEntry
    from opencode_search.core.registry import upsert_project

    with pytest.raises(ValueError, match="forbidden"):
        upsert_project(ProjectEntry(path="/tmp/should-not-register", enabled=True))


@pytest.mark.slow
@pytest.mark.parametrize("proj_key", ["ose", "federation", "standalone"])
def test_federation_search_ask_as_logical_entity(live_client, proj_key):
    """Logical-entity e2e: search and ask both return results for real project roots."""
    import asyncio

    from opencode_search.server.mcp import ask as _mcp_ask
    from opencode_search.server.mcp import search as _mcp_search

    root = _PROJECTS[proj_key]
    sr = json.loads(asyncio.run(_mcp_search("function", project_paths=[root])))
    assert sr.get("results"), f"{proj_key}: search returned no results (fan-out broken)"
    ak = asyncio.run(_mcp_ask("What is the overall architecture?", root, "global"))
    assert len(ak.strip()) > 20, f"{proj_key}: ask returned empty/short context"


# ---------------------------------------------------------------------------
# Logical-entity config (E1/E2/E3)
# ---------------------------------------------------------------------------

def test_overview_status_includes_config_key(live_client):
    """E3: overview(status) must include a 'config' key with source + exclude."""
    status = _overview("status", _PROJECTS["ose"])
    assert "config" in status, f"overview(status) missing 'config' key: {list(status)}"
    cfg = status["config"]
    assert "source" in cfg and "exclude" in cfg, f"config key missing fields: {cfg}"
    assert cfg["source"] in ("own", "inherited", "default"), f"unexpected config.source: {cfg['source']}"


def test_iter_files_always_yields_ose_config(safe_tmp_path):
    """E2: .opencode-index.yaml must be yielded even when excluded by an exclude glob."""
    from opencode_search.core.index_config import ProjectConfig
    from opencode_search.index.discover import iter_files

    (safe_tmp_path / ".opencode-index.yaml").write_text("index:\n  exclude: []\n")
    (safe_tmp_path / "normal.yaml").write_text("key: val\n")
    cfg = ProjectConfig(exclude=["*.yaml"])
    found = {p.name for p in iter_files(safe_tmp_path, cfg=cfg)}
    assert ".opencode-index.yaml" in found, (
        ".opencode-index.yaml must be yielded even when *.yaml is excluded"
    )
    assert "normal.yaml" not in found, "normal.yaml must be excluded by *.yaml glob"


def test_effective_config_inherits_root_excludes(safe_tmp_path):
    """E1: federation member effective_config includes root's exclude globs."""
    from opencode_search.core.config import ProjectEntry
    from opencode_search.core.index_config import effective_config
    from opencode_search.core.registry import remove_project, upsert_project

    root = safe_tmp_path / "root"
    member = safe_tmp_path / "member"
    root.mkdir()
    member.mkdir()
    (root / ".opencode-index.yaml").write_text("index:\n  exclude:\n    - '*.gen.py'\n")
    root_path, member_path = str(root), str(member)
    upsert_project(ProjectEntry(path=root_path, enabled=True, federation=[member_path]))
    upsert_project(ProjectEntry(path=member_path, enabled=True))
    try:
        cfg = effective_config(member_path)
        assert "*.gen.py" in cfg.exclude, f"member must inherit root's exclude; got {cfg.exclude}"
    finally:
        remove_project(root_path)
        remove_project(member_path)
