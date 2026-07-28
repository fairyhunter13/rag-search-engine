"""Session-scoped sample workspace builder for the live test suite.

Materializes committed fixture trees into a temporary registry workspace,
indexes with GPU, labels the communities the way the daemon now does, then
tears down cleanly.

The labelling step used to replay four committed enrichment.json goldens —
frozen DeepSeek narrations, so the suite never called the API. Tier 3's
deletion removed both the narrator and the goldens, and structural labelling
is not a stand-in for them: it is what the daemon writes now, so building the
workspace with `_label_project` makes the fixture match production instead of
preserving a shape nothing produces any more.

Usage:
    from tests.live._sample_workspace import SampleWorkspace, build_sample_workspace, teardown_sample_workspace
"""
from __future__ import annotations

import contextlib
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from rag_search.core.config import ProjectEntry
from rag_search.core.registry import remove_project, upsert_project

_REPO_ROOT = Path(__file__).parents[3]
_FIXTURES = _REPO_ROOT / "src" / "tests" / "fixtures" / "sample_projects"
_SHOP_SRC = _FIXTURES / "shop-federation"
_LEDGER_SRC = _FIXTURES / "ledger-standalone"
_MEMBERS = ["cart-svc", "checkout-svc", "promo-svc"]
_SAFE_BASE = Path.home() / ".local" / "share" / "rse-test-dirs"


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
    shutil.copytree(_SHOP_SRC, fed_base)
    ledger_dir = base / "ledger-standalone"
    shutil.copytree(_LEDGER_SRC, ledger_dir)
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


def label_member(member_path: str) -> None:
    """Re-label a sample member's communities (idempotent self-heal).

    Replaces replay_member_golden, which re-applied a frozen DeepSeek narration after an
    in-process re-index cleared it. _label_project only touches communities whose summary is
    NULL or empty, so calling it twice is free.
    """
    from rag_search.daemon.sweeps import _label_project
    _label_project(member_path)


def _index_members(paths: list[str]) -> None:
    from rag_search.daemon.sweeps import _index_project
    for p in paths:
        _index_project(p)


# _replay_golden and its _live_sig helper went with the goldens. They existed to re-key a frozen
# narration onto whatever community ids Leiden happened to produce this run — matching on the
# member signature when the ids drifted. Structural labelling reads the live graph directly, so
# there is nothing left to re-key.


def _deregister_under(base: Path) -> None:
    """Remove all registry entries whose path is under base (enabled or not)."""
    from rag_search.core.registry import list_projects
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
    # Also clear any stale rse-test-dirs entries whose filesystem path no longer exists.
    from rag_search.core.registry import list_projects
    for e in list_projects():
        if str(_SAFE_BASE) in e.path and not Path(e.path).exists():
            with contextlib.suppress(Exception):
                remove_project(e.path)


def build_sample_workspace() -> SampleWorkspace:
    # The `no_deepseek()` context manager that used to wrap this whole body is gone with the client
    # it suppressed. Nothing here reaches a network at all now, so there is no lane left to close.
    _SAFE_BASE.mkdir(parents=True, exist_ok=True)
    base = Path(tempfile.mkdtemp(dir=_SAFE_BASE, prefix="sample-ws-"))
    _cleanup_stale_workspaces(base)
    fed_base, ledger_dir = _copy_fixtures(base)
    fed_root, member_paths, ledger = _register(fed_base, ledger_dir)
    _index_members([fed_root, *member_paths, ledger])
    for p in [*member_paths, ledger]:
        label_member(p)
    # reconstruct_processes (BPRE) and build_federated_index (wiki) ran here; both left with tier 3.
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
