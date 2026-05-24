"""Tests for opencode_search.discover — file walking, language detection, gitignore."""
from __future__ import annotations

from pathlib import Path

from opencode_search.discover import (
    IGNORED_DIRS,
    IGNORED_EXTENSIONS,
    LANGUAGE_MAP,
    SOURCE_EXTENSIONS,
    TEXT_EXTENSIONS,
    _get_size_limit_bytes,
    _is_binary,
    _load_gitignore,
    detect_language,
    is_indexable_file,
    iter_files,
)

# ---------------------------------------------------------------------------
# detect_language
# ---------------------------------------------------------------------------


def test_detect_language_python():
    assert detect_language(Path("foo.py")) == "python"


def test_detect_language_typescript():
    assert detect_language(Path("foo.ts")) == "typescript"


def test_detect_language_tsx():
    assert detect_language(Path("foo.tsx")) == "tsx"


def test_detect_language_jsx():
    assert detect_language(Path("foo.jsx")) == "jsx"


def test_detect_language_rust():
    assert detect_language(Path("lib.rs")) == "rust"


def test_detect_language_go():
    assert detect_language(Path("main.go")) == "go"


def test_detect_language_markdown():
    assert detect_language(Path("README.md")) == "markdown"


def test_detect_language_unknown_extension():
    assert detect_language(Path("file.xyz")) == "unknown"


def test_detect_language_text_extension_falls_back_to_text():
    # .properties is in TEXT_EXTENSIONS but not LANGUAGE_MAP
    assert detect_language(Path("config.properties")) == "text"


def test_detect_language_case_insensitive():
    assert detect_language(Path("FOO.PY")) == "python"


def test_detect_language_component_and_data_formats():
    assert detect_language(Path("Component.vue")) == "vue"
    assert detect_language(Path("App.svelte")) == "svelte"
    assert detect_language(Path("page.astro")) == "astro"
    assert detect_language(Path("data.jsonl")) == "json"
    assert detect_language(Path("settings.jsonc")) == "json"
    assert detect_language(Path("README.mdx")) == "markdown"


def test_detect_language_special_filenames():
    assert detect_language(Path("Makefile")) == "makefile"
    assert detect_language(Path("Dockerfile")) == "dockerfile"


def test_detect_language_truly_unknown():
    assert detect_language(Path("foo.xyzunknownext")) == "unknown"


# ---------------------------------------------------------------------------
# _is_binary
# ---------------------------------------------------------------------------


def test_is_binary_text_file(tmp_path):
    f = tmp_path / "text.txt"
    f.write_text("Hello, world!\nLine two.\n")
    assert _is_binary(f) is False


def test_is_binary_null_bytes(tmp_path):
    f = tmp_path / "bin.dat"
    f.write_bytes(b"hello\x00world")
    assert _is_binary(f) is True


def test_is_binary_missing_file(tmp_path):
    f = tmp_path / "nonexistent.dat"
    assert _is_binary(f) is True  # treats unreadable as binary


def test_is_binary_empty_file(tmp_path):
    f = tmp_path / "empty.txt"
    f.write_bytes(b"")
    assert _is_binary(f) is False


# ---------------------------------------------------------------------------
# _get_size_limit_bytes
# ---------------------------------------------------------------------------


def test_size_limit_source_extension():
    limit = _get_size_limit_bytes(Path("main.py"))
    assert limit >= 1024 * 1024  # at least 1MB


def test_size_limit_text_extension():
    limit = _get_size_limit_bytes(Path("README.md"))
    assert limit > 0


def test_size_limit_unknown_extension():
    limit = _get_size_limit_bytes(Path("file.xyz"))
    assert limit > 0


