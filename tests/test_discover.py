"""Discovery filters, the git-aware walk, and the content-hash diff."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coderag import config, discover, filters, ignores
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


@pytest.mark.parametrize(
    "name", ["prod.env", ".global.env", "svc.env.enc", "backup.env.bak", "creds.env.json"]
)
def test_a_dotted_env_suffix_is_refused_too(name):
    """`fnmatch` anchors the whole name, so `.env.*` is only the leading form.

    These five shapes are the trailing one, and every one of them passed until a
    fleet sweep counted 352 tracked files carrying them. Named individually rather
    than asserted over the pattern tuple: a test that reads the tuple it is
    guarding agrees with any tuple, including an empty one.
    """
    assert filters.is_secret_path(name)


@pytest.mark.parametrize("name", ["app.envelope", "config.environment", "docs/environments.md"])
def test_env_lookalikes_still_index(name):
    """The trailing patterns must not swallow ordinary words containing "env"."""
    assert not filters.is_secret_path(name)


@pytest.mark.parametrize("rel", ["notes.ipynb", "data/rows.csv", "data/rows.tsv"])
def test_notebooks_and_tables_are_not_indexed(rel):
    """Excluded upstream of the chunker because no splitter rescues them.

    A notebook is JSON with base64 output blobs; a CSV strands its header row in
    the first chunk. Both produce chunks that cannot answer a query.
    """
    assert not discover.indexable(rel, ProjectConfig())
    assert discover.indexable(rel, ProjectConfig(use_default_ignores=False)), (
        "the refusal must come from DEFAULT_IGNORES, not from a binary or secret rule"
    )


@pytest.mark.parametrize("name", ["laravel-env", "prod-env", "env-production", "app.env-enc"])
def test_the_dash_spellings_of_env_are_refused(name):
    """`*.env` needs a literal dot, and the dash forms slipped past it.

    `laravel-env` was indexed here with 287 value-bearing assignments in it.
    """
    assert filters.is_secret_path(name)


@pytest.mark.parametrize("name", ["env-template", ".env-example", "config-sample"])
def test_a_dash_spelled_template_stays_indexable(name):
    """Exempted deliberately rather than missed: the exempt list has the dash forms too."""
    assert not filters.is_secret_path(name)


@pytest.mark.parametrize(
    "rel",
    [
        "packages/a/node_modules/x.js",
        "api/vendor/lib.php",
        "a/b/__pycache__/m.pyc",
        "sub/.git/config",
        "app/frontend/dist/bundle.js",
    ],
)
def test_an_ignored_directory_is_ignored_at_any_depth(rel):
    """The old `node_modules/*` was root-anchored and let 278 files through."""
    assert not discover.indexable(rel, ProjectConfig())


@pytest.mark.parametrize(
    "rel",
    [
        "lib/main.dart",
        "public/index.php",
        "resources/views/home.blade.php",
        "testdata/golden.json",
        "go.mod",
        "types/api.d.ts",
        "src/query.sql",
        "docs/architecture.md",
    ],
)
def test_the_names_refused_from_the_ignore_list_still_index(rel):
    """The half that catches an over-broad list, which otherwise fails silently.

    Each of these is a directory or suffix some upstream list prunes and this one
    does not: `lib/` is a Dart source root, `testdata/` the Go fixture convention,
    `.d.ts` often the only readable form of a dependency's API.
    """
    assert discover.indexable(rel, ProjectConfig())


def test_every_ignored_directory_is_a_bare_segment():
    """A slash or a glob character here is an entry the segment matcher never fires on."""
    for name in ignores.IGNORE_DIRS:
        assert "/" not in name and not set(name) & set("*?[")


def test_every_ignored_name_is_bare_and_lowercase():
    """The set is probed with a lowercased name, so an uppercase entry is unreachable."""
    for name in ignores.IGNORE_NAMES:
        assert name == name.lower() and "/" not in name and not set(name) & set("*?[")


def test_the_glob_list_holds_no_whole_filename():
    """A whole name spelled as a glob is root-anchored -- the defect this list
    already carries for directories. 27 nested lockfiles were indexed that way."""
    plain = [p for p in ignores.DEFAULT_IGNORES if not set(p) & set("*?[")]
    assert plain == [], plain


@pytest.mark.parametrize(
    "rel", ["packages/a/package-lock.json", "svc/api/go.sum", "infra/mod/.terraform.lock.hcl"]
)
def test_a_generated_file_is_ignored_at_any_depth_not_only_at_the_root(rel):
    assert not discover.indexable(rel, ProjectConfig())


def test_no_extension_is_both_a_language_and_a_binary():
    """`is_binary_ext` gates indexing, so an overlap silently refuses source.
    `.vhd` was VHDL and a disk image at once, and the disk image won."""
    assert set(filters.LANGS) & set(filters._BINARY_EXT) == set()
    assert set(filters.LANGS) & set(filters._IMAGE_EXT) == set()


def test_no_whole_filename_is_shadowed_by_its_own_suffix():
    """`CMakeLists.txt` resolved to docs, because `.txt` was consulted first."""
    assert filters.lang_of("CMakeLists.txt") == "cmake"


@pytest.mark.parametrize(
    ("rel", "lang"),
    [("Dockerfile", "dockerfile"), ("ci/Jenkinsfile", "groovy"), ("Makefile", "make")],
)
def test_a_file_with_no_extension_still_has_a_language(rel, lang):
    """`Path.suffix` is empty for 1,760 files here, most of them build definitions."""
    assert filters.lang_of(rel) == lang


def test_generated_icon_markup_is_treated_as_an_image():
    """8,039 SVGs, nearly all generated, and the top source of chunks with no newline."""
    assert filters.is_image_path("assets/icons/close.svg")
    assert not filters.is_binary_ext("go.mod"), "`.mod` is a suffix source uses"


def test_nul_byte_sniff_catches_an_extensionless_binary():
    assert filters.looks_binary(b"\x7fELF\x02\x00\x00\x00text")
    assert not filters.looks_binary(b"#!/bin/sh\necho hi\n")


def test_forbidden_roots_are_refused(tmp_path):
    with pytest.raises(ValueError, match="not a project directory"):
        discover.candidates("/", ProjectConfig())
    discover.candidates(tmp_path, ProjectConfig())  # an ordinary dir is fine


def test_a_subdirectory_of_a_forbidden_tree_is_refused_too():
    """An exact-match check never fires: a caller names a directory inside the
    cache, never the cache itself."""
    assert filters.is_forbidden_root(Path.home() / ".cache" / "some-tool" / "checkout")
    assert filters.is_forbidden_root("/usr/share/doc")
    # And the two that cannot walk: every project on this machine is under both.
    assert not filters.is_forbidden_root(Path.home() / "git" / "project")


def test_gitignored_files_never_reach_the_indexer(repo):
    found = discover.candidates(repo, ProjectConfig())
    assert "src/app.py" in found
    assert "ignored/junk.py" not in found, "git's own exclude chain must be honoured"


def _submodule(outer: Path, at: str, tmp_path: Path, files: dict[str, str], name="inner") -> Path:
    """Add a real, populated submodule and stage the gitlink."""
    inner = tmp_path / name
    inner.mkdir(parents=True)
    for rel, text in files.items():
        (inner / rel).parent.mkdir(parents=True, exist_ok=True)
        (inner / rel).write_text(text)
    subprocess.run(["git", "init", "-q"], cwd=inner, check=True)
    subprocess.run(["git", "add", "-A"], cwd=inner, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "seed"],
        cwd=inner,
        check=True,
    )
    subprocess.run(
        # `protocol.file.allow` is denied by default since CVE-2022-39253,
        # and a local path is the only clone source a test has.
        ["git", "-c", "protocol.file.allow=always", "submodule", "add", "-q", "--", str(inner), at],
        cwd=outer,
        check=True,
        capture_output=True,
    )
    return outer / at


def test_a_populated_submodule_reaches_the_indexer(repo, tmp_path):
    """`ls-files` lists a gitlink and never descends, so this was empty."""
    _submodule(repo, "Domain", tmp_path, {"b.py": "y = 2\n", "deep/c.py": "z = 3\n"})

    found = discover.candidates(repo, ProjectConfig())
    assert "Domain/b.py" in found
    assert "Domain/deep/c.py" in found
    assert "src/app.py" in found


def test_an_exclude_still_drops_a_submodule_file(repo, tmp_path):
    """The path is prefixed back to the outer project, so the outer list bites."""
    _submodule(repo, "Domain", tmp_path, {"b.py": "y = 2\n"})
    _submodule(repo, "Other", tmp_path, {"c.py": "z = 3\n"}, name="other")

    found = discover.candidates(repo, ProjectConfig(exclude=("Domain/*",)))
    assert "Domain/b.py" not in found
    # The kept sibling is what reds this arm on the pre-fix code, where the
    # absence above holds for the wrong reason.
    assert "Other/c.py" in found
    assert "src/app.py" in found


def test_an_empty_submodule_directory_adds_nothing(repo, tmp_path):
    """47 of 69 worktrees in this workspace hold exactly this."""
    _submodule(repo, "Domain", tmp_path, {"b.py": "y = 2\n"})
    subprocess.run(
        ["git", "submodule", "deinit", "-f", "Domain"], cwd=repo, check=True, capture_output=True
    )

    assert (repo / "Domain").is_dir()
    assert [f for f in discover.candidates(repo, ProjectConfig()) if f.startswith("Domain")] == []


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


def test_a_committed_file_symlink_is_not_read_through(tmp_path):
    """The default lane is git's, and `git ls-files` lists a symlink as an
    ordinary path. `is_file()` follows it, so a link committed into a repo
    indexed that file's content and attributed it to this project -- reachable
    by anyone pinned here. The walk lane refused the same file."""
    secret = tmp_path / "outside" / "notes.md"
    secret.parent.mkdir()
    secret.write_text("a private note that is not in this repo\n")
    proj = tmp_path / "p"
    proj.mkdir()
    (proj / "a.py").write_text("x = 1\n")
    (proj / "notes.md").symlink_to(secret)
    subprocess.run(["git", "init", "-q"], cwd=proj, check=True)
    subprocess.run(["git", "add", "-A"], cwd=proj, check=True)

    assert discover._git_files(proj) and "notes.md" in discover._git_files(proj)
    assert discover.candidates(proj, ProjectConfig()) == ["a.py"]
    assert discover.read(proj, "notes.md") is None


def test_a_file_symlink_is_not_read_through_in_the_walk_lane_either(tmp_path):
    """The lanes have to agree: which one runs is a config default away."""
    secret = tmp_path / "outside" / "notes.md"
    secret.parent.mkdir()
    secret.write_text("a private note that is not in this repo\n")
    proj = tmp_path / "p"
    proj.mkdir()
    (proj / "a.py").write_text("x = 1\n")
    (proj / "notes.md").symlink_to(secret)

    assert discover.candidates(proj, ProjectConfig(respect_gitignore=False)) == ["a.py"]


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
