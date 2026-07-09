"""P22: KB e2e behavior + federation/symlink-repo invariants (S5-S6)."""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import pytest
import requests

from tests.live._sample_workspace import SampleWorkspace

pytestmark = pytest.mark.live

_BASE = "http://127.0.0.1:8765"
_HDR = {"Content-Type": "application/json"}
@pytest.fixture(scope="session")
def projects(sample_workspace: SampleWorkspace) -> dict[str, str]:
    return {
        "service": sample_workspace.promo,
        "federation": sample_workspace.fed_root,
        "standalone": sample_workspace.ledger,
    }


def _overview(what: str, project: str, timeout: int = 20) -> dict:
    r = requests.post(f"{_BASE}/api/overview",
                      json={"what": what, "project_path": project},
                      headers=_HDR, timeout=timeout)
    assert r.status_code == 200, f"overview({what}, {project}): HTTP {r.status_code}"
    return json.loads(r.text)


def _graph_db(project: str) -> Path:
    from rag_search.core.config import project_graph_db
    return project_graph_db(project)


def _converge_ready(project: str, timeout: int = 240) -> None:
    """Index (if needed) + enrich all members, poll until kb_state==ready, l1==100.

    Re-calls _enrich_project when l1_enriched_pct < 100: head communities can fail
    a DeepSeek batch (throttle/timeout), leaving them unenriched until the next call.
    Each retry picks up exactly the communities still missing a summary (idempotent).
    """
    import time

    from rag_search.daemon.sweeps import _enrich_project, _index_project
    _enrich_project(project)
    s = _overview("status", project)
    for m in s.get("members", []):
        ks = m.get("kb_state")
        if ks == "indexing":
            _index_project(m["path"])  # never-indexed members need full index first
        if ks != "ready":
            _enrich_project(m["path"])
    _last_retry = time.monotonic()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        s = _overview("status", project)
        if (s.get("kb_state") == "ready"
                and s.get("l1_enriched_pct") == 100.0):
            return
        # Re-enrich if head communities are still unenriched (DeepSeek batch may have failed).
        # Throttle retries to once per 30s to avoid hammering DeepSeek on transient errors.
        if s.get("l1_enriched_pct", 0) < 100.0 and time.monotonic() - _last_retry >= 30:
            _enrich_project(project)
            _last_retry = time.monotonic()
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

@pytest.mark.parametrize("proj_key", ["service", "federation", "standalone"])
def test_e2e_status_has_required_fields(live_client, proj_key, projects):
    """S5a: overview(status) returns required fields for each named project."""
    project = projects[proj_key]
    status = _overview("status", project)
    for field in ("kb_state", "enriched_pct", "l1_enriched_pct",
                  "symbols", "communities"):
        assert field in status, f"overview(status) missing {field!r} for {proj_key}"
    assert status["kb_state"] in ("indexing", "searchable", "enriching", "ready"), (
        f"unexpected kb_state={status['kb_state']!r} for {proj_key}"
    )


@pytest.mark.parametrize("proj_key", ["service", "federation", "standalone"])
def test_e2e_no_l1_placeholders(live_client, proj_key, projects):
    """S5b: L1 communities must not contain 'Domain N' placeholder titles."""
    project = projects[proj_key]
    data = _overview("communities", project)
    communities = data.get("communities", [])
    placeholder = re.compile(r"^Domain\s+\d+$", re.IGNORECASE)
    bad = [c.get("title", "") for c in communities if c.get("level") == 1
           and placeholder.match(c.get("title", "") or "")]
    assert not bad, f"{proj_key}: placeholder community titles found: {bad}"


@pytest.mark.parametrize("proj_key", ["service", "federation", "standalone"])
def test_e2e_ask_global_non_empty(live_client, proj_key, projects):
    """S5c: MCP ask scope=global returns a non-empty assembled context."""
    import asyncio

    from rag_search.server.mcp import ask as _mcp_ask
    project = projects[proj_key]
    ctx = asyncio.run(_mcp_ask("What is the overall architecture?", project, "global"))
    assert len(ctx.strip()) > 20, f"ask(global, {proj_key}): context too short: {ctx[:80]}"


# ---------------------------------------------------------------------------
# S6: Federation / symlink-repo invariants
# ---------------------------------------------------------------------------

_SAFE_BASE = Path.home() / ".local" / "share" / "rse-test-dirs"


