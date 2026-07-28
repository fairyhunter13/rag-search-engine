"""Phase 3H — config universality (HH1–HH5). Requires live GPU embedder."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.live

_EXCLUDE_CFG = {"index": {"exclude": ["secret", "secret/**"]}}


def _write_config(root: Path, cfg: dict) -> None:
    (root / ".rse-index.yaml").write_text(yaml.dump(cfg))


def test_hh1_full_index_baseline(safe_tmp_path, embedder):
    """HH1: excluded file is absent from vector store after full index_project."""
    from rag_search.index.indexer import index_project
    from rag_search.index.store import VectorStore

    root = safe_tmp_path / "proj_hh1"
    root.mkdir()
    (root / "keep.py").write_text("def public(): pass\n")
    secret = root / "secret"
    secret.mkdir()
    (secret / "leak.go").write_text("package main\nfunc secretFn() {}\n")
    _write_config(root, _EXCLUDE_CFG)

    store_path = safe_tmp_path / "hh1.db"
    vs = VectorStore(store_path)
    try:
        index_project(root, embedder, vs, federation_mode=False)
        all_paths = {r[0] for r in vs._con.execute("SELECT path FROM chunks").fetchall()}
    finally:
        vs.close()

    assert any("keep.py" in p for p in all_paths), "keep.py must be indexed"
    assert not any("secret" in p for p in all_paths), (
        f"secret/ must be excluded by .rse-index.yaml; found: {[p for p in all_paths if 'secret' in p]}"
    )


def test_hh2_on_change_filters_excluded(safe_tmp_path, embedder):
    """HH2: _index_files filters out files excluded by .rse-index.yaml."""
    from rag_search.core.config import ProjectEntry, project_vector_db
    from rag_search.core.registry import remove_project, upsert_project
    from rag_search.daemon.sweeps import _index_files
    from rag_search.index.store import VectorStore

    root = safe_tmp_path / "proj_hh2"
    root.mkdir()
    keep_file = root / "keep.py"
    keep_file.write_text("def keep(): pass\n")
    secret = root / "secret"
    secret.mkdir()
    leak_file = secret / "leak.go"
    leak_file.write_text("package main\nfunc leak() {}\n")
    _write_config(root, _EXCLUDE_CFG)

    upsert_project(ProjectEntry(path=str(root), enabled=True))
    try:
        _index_files(str(root), [str(leak_file), str(keep_file)])
        vdb = project_vector_db(str(root))
        vs = VectorStore(vdb)
        try:
            all_paths = {r[0] for r in vs._con.execute("SELECT path FROM chunks").fetchall()}
        finally:
            vs.close()
    finally:
        remove_project(str(root))

    assert any("keep.py" in p for p in all_paths), "keep.py must be indexed"
    assert not any("leak.go" in p for p in all_paths), (
        f"secret/leak.go must be filtered by _index_files; found: {[p for p in all_paths if 'leak' in p]}"
    )


# HH3 and HH4 used to drive kb/bpre.py::_source_files, the second walk over a project — BPRE's.
# Both properties still matter, because the code-only walk still exists: `_code_source_fingerprint`
# and `_extract_graph` both compose iter_files() with is_code_language(detect_language(...)), and
# that composition is what these two now test. Written against the composition rather than a named
# helper, because the defect worth catching is the two walks disagreeing, not a signature change.


def _code_files(root: Path) -> set[str]:
    from rag_search.index.discover import detect_language, is_code_language, iter_files
    return {f.name for f in iter_files(root) if is_code_language(detect_language(f))}


def test_hh3_code_walk_honours_exclude(safe_tmp_path):
    """HH3: an excluded dir is absent from the code-only walk, not just from the index."""
    root = safe_tmp_path / "proj_hh3"
    root.mkdir()
    (root / "app.py").write_text("def run(): pass\n")
    secret = root / "secret"
    secret.mkdir()
    (secret / "credentials.go").write_text("package main\nfunc creds() {}\n")
    _write_config(root, _EXCLUDE_CFG)

    found = _code_files(root)
    assert "app.py" in found, f"app.py must survive the exclude: {found}"
    assert "credentials.go" not in found, (
        f"credentials.go must be excluded from the code walk; found: {found}"
    )


def test_hh4_code_walk_universal_discovery(safe_tmp_path):
    """HH4: the code walk finds long-tail languages (lua) and excludes non-code (md/json)."""
    root = safe_tmp_path / "proj_hh4"
    root.mkdir()
    (root / "gateway.lua").write_text('http.get("/status")\n')
    (root / "README.md").write_text("# hello\n")
    (root / "config.json").write_text('{"k":"v"}\n')
    (root / "app.go").write_text("package main\n")
    found = _code_files(root)
    assert "gateway.lua" in found, f"gateway.lua missing from the code walk: {found}"
    assert "README.md" not in found, f"README.md must not be a code file (text): {found}"
    assert "config.json" not in found, f"config.json must not be a code file (data): {found}"
    assert "app.go" in found, f"app.go missing from the code walk: {found}"


def test_hh5_is_code_language_contract():
    """HH5: is_code_language returns True for code langs, False for text/data/unknown."""
    from rag_search.index.discover import is_code_language
    assert is_code_language("lua"), "lua must be a code language"
    assert is_code_language("go"), "go must be a code language"
    assert not is_code_language("markdown"), "markdown is text, not code"
    assert not is_code_language("json"), "json is data, not code"
    assert not is_code_language("unknown"), "unknown is not code"
    assert not is_code_language(""), "empty string is not code"
