"""Session-scoped sample workspace builder for the live test suite.

Materializes committed fixture trees into a temporary registry workspace,
indexes with GPU, replays frozen enrichment.json goldens (no DeepSeek at
test time), then tears down cleanly.

Usage:
    from tests.live._sample_workspace import SampleWorkspace, build_sample_workspace, teardown_sample_workspace
"""
from __future__ import annotations

import contextlib
import json
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path

import pytest

from opencode_search.core.config import ProjectEntry, project_graph_db
from opencode_search.core.registry import remove_project, upsert_project
from opencode_search.graph.store import GraphStore

_REPO_ROOT = Path(__file__).parents[3]
_FIXTURES = _REPO_ROOT / "src" / "tests" / "fixtures" / "sample_projects"
_SHOP_SRC = _FIXTURES / "shop-federation"
_LEDGER_SRC = _FIXTURES / "ledger-standalone"
_MEMBERS = ["cart-svc", "checkout-svc", "promo-svc"]
_SAFE_BASE = Path.home() / ".local" / "share" / "ocs-test-dirs"


@dataclass
class SampleWorkspace:
    base: Path
    fed_root: str
    cart: str
    checkout: str
    promo: str
    ledger: str

    @property
    def member_paths(self) -> list[str]:
        return [self.cart, self.checkout, self.promo]


def _copy_fixtures(base: Path) -> tuple[Path, Path]:
    fed_base = base / "shop-federation"
    shutil.copytree(_SHOP_SRC, fed_base, ignore=shutil.ignore_patterns("enrichment.json"))
    ledger_dir = base / "ledger-standalone"
    shutil.copytree(_LEDGER_SRC, ledger_dir, ignore=shutil.ignore_patterns("enrichment.json"))
    return fed_base, ledger_dir


def _register(fed_base: Path, ledger_dir: Path) -> tuple[str, list[str], str]:
    member_paths = [str(fed_base / m) for m in _MEMBERS]
    fed_root = str(fed_base)
    upsert_project(ProjectEntry(path=fed_root, enabled=True, federation=member_paths))
    for m in member_paths:
        upsert_project(ProjectEntry(path=m, enabled=True))
    ledger = str(ledger_dir)
    upsert_project(ProjectEntry(path=ledger, enabled=True))
    return fed_root, member_paths, ledger


def _golden_path_for(member_path: str) -> Path:
    """Return the committed enrichment.json golden for a sample member path."""
    name = Path(member_path).name
    if name in _MEMBERS:
        return _SHOP_SRC / name / "enrichment.json"
    return _LEDGER_SRC / "enrichment.json"


def replay_member_golden(member_path: str) -> None:
    """Re-apply the committed enrichment golden for a sample member (idempotent self-heal).

    Restores golden community summaries after an in-process re-index/re-derive cleared them.
    """
    _replay_golden(member_path, _golden_path_for(member_path))


def _index_members(paths: list[str]) -> None:
    from opencode_search.daemon.sweeps import _index_project
    for p in paths:
        _index_project(p)


def _live_sig(con: sqlite3.Connection, cid: int) -> list[str]:
    return sorted(r[0] for r in con.execute(
        "SELECT name FROM symbols WHERE community_id=? ORDER BY name", (cid,)
    ).fetchall())


def _replay_golden(project_path: str, golden_path: Path) -> None:
    if not golden_path.exists():
        pytest.fail(
            f"enrichment.json missing for {Path(project_path).name} — "
            "run scripts/build_sample_enrichment.py to generate it"
        )
    golden = json.loads(golden_path.read_text())
    gdb = project_graph_db(project_path)
    gs = GraphStore(gdb)
    try:
        with sqlite3.connect(str(gdb)) as con:
            live_ids = {r[0] for r in con.execute("SELECT id FROM communities WHERE level=1").fetchall()}
            for row in golden:
                cid = row["community_id"]
                if cid not in live_ids:
                    golden_sig = sorted(row["member_signature"])
                    matched = next(
                        (lid for lid in live_ids if _live_sig(con, lid) == golden_sig),
                        None,
                    )
                    if matched is None:
                        pytest.fail(
                            f"Golden community {cid!r} in {golden_path} has no match in live "
                            "graph.db — re-run scripts/build_sample_enrichment.py"
                        )
                    cid = matched
                gs.upsert_community(
                    cid, row["level"], row["title"], row["summary"],
                    row["member_count"], row["semantic_type"], row["narrated"],
                )
        gs.commit()
    finally:
        gs.close()


def _deregister_under(base: Path) -> None:
    """Remove all registry entries whose path is under base (enabled or not)."""
    from opencode_search.core.registry import list_projects
    prefix = str(base) + "/"
    for e in list_projects():
        if e.path.startswith(prefix) or e.path == str(base):
            with contextlib.suppress(Exception):
                remove_project(e.path)


def _cleanup_stale_workspaces(keep: Path) -> None:
    """Remove leftover sample-ws-* dirs and their registry entries from crashed sessions."""
    for d in _SAFE_BASE.glob("sample-ws-*"):
        if d != keep:
            _deregister_under(d)
            shutil.rmtree(d, ignore_errors=True)
    # Also clear any stale ocs-test-dirs entries whose filesystem path no longer exists.
    from opencode_search.core.registry import list_projects
    for e in list_projects():
        if str(_SAFE_BASE) in e.path and not Path(e.path).exists():
            with contextlib.suppress(Exception):
                remove_project(e.path)


def build_sample_workspace() -> SampleWorkspace:
    from opencode_search.graph.llm import no_deepseek
    _SAFE_BASE.mkdir(parents=True, exist_ok=True)
    base = Path(tempfile.mkdtemp(dir=_SAFE_BASE, prefix="sample-ws-"))
    _cleanup_stale_workspaces(base)
    with no_deepseek():
        fed_base, ledger_dir = _copy_fixtures(base)
        fed_root, member_paths, ledger = _register(fed_base, ledger_dir)
        _index_members([fed_root, *member_paths, ledger])
        for mp in member_paths:
            _replay_golden(mp, _golden_path_for(mp))
        _replay_golden(ledger, _golden_path_for(ledger))
        from opencode_search.kb.bpre import reconstruct_processes
        reconstruct_processes(fed_root)
        from opencode_search.kb.wiki import build_federated_index
        build_federated_index(fed_root)
    return SampleWorkspace(
        base=base,
        fed_root=fed_root,
        cart=member_paths[0],
        checkout=member_paths[1],
        promo=member_paths[2],
        ledger=ledger,
    )


def teardown_sample_workspace(ws: SampleWorkspace) -> None:
    for p in [ws.fed_root, *ws.member_paths, ws.ledger]:
        with contextlib.suppress(Exception):
            remove_project(p)
    shutil.rmtree(ws.base, ignore_errors=True)
