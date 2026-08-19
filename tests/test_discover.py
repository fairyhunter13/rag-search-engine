"""Discovery filters, the git-aware walk, and the content-hash diff."""

from __future__ import annotations

import subprocess

import pytest

from coderag import config, discover, filters
from coderag.projcfg import ProjectConfig


@pytest.mark.parametrize(
    "name",
    [".env", ".env.production", "id_rsa", "server.pem", "app.key", "credentials.json", ".npmrc"],
)
def test_secrets_are_refused(name):
    """An indexed .env is a credential in a store that answers questions."""
    assert filters.is_secret_path(name)


@pytest.mark.parametrize("name", [".env.example", ".env.sample", "config.json", "keyboard.py"])
def test_templates_and_ordinary_files_are_not_secrets(name):
    """Both directions: a filter that refuses everything passes the test above."""
    assert not filters.is_secret_path(name)


def test_nul_byte_sniff_catches_an_extensionless_binary():
    assert filters.looks_binary(b"\x7fELF\x02\x00\x00\x00text")
    assert not filters.looks_binary(b"#!/bin/sh\necho hi\n")


def test_forbidden_roots_are_refused(tmp_path):
    with pytest.raises(ValueError, match="not a project directory"):
        discover.candidates("/", ProjectConfig())
    discover.candidates(tmp_path, ProjectConfig())  # an ordinary dir is fine


def test_gitignored_files_never_reach_the_indexer(repo):
    found = discover.candidates(repo, ProjectConfig())
    assert "src/app.py" in found
    assert "ignored/junk.py" not in found, "git's own exclude chain must be honoured"


def test_the_walk_fallback_works_without_git(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "b.js").write_text("y\n")

    found = discover.candidates(tmp_path, ProjectConfig())
    assert found == ["a.py"]


def test_excludes_apply_and_includes_win_them_back(repo):
    cfg = ProjectConfig(exclude=("src/*",))
    assert discover.candidates(repo, cfg) == [".gitignore"]

    cfg = ProjectConfig(exclude=("src/*",), include=("src/app.py",))
    assert "src/app.py" in discover.candidates(repo, cfg)


def test_an_inherited_exclude_suppresses_a_member_file(repo):
    """The federation union's whole purpose, asserted on a real walk."""
    assert "src/util.js" in discover.candidates(repo, ProjectConfig())
    assert "src/util.js" not in discover.candidates(repo, ProjectConfig(exclude=("*.js",)))


def test_oversized_files_are_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MAX_FILE_BYTES", 100)
    (tmp_path / "big.py").write_text("x" * 500)
    (tmp_path / "small.py").write_text("x = 1\n")

    assert discover.candidates(tmp_path, ProjectConfig()) == ["small.py"]


def test_binary_content_is_refused_at_read_time(tmp_path):
    (tmp_path / "blob").write_bytes(b"MZ\x00\x00\x01\x02")
    assert discover.read(tmp_path, "blob") is None


def test_a_file_that_vanished_mid_walk_is_skipped_not_fatal(tmp_path):
    """A reconcile over 61,714 files races the user's editor constantly."""
    assert discover.read(tmp_path, "never-existed.py") is None


def test_read_returns_content_with_its_hash_and_language(repo):
    meta = discover.read(repo, "src/app.py")
    assert meta.lang == "python" and meta.n_lines >= 3
    assert meta.sha256 == discover.read(repo, "src/app.py").sha256
    assert "parseUserConfig" in meta.text


def test_the_diff_reports_new_changed_and_deleted(repo):
    cfg = ProjectConfig()
    write, delete = discover.changed(repo, cfg, {})
    assert {m.rel for m in write} == {".gitignore", "src/app.py", "src/util.js"}
    assert delete == []

    known = {m.rel: m.sha256 for m in write}
    write, delete = discover.changed(repo, cfg, known)
    assert write == [] and delete == [], "an unchanged tree must be a no-op"

    (repo / "src" / "app.py").write_text("def other(): pass\n")
    known["gone.py"] = "deadbeef"
    write, delete = discover.changed(repo, cfg, known)
    assert [m.rel for m in write] == ["src/app.py"]
    assert delete == ["gone.py"]


def test_a_newly_excluded_file_is_reported_for_deletion(repo):
    """The late-join reconcile: a root's excludes must remove what is already
    indexed, not merely stop adding to it."""
    write, _ = discover.changed(repo, ProjectConfig(), {})
    known = {m.rel: m.sha256 for m in write}

    _, delete = discover.changed(repo, ProjectConfig(exclude=("*.js",)), known)
    assert delete == ["src/util.js"]


def test_symlinks_are_not_followed_into_another_project(tmp_path):
    """They are registered and walked under their own resolved path."""
    proj = tmp_path / "p"
    proj.mkdir()
    (proj / "a.py").write_text("x = 1\n")
    other = tmp_path / "other"
    other.mkdir()
    (other / "b.py").write_text("y = 2\n")
    (proj / "link").symlink_to(other)

    assert discover.candidates(proj, ProjectConfig()) == ["a.py"]


def test_respect_gitignore_false_falls_back_to_the_plain_walk(repo):
    found = discover.candidates(repo, ProjectConfig(respect_gitignore=False))
    assert "ignored/junk.py" in found


def test_an_empty_file_is_not_indexed(tmp_path):
    (tmp_path / "empty.py").write_text("   \n\n")
    assert discover.read(tmp_path, "empty.py") is None


def test_docs_are_a_language_group():
    assert filters.lang_of("README.md") == "markdown"
    assert "markdown" in filters.DOC_LANGS


def test_a_git_repo_with_no_commits_still_lists_files(tmp_path):
    tmp_path.joinpath("a.py").write_text("x = 1\n")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    assert discover.candidates(tmp_path, ProjectConfig()) == ["a.py"]