@pytest.fixture(scope="module")
def synth_symlink_proj():
    """Synthetic root with one external symlinked sub-dir — used by S6a/S6b.

    Layout built under ~/.local/share/rse-test-dirs/:
      root/own.py          — own source file (indexed)
      root/ext-link/ → external/  — symlink to external dir (must be pruned)
      external/leaked.py   — file that must NOT appear in root's graph.db
    """
    import shutil
    import tempfile

    from rag_search.core.config import ProjectEntry
    from rag_search.core.registry import remove_project, upsert_project
    from rag_search.daemon.sweeps import _index_project

    _SAFE_BASE.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(dir=_SAFE_BASE, prefix="synth-symlink-root-"))
    external = Path(tempfile.mkdtemp(dir=_SAFE_BASE, prefix="synth-symlink-ext-"))
    try:
        (root / "own.py").write_text("def own_fn():\n    pass\n")
        (external / "leaked.py").write_text("def leaked_fn():\n    pass\n")
        link = root / "ext-link"
        link.symlink_to(external)
        upsert_project(ProjectEntry(path=str(root), enabled=True))
        _index_project(str(root))
        yield str(root), str(external)
        remove_project(str(root))
    finally:
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(external, ignore_errors=True)


def test_federation_indexing_prunes_symlink_targets(live_client, synth_symlink_proj):
    """S6a: indexing a root with a symlinked external sub-dir must not inflate file_count."""
    root, _external = synth_symlink_proj
    root_path = Path(root)
    own_files = sum(
        1 for f in root_path.iterdir() if f.is_file()
    )
    status = _overview("status", root)
    reported = status.get("file_count", 0)
    if own_files > 0:
        assert reported <= own_files * 2, (
            f"file_count {reported} > 2× own_files {own_files} "
            f"— symlinked external dir may have been inlined"
        )


def test_federation_kb_reflects_root_only(live_client, synth_symlink_proj):
    """S6b: graph.db symbols must not include files from the symlinked external dir."""
    root, external = synth_symlink_proj
    gdb = _graph_db(root)
    assert gdb.exists(), f"graph.db not found for synthetic symlink root {root}"
    con = sqlite3.connect(str(gdb))
    try:
        outsiders = [
            r[0] for r in con.execute("SELECT DISTINCT file FROM symbols").fetchall()
            if r[0] and r[0].startswith(external)
        ]
    finally:
        con.close()
    assert not outsiders, (
        f"graph.db for {root!r} contains {len(outsiders)} symbol files "
        f"from symlinked external dir {external!r}: {outsiders[:3]}"
    )



# ---------------------------------------------------------------------------
# T1/HR7: all 3 projects must reach kb_state='ready'
# ---------------------------------------------------------------------------

@pytest.mark.slow
@pytest.mark.parametrize("proj_key", ["service", "standalone", "federation"])
def test_kb_state_ready_all_projects(live_client, proj_key, projects):
    """T1/TC1/HR7: converge+assert kb_state='ready' and l1_enriched_pct==100 for all 3 projects."""
    # federation root aggregates all members — DeepSeek enrichment can exceed the default 240s
    timeout = 600 if proj_key == "federation" else 240
    _converge_ready(projects[proj_key], timeout=timeout)


# ---------------------------------------------------------------------------
# T3/HR3: overview(status) must return identical counts between reads (no churn)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("proj_key", ["service", "standalone"])
def test_kb_state_no_churn(live_client, proj_key, projects):
    """T3/HR3: symbols/communities/enriched_pct must be stable between back-to-back reads."""
    import time
    s1 = _overview("status", projects[proj_key])
    time.sleep(2)
    s2 = _overview("status", projects[proj_key])
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

    from rag_search.core.config import project_graph_db
    from rag_search.core.registry import ProjectEntry, remove_project, upsert_project
    from rag_search.daemon.sweeps import _needs_enrich

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


def test_real_federation_fanout(live_client, projects):
    """T6/HR4: federation root members list has ≥1 member with real symbols; search fans out."""
    status = _overview("status", projects["federation"])
    members = status.get("members", [])
    assert members, "federation root overview(status) returned no members (federation broken)"
    with_syms = [m for m in members if m.get("symbols", 0) > 0]
    assert with_syms, (
        f"no federation members have symbols > 0 (fan-out not working); "
        f"sample: {[(m['path'].split('/')[-1], m['symbols']) for m in members[:3]]}"
    )
    import asyncio

    from rag_search.server.mcp import search as _mcp_search
    data = json.loads(asyncio.run(_mcp_search("function", project_paths=[projects["federation"]])))
    results = data.get("results", [])
    assert results, "search(project_paths=[federation root]) returned no results (fan-out broken)"


# Fix #2a: _needs_index keys on indexed_at, not stray chunks

