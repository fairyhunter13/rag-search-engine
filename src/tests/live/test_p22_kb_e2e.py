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

_PROJECTS = {
    "ose": "/home/user/git/github.com/fairyhunter13/opencode-search-engine",
    "astro": "/home/user/git/github.com/fairyhunter13/astro-project",
    "payment": "/home/user/go/src/github.com/example-org/payment-gateway",
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
    """Trigger enrich (+ federation members), poll until kb_state==ready, l2==100. Hard-fails on timeout."""
    import time
    hdr = {"Content-Type": "application/json"}
    requests.post(f"{_BASE}/api/enrich_project", json={"project_path": project}, headers=hdr, timeout=10)
    # Fan out to federation members not yet ready so worst-of converges
    s = _overview("status", project)
    for m in s.get("members", []):
        if m.get("kb_state") != "ready":
            requests.post(f"{_BASE}/api/enrich_project", json={"project_path": m["path"]},
                          headers=hdr, timeout=10)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        s = _overview("status", project)
        if s.get("kb_state") == "ready" and s.get("l2_enriched_pct") == 100.0:
            return
        time.sleep(3)
    s = _overview("status", project)
    assert s.get("kb_state") == "ready" and s.get("l2_enriched_pct") == 100.0, (
        f"{project!r} did not reach ready in {timeout}s — "
        f"kb_state={s.get('kb_state')!r}, l2={s.get('l2_enriched_pct')}"
    )


# ---------------------------------------------------------------------------
# S5: E2E MCP round-trip for 3 named projects
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("proj_key", ["ose", "astro", "payment"])
def test_e2e_status_has_required_fields(live_client, proj_key):
    """S5a: overview(status) returns required fields for each named project."""
    project = _PROJECTS[proj_key]
    status = _overview("status", project)
    for field in ("kb_state", "enriched_pct", "l1_enriched_pct", "l2_enriched_pct",
                  "symbols", "communities"):
        assert field in status, f"overview(status) missing {field!r} for {proj_key}"
    assert status["kb_state"] in ("indexing", "searchable", "enriching", "ready"), (
        f"unexpected kb_state={status['kb_state']!r} for {proj_key}"
    )


@pytest.mark.parametrize("proj_key", ["ose", "astro", "payment"])
def test_e2e_no_domain_placeholders(live_client, proj_key):
    """S5b: architecture_domains must not contain 'Domain N' placeholder titles."""
    project = _PROJECTS[proj_key]
    data = _overview("architecture_domains", project)
    domains = data.get("architecture_domains", [])
    placeholder = re.compile(r"^Domain\s+\d+$", re.IGNORECASE)
    bad = [d.get("title", "") for d in domains if placeholder.match(d.get("title", "") or "")]
    assert not bad, f"{proj_key}: placeholder domain titles found: {bad}"


@pytest.mark.parametrize("proj_key", ["ose", "astro", "payment"])
def test_e2e_ask_global_non_empty(live_client, proj_key):
    """S5c: POST /api/ask scope=global returns a non-empty answer."""
    project = _PROJECTS[proj_key]
    r = requests.post(f"{_BASE}/api/ask",
                      json={"query": "What is the overall architecture?",
                            "project_path": project, "scope": "global"},
                      headers=_HDR, timeout=60)
    assert r.status_code == 200, f"ask(global, {proj_key}): HTTP {r.status_code}"
    assert len(r.text.strip()) > 20, f"ask(global, {proj_key}): response too short"


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
    assert root is not None, "no indexed project with external symlinked sub-dirs (expected astro-project)"
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
    assert root is not None, "no indexed project with external symlinked sub-dirs (expected astro-project)"
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


def test_api_federation_uses_expand_federation(safe_tmp_path):
    """/api/federation must return the real symlink-based members, not all enabled projects."""
    import shutil

    from opencode_search.core.config import ProjectEntry, index_dir
    from opencode_search.core.registry import remove_project, upsert_project
    from opencode_search.daemon.federation import index_members

    uid = str(id(safe_tmp_path))[-6:]
    root = safe_tmp_path / "root"
    member = safe_tmp_path / f"member-{uid}"
    root.mkdir()
    member.mkdir()
    (member / "x.py").write_text("x = 1\n")
    (root / "link").symlink_to(member)
    remove_project(str(root))
    remove_project(str(member))
    try:
        upsert_project(ProjectEntry(path=str(root), enabled=True))
        index_members(str(root))
        r = requests.get(f"{_BASE}/api/federation", params={"project": str(root)})
        assert r.status_code == 200, f"HTTP {r.status_code}: {r.text}"
        data = r.json()
        assert data.get("members") == [str(member)], (
            f"Expected members=[{member}], got {data.get('members')}"
        )
    finally:
        remove_project(str(root))
        remove_project(str(member))
        shutil.rmtree(index_dir(str(root)), ignore_errors=True)
        shutil.rmtree(index_dir(str(member)), ignore_errors=True)


# ---------------------------------------------------------------------------
# T1/HR7: all 3 projects must reach kb_state='ready'
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("proj_key", ["ose", "payment", "astro"])
def test_kb_state_ready_all_projects(live_client, proj_key):
    """T1/TC1/HR7: converge+assert kb_state='ready' and l2_enriched_pct==100 for all 3 projects."""
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
# T6/HR4: federation fan-out on the REAL astro-project
# ---------------------------------------------------------------------------

def test_real_federation_fanout(live_client):
    """T6/HR4: astro-project members list has ≥1 member with real symbols; search fans out."""
    status = _overview("status", _PROJECTS["astro"])
    members = status.get("members", [])
    assert members, "astro-project overview(status) returned no members (federation broken)"
    with_syms = [m for m in members if m.get("symbols", 0) > 0]
    assert with_syms, (
        f"no astro members have symbols > 0 (fan-out not working); "
        f"sample: {[(m['path'].split('/')[-1], m['symbols']) for m in members[:3]]}"
    )
    r = requests.post(f"{_BASE}/api/search",
                      json={"query": "function", "project_paths": [_PROJECTS["astro"]]},
                      headers=_HDR, timeout=20)
    assert r.status_code == 200, f"search fan-out from astro-project: HTTP {r.status_code}"
    results = json.loads(r.text).get("results", [])
    assert results, "search(project_paths=[astro-project]) returned no results (fan-out broken)"
