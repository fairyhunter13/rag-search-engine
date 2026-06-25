"""Phase 3H — config universality (HH1–HH3). Requires live GPU embedder."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.live

_EXCLUDE_CFG = {"index": {"exclude": ["secret", "secret/**"]}}


def _write_config(root: Path, cfg: dict) -> None:
    (root / ".opencode-index.yaml").write_text(yaml.dump(cfg))


def test_hh1_full_index_baseline(safe_tmp_path, embedder):
    """HH1: excluded file is absent from vector store after full index_project."""
    from opencode_search.index.indexer import index_project
    from opencode_search.index.store import VectorStore

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
        f"secret/ must be excluded by .opencode-index.yaml; found: {[p for p in all_paths if 'secret' in p]}"
    )


def test_hh2_on_change_filters_excluded(safe_tmp_path, embedder):
    """HH2: _index_files filters out files excluded by .opencode-index.yaml."""
    from opencode_search.core.config import ProjectEntry, project_vector_db
    from opencode_search.core.registry import remove_project, upsert_project
    from opencode_search.daemon.sweeps import _index_files
    from opencode_search.index.store import VectorStore

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


def test_hh3_bpre_exclude(safe_tmp_path):
    """HH3: excluded dir absent from _source_files BPRE walk."""
    from opencode_search.kb.bpre import _source_files

    root = safe_tmp_path / "proj_hh3"
    root.mkdir()
    (root / "app.py").write_text("def run(): pass\n")
    secret = root / "secret"
    secret.mkdir()
    (secret / "credentials.go").write_text("package main\nfunc creds() {}\n")
    _write_config(root, _EXCLUDE_CFG)

    bpre_files = _source_files(str(root))
    bpre_strs = [str(f) for f in bpre_files]
    assert not any("credentials.go" in p for p in bpre_strs), (
        f"credentials.go must be excluded from _source_files; found: {bpre_strs}"
    )