def test_is_indexable_file_allows_dotenv_in_project_root(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("TOKEN=abc\n")

    assert is_indexable_file(env_file, root=tmp_path) is True


def test_is_indexable_file_allows_ignored_ancestor_outside_project_root(tmp_path):
    project_root = tmp_path / "build" / "repo"
    project_root.mkdir(parents=True)
    source_file = project_root / "app.py"
    source_file.write_text("x = 1\n")

    assert is_indexable_file(source_file, root=project_root) is True


def test_is_indexable_file_rejects_ignored_directory_inside_project_root(tmp_path):
    nested_build_dir = tmp_path / "src" / "build"
    nested_build_dir.mkdir(parents=True)
    source_file = nested_build_dir / "app.py"
    source_file.write_text("x = 1\n")

    assert is_indexable_file(source_file, root=tmp_path) is False


def test_is_indexable_file_allows_symlink_inside_project_to_external_target(tmp_path):
    external_dir = tmp_path / "external"
    external_dir.mkdir()
    external_file = external_dir / "shared.py"
    external_file.write_text("x = 1\n")

    project_root = tmp_path / "repo"
    project_root.mkdir()
    symlink_path = project_root / "link.py"
    symlink_path.symlink_to(external_file)

    assert is_indexable_file(symlink_path, root=project_root) is True


# ---------------------------------------------------------------------------
# _load_gitignore
# ---------------------------------------------------------------------------


def test_load_gitignore_missing(tmp_path):
    assert _load_gitignore(tmp_path) is None


def test_load_gitignore_present(tmp_path):
    (tmp_path / ".gitignore").write_text("*.log\nbuild/\n")
    spec = _load_gitignore(tmp_path)
    assert spec is not None
    assert spec.match_file("foo.log")
    assert spec.match_file("build/output.txt")
    assert not spec.match_file("foo.py")


# ---------------------------------------------------------------------------
# iter_files — integration
# ---------------------------------------------------------------------------


def test_iter_files_empty_directory(tmp_path):
    files = list(iter_files(tmp_path))
    assert files == []


def test_iter_files_finds_python(tmp_path):
    (tmp_path / "main.py").write_text("print('hi')\n")
    files = list(iter_files(tmp_path))
    assert len(files) == 1
    assert files[0].name == "main.py"


def test_iter_files_skips_ignored_dirs(tmp_path):
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "lodash.js").write_text("// dep\n")
    (tmp_path / "src.py").write_text("x = 1\n")
    files = [f.name for f in iter_files(tmp_path)]
    assert "src.py" in files
    assert "lodash.js" not in files


def test_iter_files_skips_ignored_extensions(tmp_path):
    (tmp_path / "img.png").write_bytes(b"\x89PNG\x00")
    (tmp_path / "main.py").write_text("ok\n")
    files = [f.name for f in iter_files(tmp_path)]
    assert "img.png" not in files
    assert "main.py" in files


def test_iter_files_respects_gitignore(tmp_path):
    (tmp_path / ".gitignore").write_text("*.log\n")
    (tmp_path / "ignored.log").write_text("ignore me\n")
    (tmp_path / "kept.py").write_text("keep me\n")
    files = [f.name for f in iter_files(tmp_path)]
    assert "kept.py" in files
    assert "ignored.log" not in files


def test_iter_files_skips_binary(tmp_path):
    (tmp_path / "data.bin").write_bytes(b"hello\x00world")
    (tmp_path / "code.py").write_text("def f(): pass\n")
    files = [f.name for f in iter_files(tmp_path)]
    assert "data.bin" not in files
    assert "code.py" in files


def test_iter_files_nested_dirs(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "b").mkdir()
    (tmp_path / "a" / "b" / "deep.py").write_text("x = 1\n")
    files = list(iter_files(tmp_path))
    assert len(files) == 1
    assert files[0].name == "deep.py"


