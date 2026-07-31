"""Live tests: dotenv-family files never reach the index.

Provenance (measured 2026-07-31, whole fleet): 509 chunks from `.env` files were sitting in the
vector store, including `mysql-docker/kubernetes/mysql-credentials.env` and
`instance-secrets.env`. A chunk in the store is a chunk `search` can return and a chunk the
dashboard can paste into a `claude -p` prompt, so this was an exposure, not a coverage gap.

They were never excluded by anything. `IGNORED_DIRS` names `.env` as a *directory*, and
`_should_drop`'s hidden-name skip is restricted to directory segments on purpose, so that tracked
dotfiles (`.gitignore`, `.eslintrc`) still index. Both behaviours are correct and neither one
covers a *file* named `.env`.

SEC1 — is_secret_path() truth table: the dotenv family matches, ordinary source never does.
SEC2 — is_ignored_path() drops them, so watcher, indexer and drift gate agree by construction
       (they share `_should_drop`; testing the shared entry point is what makes that true for all
       three rather than for the one this test happened to call).
SEC3 — an explicit RSE `include` still wins, matching is_generated_path's placement in the
       decision order. Asserted so the precedence is a decision on record, not an accident.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.live


def test_sec1_is_secret_path_truth_table():
    """SEC1: the dotenv family matches; hand-written source never does."""
    from rag_search.index.discover import is_secret_path

    secret = [
        ".env",
        ".env.local",
        ".env.production",
        ".env.example",          # placeholders as a rule, a real credential when someone slips
        "misp-docker/template.env",
        "kubernetes/mysql-credentials.env",
        "kubernetes/instance-secrets.env",
    ]
    ordinary = [
        "src/main.go",
        "src/env.py",            # 'env' in the stem, not a dotenv file
        "config/environment.ts",
        "docs/env.md",
        ".envrc",                # direnv script, not a dotenv key/value file
        ".gitignore",            # the tracked dotfile the directory-only skip exists to keep
    ]
    for p in secret:
        assert is_secret_path(p), f"{p} should be treated as a secret file"
    for p in ordinary:
        assert not is_secret_path(p), f"{p} should NOT be treated as a secret file"


def test_sec2_is_ignored_path_drops_dotenv(safe_tmp_path):
    """SEC2: discovery's shared decision order drops them, so all three consumers agree."""
    from rag_search.index.discover import is_ignored_path

    root = safe_tmp_path
    (root / "app").mkdir()
    dropped = root / "app" / ".env"
    dropped.write_text("DB_PASSWORD=hunter2\n")
    kept = root / "app" / "main.py"
    kept.write_text("def main():\n    return 1\n")

    assert is_ignored_path(dropped, root), (
        f"{dropped} reached the index — a dotenv file is searchable and can be pasted into a "
        "chat prompt; see the module docstring for the 509-chunk measurement."
    )
    assert not is_ignored_path(kept, root), (
        "the exclusion took an ordinary source file with it — it must match the dotenv family only"
    )


def test_sec3_explicit_include_still_wins(safe_tmp_path):
    """SEC3: an explicit RSE `include` overrides the drop, as it does for generated files.

    Pins the precedence rather than the convenience: `cfg.include` is the user naming a file in
    their *own* repo, and silently ignoring that is the more surprising failure. If this ever
    needs to become absolute, it should change here first, deliberately.
    """
    from rag_search.core.index_config import ProjectConfig
    from rag_search.index.discover import is_ignored_path

    root = safe_tmp_path
    (root / "app").mkdir()
    target = root / "app" / ".env"
    target.write_text("DB_PASSWORD=hunter2\n")

    cfg = ProjectConfig(include=["app/.env"])
    assert not is_ignored_path(target, root, cfg), (
        "an explicit include did not override the secret-file drop — the decision order no "
        "longer matches is_generated_path's, which sits directly above it"
    )