def test_needs_index_keys_on_indexed_at(safe_tmp_path):
    """Fix #2a: _needs_index returns True for indexed_at=None even with stray chunks present."""
    import sqlite3

    from rag_search.core.config import project_vector_db
    from rag_search.core.registry import ProjectEntry, remove_project, upsert_project
    from rag_search.daemon.sweeps import _needs_index

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
    from rag_search.core.config import ProjectEntry
    from rag_search.core.registry import get_project, remove_project, upsert_project
    from rag_search.daemon.sweeps import _index_project

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
    from rag_search.core.config import project_graph_db, project_vector_db
    from rag_search.core.registry import ProjectEntry, remove_project, upsert_project
    from rag_search.graph.store import GraphStore
    from rag_search.server._overview import handle_overview

    upsert_project(ProjectEntry(path=str(safe_tmp_path), enabled=True, indexed_at=None))
    try:
        # Create a vectors.db with stray chunks (reproduces a stale-index state)
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
@pytest.mark.parametrize("proj_key", ["service", "federation", "standalone"])
def test_federation_no_member_stuck_indexing(live_client, proj_key, projects):
    """Fix #2: after reconcile completes, no federation member may have kb_state='indexing'.

    Marked slow: reconcile must finish indexing all never-indexed members before this passes.
    Run after `reconcile_projects()` has had time to complete for all members.
    """
    from rag_search.daemon.sweeps import reconcile_projects
    reconcile_projects()  # ensure local state is converged before asserting
    status = _overview("status", projects[proj_key])
    stuck = [m for m in status.get("members", []) if m.get("kb_state") == "indexing"]
    assert not stuck, (
        f"{proj_key}: {len(stuck)} member(s) still 'indexing' after reconcile — "
        f"{[m['path'].split('/')[-1] for m in stuck[:3]]}"
    )


def test_upsert_project_rejects_forbidden_root():
    """Fix D1: upsert_project must raise ValueError for /tmp and ~/.cache paths."""
    import pytest

    from rag_search.core.config import ProjectEntry
    from rag_search.core.registry import upsert_project

    with pytest.raises(ValueError, match="forbidden"):
        upsert_project(ProjectEntry(path="/tmp/should-not-register", enabled=True))


@pytest.mark.slow
@pytest.mark.parametrize("proj_key", ["service", "federation", "standalone"])
def test_federation_search_ask_as_logical_entity(live_client, proj_key, projects):
    """Logical-entity e2e: search and ask both return results for real project roots."""
    import asyncio

    from rag_search.server.mcp import ask as _mcp_ask
    from rag_search.server.mcp import search as _mcp_search

    root = projects[proj_key]
    sr = json.loads(asyncio.run(_mcp_search("function", project_paths=[root])))
    assert sr.get("results"), f"{proj_key}: search returned no results (fan-out broken)"
    ak = asyncio.run(_mcp_ask("What is the overall architecture?", root, "global"))
    assert len(ak.strip()) > 20, f"{proj_key}: ask returned empty/short context"


# ---------------------------------------------------------------------------
# Logical-entity config (E1/E2/E3)
# ---------------------------------------------------------------------------

def test_overview_status_includes_config_key(live_client, projects):
    """E3: overview(status) must include a 'config' key with source + exclude."""
    status = _overview("status", projects["service"])
    assert "config" in status, f"overview(status) missing 'config' key: {list(status)}"
    cfg = status["config"]
    assert "source" in cfg and "exclude" in cfg, f"config key missing fields: {cfg}"
    assert cfg["source"] in ("own", "inherited", "default"), f"unexpected config.source: {cfg['source']}"


def test_iter_files_always_yields_rse_config(safe_tmp_path):
    """E2: .rse-index.yaml must be yielded even when excluded by an exclude glob."""
    from rag_search.core.index_config import ProjectConfig
    from rag_search.index.discover import iter_files

    (safe_tmp_path / ".rse-index.yaml").write_text("index:\n  exclude: []\n")
    (safe_tmp_path / "normal.yaml").write_text("key: val\n")
    cfg = ProjectConfig(exclude=["*.yaml"])
    found = {p.name for p in iter_files(safe_tmp_path, cfg=cfg)}
    assert ".rse-index.yaml" in found, (
        ".rse-index.yaml must be yielded even when *.yaml is excluded"
    )
    assert "normal.yaml" not in found, "normal.yaml must be excluded by *.yaml glob"


def test_effective_config_inherits_root_excludes(safe_tmp_path):
    """E1: federation member effective_config includes root's exclude globs."""
    from rag_search.core.config import ProjectEntry
    from rag_search.core.index_config import effective_config
    from rag_search.core.registry import remove_project, upsert_project

    root = safe_tmp_path / "root"
    member = safe_tmp_path / "member"
    root.mkdir()
    member.mkdir()
    (root / ".rse-index.yaml").write_text("index:\n  exclude:\n    - '*.gen.py'\n")
    root_path, member_path = str(root), str(member)
    upsert_project(ProjectEntry(path=root_path, enabled=True, federation=[member_path]))
    upsert_project(ProjectEntry(path=member_path, enabled=True))
    try:
        cfg = effective_config(member_path)
        assert "*.gen.py" in cfg.exclude, f"member must inherit root's exclude; got {cfg.exclude}"
    finally:
        remove_project(root_path)
        remove_project(member_path)
