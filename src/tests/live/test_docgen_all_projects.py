"""Phase 6 — live e2e: run ose-docgen against ALL globally indexed projects.

Imports ose_docgen from vendor/docgen/src (submodule — no OSE import coupling).
Each test project runs generate(llm=False) into a throwaway tmp dir (never
mutating the real repo). Asserts: no errors, tree built, no path leak, idempotent.

Projects skipped: ocs-test-dirs/tmp*, test-* indexed paths, empty graph.db.
"""
from __future__ import annotations

import hashlib
import sqlite3
import sys
from pathlib import Path

import pytest

_VENDOR_SRC = Path(__file__).parent.parent.parent.parent / "vendor" / "docgen" / "src"
if str(_VENDOR_SRC) not in sys.path:
    sys.path.insert(0, str(_VENDOR_SRC))

from ose_docgen.generate import generate  # noqa: E402 (path insertion above)


def _db_has_content(graph_db: Path) -> bool:
    """True if the graph.db has at least some communities or symbols."""
    try:
        con = sqlite3.connect(f"file:{graph_db}?mode=ro", uri=True)
        n = con.execute("SELECT COUNT(*) FROM communities").fetchone()[0]
        con.close()
        return n > 0
    except Exception:
        return False


def _should_skip(path: str) -> bool:
    p = Path(path)
    name = p.name
    return (
        "ocs-test-dirs" in path
        or name.startswith("tmp")
        or name.startswith("test-")
        or ".local/share/ocs-test" in path
    )


def _collect_projects() -> list[str]:
    """Return project_paths for every real, runnable indexed project."""
    from opencode_search.core.registry import list_projects

    result: list[str] = []
    for p in list_projects():
        if not p.enabled or _should_skip(p.path):
            continue
        if not Path(p.path).is_dir():
            continue
        result.append(p.path)
    return result


_ALL_PROJECTS = _collect_projects()
_PROJECT_IDS = [Path(pp).name for pp in _ALL_PROJECTS]


@pytest.mark.live
@pytest.mark.parametrize("project_path", _ALL_PROJECTS, ids=_PROJECT_IDS)
def test_docgen_skeleton_no_errors(project_path, tmp_path):
    """Every indexed project must yield a valid docs skeleton with no errors."""
    result = generate(project_path=project_path, docs_dir=str(tmp_path), llm=False)
    assert result["errors"] == [], f"{Path(project_path).name}: errors: {result['errors']}"
    assert len(result["written"]) + len(result["skipped"]) > 5, (
        f"{Path(project_path).name}: expected >5 files, got {len(result['written'])+len(result['skipped'])}"
    )


@pytest.mark.live
@pytest.mark.parametrize("project_path", _ALL_PROJECTS, ids=_PROJECT_IDS)
def test_docgen_no_path_leak(project_path, tmp_path):
    """No absolute device path may appear in any generated .md file."""
    generate(project_path=project_path, docs_dir=str(tmp_path), llm=False)
    home = str(Path.home())
    leaks = []
    for md in tmp_path.rglob("*.md"):
        if home in md.read_text(encoding="utf-8", errors="replace"):
            leaks.append(md.name)
    assert not leaks, f"{Path(project_path).name}: path leak in: {leaks}"


@pytest.mark.live
@pytest.mark.parametrize("project_path", _ALL_PROJECTS, ids=_PROJECT_IDS)
def test_docgen_idempotent(project_path, tmp_path):
    """Second run with same source must produce zero writes."""
    generate(project_path=project_path, docs_dir=str(tmp_path), llm=False)
    r2 = generate(project_path=project_path, docs_dir=str(tmp_path), llm=False)
    assert r2["written"] == [], (
        f"{Path(project_path).name}: second run wrote {len(r2['written'])} files"
    )


# ── Standardization sub-track (5 real docs-bearing projects) ─────────────────

_DOCS_PROJECTS = [pp for pp in _ALL_PROJECTS if (Path(pp) / "docs").is_dir()]
_DOCS_IDS = [Path(pp).name for pp in _DOCS_PROJECTS]


@pytest.mark.live
@pytest.mark.parametrize("project_path", _DOCS_PROJECTS, ids=_DOCS_IDS)
def test_standardize_human_files_preserved(project_path, tmp_path):
    """Human docs in existing docs/ must be byte-unchanged after standardization."""
    from ose_docgen.provenance import classify

    real_docs = Path(project_path) / "docs"
    human_hashes: dict[str, str] = {}
    for f in real_docs.rglob("*"):
        if f.is_file() and classify(f) == "human":
            rel = f.relative_to(real_docs)
            dest = tmp_path / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(f.read_bytes())
            human_hashes[str(rel)] = hashlib.sha256(f.read_bytes()).hexdigest()

    generate(project_path=project_path, docs_dir=str(tmp_path), llm=False)

    for rel, before_hash in human_hashes.items():
        after = (tmp_path / rel).read_bytes()
        assert hashlib.sha256(after).hexdigest() == before_hash, (
            f"{Path(project_path).name}: human file `{rel}` was modified"
        )


@pytest.mark.live
@pytest.mark.parametrize("project_path", _DOCS_PROJECTS, ids=_DOCS_IDS)
def test_standardize_migration_md_emitted(project_path, tmp_path):
    """MIGRATION.md must be emitted for every docs-bearing project."""
    generate(project_path=project_path, docs_dir=str(tmp_path), llm=False)
    assert (tmp_path / "_meta" / "MIGRATION.md").exists()