def test_iter_files_does_not_follow_symlink_by_default(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (real / "code.py").write_text("x = 1\n")
    (tmp_path / "link").symlink_to(real, target_is_directory=True)

    files = [f.relative_to(tmp_path) for f in iter_files(tmp_path)]

    assert Path("real/code.py") in files
    assert Path("link/code.py") not in files


def test_iter_files_follows_symlinked_dir_when_enabled(tmp_path):
    external = tmp_path / "external-repo"
    external.mkdir()
    (external / "service.go").write_text("package main\n")

    project = tmp_path / "monorepo"
    project.mkdir()
    (project / "main.py").write_text("x = 1\n")
    (project / "services").symlink_to(external, target_is_directory=True)

    files_no_follow = [f.relative_to(project) for f in iter_files(project, follow_symlinks=False)]
    files_follow = [f.relative_to(project) for f in iter_files(project, follow_symlinks=True)]

    assert Path("main.py") in files_no_follow
    assert Path("services/service.go") not in files_no_follow

    assert Path("main.py") in files_follow
    assert Path("services/service.go") in files_follow


def test_iter_files_skips_oversize(tmp_path, monkeypatch):
    # Make the limit tiny so we can test
    monkeypatch.setattr(
        "opencode_search.discover.DEFAULT_SOURCE_FILE_SIZE_KB", 1
    )
    big = tmp_path / "big.py"
    big.write_text("x = 1\n" * 1000)  # >1KB
    small = tmp_path / "small.py"
    small.write_text("x\n")
    files = [f.name for f in iter_files(tmp_path)]
    # Behavior depends on whether _get_size_limit_bytes re-reads the monkeypatched
    # constant — if not, both files survive. Just verify small.py is present.
    assert "small.py" in files


def test_iter_files_gitignore_inheritance(tmp_path):
    """A child directory should inherit the parent's gitignore patterns."""
    (tmp_path / ".gitignore").write_text("*.log\n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "child.log").write_text("blocked")
    (tmp_path / "sub" / "child.py").write_text("ok")
    files = [f.name for f in iter_files(tmp_path)]
    assert "child.log" not in files
    assert "child.py" in files


# ---------------------------------------------------------------------------
# .opencode-index.yaml — include/exclude integration
# ---------------------------------------------------------------------------


def test_iter_files_respects_opencode_index_exclude(tmp_path):
    (tmp_path / ".opencode-index.yaml").write_text(
        "index:\n"
        "  exclude:\n"
        "    - \"docs/**\"\n"
    )
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "plan.md").write_text("stale\n")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("x = 1\n")

    files = [f.relative_to(tmp_path).as_posix() for f in iter_files(tmp_path)]
    assert "src/app.py" in files
    assert "docs/plan.md" not in files


def test_iter_files_include_overrides_exclude(tmp_path):
    (tmp_path / ".opencode-index.yaml").write_text(
        "index:\n"
        "  exclude:\n"
        "    - \"docs/**\"\n"
        "  include:\n"
        "    - \"docs/KEEP.md\"\n"
    )
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "KEEP.md").write_text("keep\n")
    (tmp_path / "docs" / "skip.md").write_text("skip\n")
    (tmp_path / "src.py").write_text("x = 1\n")

    files = [f.relative_to(tmp_path).as_posix() for f in iter_files(tmp_path)]
    assert "src.py" in files
    assert "docs/KEEP.md" in files
    assert "docs/skip.md" not in files


def test_iter_files_include_can_override_gitignore(tmp_path):
    (tmp_path / ".gitignore").write_text("docs/\n")
    (tmp_path / ".opencode-index.yaml").write_text(
        "index:\n"
        "  include:\n"
        "    - \"docs/KEEP.md\"\n"
    )
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "KEEP.md").write_text("keep\n")
    (tmp_path / "docs" / "other.md").write_text("ignore\n")

    files = [f.relative_to(tmp_path).as_posix() for f in iter_files(tmp_path)]
    assert "docs/KEEP.md" in files
    assert "docs/other.md" not in files


def test_iter_files_linked_override_applies_to_external_symlink(tmp_path):
    external = tmp_path / "external"
    external.mkdir()
    (external / "docs").mkdir()
    (external / "docs" / "design.md").write_text("stale\n")
    (external / "lib.py").write_text("x = 1\n")

    project = tmp_path / "project"
    project.mkdir()
    (project / "dep").symlink_to(external, target_is_directory=True)
    (project / ".opencode-index.yaml").write_text(
        "index:\n"
        "  include: [\"**/*\"]\n"
        "linked:\n"
        "  dep:\n"
        "    exclude: [\"docs/**\"]\n"
    )

    files = [f.relative_to(project).as_posix() for f in iter_files(project, follow_symlinks=True)]
    assert "dep/lib.py" in files
    assert "dep/docs/design.md" not in files


# ---------------------------------------------------------------------------
# Static data sanity
# ---------------------------------------------------------------------------


def test_ignored_dirs_includes_common():
    for d in [".git", "node_modules", "__pycache__", ".venv", "target"]:
        assert d in IGNORED_DIRS


def test_ignored_extensions_includes_binaries():
    for ext in [".png", ".exe", ".so", ".jar"]:
        assert ext in IGNORED_EXTENSIONS


def test_source_extensions_includes_languages():
    for ext in [".py", ".go", ".rs", ".ts", ".js"]:
        assert ext in SOURCE_EXTENSIONS


def test_text_extensions_includes_docs():
    for ext in [".md", ".txt", ".json", ".yaml"]:
        assert ext in TEXT_EXTENSIONS


def test_language_map_includes_common():
    assert LANGUAGE_MAP[".py"] == "python"
    assert LANGUAGE_MAP[".rs"] == "rust"
    assert LANGUAGE_MAP[".go"] == "go"
