#!/usr/bin/env python3
"""Maintainer tool: generate enrichment.json goldens for sample fixture projects.

Re-run when src/tests/fixtures/sample_projects/ source changes.
Requires DEEPSEEK_API_KEY + CUDA GPU.
"""
from __future__ import annotations

import contextlib
import json
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

FIXTURES = REPO_ROOT / "src" / "tests" / "fixtures" / "sample_projects"
SHOP_FED_SRC = FIXTURES / "shop-federation"
LEDGER_SRC = FIXTURES / "ledger-standalone"
MEMBERS = ["cart-svc", "checkout-svc", "promo-svc"]
_SAFE_BASE = Path.home() / ".local" / "share" / "rse-test-dirs"
_MIN_FED_L1, _MIN_BR, _MIN_BP, _MIN_TEST_MEMBERS = 10, 1, 2, 1


def _require_key() -> None:
    from rag_search.graph.llm import deepseek_key
    if not deepseek_key():
        sys.exit("ERROR: DEEPSEEK_API_KEY not set. Export it and re-run.")


def _setup_workspace(base: Path) -> tuple[str, list[str], str]:
    from rag_search.core.config import ProjectEntry
    from rag_search.core.registry import upsert_project
    from rag_search.daemon.federation import index_members
    fed_base = base / "shop-federation"
    shutil.copytree(SHOP_FED_SRC, fed_base, ignore=shutil.ignore_patterns("enrichment.json"))
    ledger_dir = base / "ledger-standalone"
    shutil.copytree(LEDGER_SRC, ledger_dir, ignore=shutil.ignore_patterns("enrichment.json"))
    member_paths = [str(fed_base / m) for m in MEMBERS]
    fed_root = str(fed_base)
    upsert_project(ProjectEntry(path=fed_root, enabled=True, federation=member_paths))
    for m in member_paths:
        upsert_project(ProjectEntry(path=m, enabled=True))
    index_members(fed_root)
    ledger = str(ledger_dir)
    upsert_project(ProjectEntry(path=ledger, enabled=True))
    return fed_root, member_paths, ledger


def _index_all(paths: list[str]) -> None:
    from rag_search.daemon.sweeps import _index_project
    for p in paths:
        print(f"  indexing {Path(p).name}...")
        _index_project(p)


def _enrich_all(paths: list[str]) -> None:
    from rag_search.daemon.sweeps import _enrich_project
    for p in paths:
        print(f"  enriching {Path(p).name}...")
        _enrich_project(p)


def _export(project_path: str, fixture_src: Path) -> list[dict]:
    from rag_search.core.config import project_graph_db
    gdb = project_graph_db(project_path)
    communities = []
    with sqlite3.connect(str(gdb)) as con:
        rows = con.execute(
            "SELECT id,level,title,summary,member_count,semantic_type,narrated "
            "FROM communities WHERE level=1 ORDER BY id"
        ).fetchall()
        for cid, level, title, summary, mc, stype, narrated in rows:
            syms = con.execute(
                "SELECT name FROM symbols WHERE community_id=? ORDER BY name", (cid,)
            ).fetchall()
            communities.append({
                "community_id": cid, "level": level, "title": title,
                "summary": summary or "", "member_count": mc,
                "semantic_type": stype or "", "narrated": narrated or 0,
                "member_signature": sorted(r[0] for r in syms),
            })
    out = fixture_src / "enrichment.json"
    out.write_text(json.dumps(communities, indent=2))
    print(f"  {out.relative_to(REPO_ROOT)} ({len(communities)} communities)")
    return communities


def _assert_floors(member_exports: list[list[dict]], ledger_export: list[dict]) -> None:
    fed_flat = [c for comms in member_exports for c in comms]
    tc: dict[str, int] = {}
    for c in fed_flat:
        tc[c["semantic_type"]] = tc.get(c["semantic_type"], 0) + 1
    tm = sum(1 for comms in member_exports if any(c["semantic_type"] == "test" for c in comms))
    errs = []
    if len(fed_flat) < _MIN_FED_L1:
        errs.append(f"federation L1={len(fed_flat)} < {_MIN_FED_L1}")
    if tc.get("business_rule", 0) < _MIN_BR:
        errs.append(f"business_rule={tc.get('business_rule',0)} < {_MIN_BR}")
    if tc.get("business_process", 0) < _MIN_BP:
        errs.append(f"business_process={tc.get('business_process',0)} < {_MIN_BP}")
    if tm < _MIN_TEST_MEMBERS:
        errs.append(f"test_members={tm} < {_MIN_TEST_MEMBERS}")
    if not ledger_export:
        errs.append("ledger-standalone has 0 L1 communities")
    if errs:
        sys.exit("FLOOR ASSERTION FAILED:\n" + "\n".join(f"  - {e}" for e in errs))
    print(f"Floors OK: l1={len(fed_flat)} br={tc.get('business_rule',0)} "
          f"bp={tc.get('business_process',0)} tm={tm}")


def _teardown(fed_root: str, member_paths: list[str], ledger: str, base: Path) -> None:
    from rag_search.core.registry import remove_project
    for p in [fed_root, *member_paths, ledger]:
        with contextlib.suppress(Exception):
            remove_project(p)
    shutil.rmtree(base, ignore_errors=True)


def main() -> None:
    _require_key()
    _SAFE_BASE.mkdir(parents=True, exist_ok=True)
    base = Path(tempfile.mkdtemp(dir=_SAFE_BASE, prefix="enrichment-build-"))
    print(f"Workspace: {base}")
    fed_root, member_paths, ledger = "", [], ""
    try:
        fed_root, member_paths, ledger = _setup_workspace(base)
        print("Indexing (GPU embed + graph + communities)...")
        _index_all([*member_paths, ledger])
        print("Enriching (DeepSeek LLM narration + classification)...")
        _enrich_all([*member_paths, ledger])
        print("Exporting enrichment.json goldens...")
        me = [_export(member_paths[i], SHOP_FED_SRC / MEMBERS[i]) for i in range(len(MEMBERS))]
        le = _export(ledger, LEDGER_SRC)
        print("Asserting floors...")
        _assert_floors(me, le)
        print("Done. Commit the enrichment.json files.")
    finally:
        print("Tearing down workspace...")
        _teardown(fed_root, member_paths, ledger, base)


if __name__ == "__main__":
    main()
