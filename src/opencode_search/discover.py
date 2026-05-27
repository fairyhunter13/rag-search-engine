"""File discovery with gitignore-aware traversal."""

import logging
import os
from collections.abc import Iterator
from pathlib import Path

import pathspec

from opencode_search.config import (
    DEFAULT_SOURCE_FILE_SIZE_KB,
    DEFAULT_TEXT_FILE_SIZE_KB,
    DEFAULT_UNKNOWN_FILE_SIZE_KB,
)
from opencode_search.index_config import (
    ProjectConfig,
    effective_index_config,
    load_project_config,
    matches_any_pattern,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Extension / directory ignore lists
# ---------------------------------------------------------------------------

IGNORED_DIRS: frozenset[str] = frozenset(
    [
        ".git", ".svn", ".hg", "node_modules", "target", "__pycache__",
        ".pytest_cache", ".mypy_cache", ".ruff_cache", "vendor", "dist",
        "build", ".build", "out", ".next", ".nuxt", ".cache", "coverage",
        ".coverage", ".tox", "venv", ".venv", "env", ".env", "bower_components",
        ".cargo", "pkg", ".idea", ".vscode", "Pods", "DerivedData",
        # opencode-search's own per-project index directory — never index it
        ".opencode",
    ]
)

IGNORED_EXTENSIONS: frozenset[str] = frozenset(
    [
        ".o", ".a", ".so", ".dylib", ".dll", ".exe", ".bin", ".obj",
        ".pyc", ".pyo", ".class", ".jar", ".war", ".ear",
        ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar",
        ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp",
        ".mp3", ".mp4", ".wav", ".avi", ".mov",
        ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
        ".lock",   # package lock files (too noisy, rarely useful for search)
        ".sum",    # go.sum
    ]
)

SOURCE_EXTENSIONS: frozenset[str] = frozenset(
    [
        ".go", ".rs", ".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".kt",
        ".vue", ".svelte", ".astro",
        ".swift", ".c", ".cpp", ".cc", ".h", ".hpp", ".cs", ".rb", ".php",
        ".scala", ".clj", ".ex", ".exs", ".hs", ".ml", ".mli", ".fs", ".fsx",
        ".lua", ".r", ".jl", ".nim", ".zig", ".v", ".elm", ".dart",
        ".sh", ".bash", ".zsh", ".fish", ".ps1",
        ".sql", ".graphql", ".proto",
    ]
)

TEXT_EXTENSIONS: frozenset[str] = frozenset(
    [
        ".md", ".mdx", ".markdown", ".txt", ".rst", ".adoc",
        ".json", ".jsonc", ".json5", ".jsonl", ".yaml", ".yml", ".toml", ".xml", ".html", ".htm",
        ".css", ".scss", ".sass", ".less",
        ".env", ".cfg", ".conf", ".ini", ".properties",
        ".dockerfile", "Dockerfile", ".makefile", "Makefile",
        ".gitignore", ".gitattributes", ".editorconfig",
    ]
)

LANGUAGE_MAP: dict[str, str] = {
    ".go": "go",
    ".rs": "rust",
    ".py": "python",
    ".js": "javascript",
    ".ts": "typescript",
    ".jsx": "jsx",
    ".tsx": "tsx",
    ".vue": "vue",
    ".svelte": "svelte",
    ".astro": "astro",
    ".java": "java",
    ".kt": "kotlin",
    ".swift": "swift",
    ".c": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".h": "c",
    ".hpp": "cpp",
    ".cs": "csharp",
    ".rb": "ruby",
    ".php": "php",
    ".scala": "scala",
    ".clj": "clojure",
    ".ex": "elixir",
    ".exs": "elixir",
    ".hs": "haskell",
    ".ml": "ocaml",
    ".mli": "ocaml",
    ".fs": "fsharp",
    ".fsx": "fsharp",
    ".lua": "lua",
    ".r": "r",
    ".jl": "julia",
    ".nim": "nim",
    ".zig": "zig",
    ".v": "v",
    ".elm": "elm",
    ".dart": "dart",
    ".sh": "bash",
    ".bash": "bash",
    ".zsh": "zsh",
    ".fish": "fish",
    ".ps1": "powershell",
    ".sql": "sql",
    ".graphql": "graphql",
    ".proto": "protobuf",
    ".md": "markdown",
    ".mdx": "markdown",
    ".rst": "restructuredtext",
    ".json": "json",
    ".jsonc": "json",
    ".json5": "json",
    ".jsonl": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".xml": "xml",
    ".html": "html",
    ".htm": "html",
    ".css": "css",
    ".scss": "scss",
    "Dockerfile": "dockerfile",
    "dockerfile": "dockerfile",
    "Makefile": "makefile",
    "makefile": "makefile",
    "GNUmakefile": "makefile",
    "gnumakefile": "makefile",
    "CMakeLists.txt": "cmake",
    "cmakelists.txt": "cmake",
}

# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def detect_language(path: Path) -> str:
    """Return a language identifier for *path* based on file extension.

    Falls back to "text" for recognised text extensions and "unknown" otherwise.
    """
    suffix = path.suffix.lower()
    if suffix in LANGUAGE_MAP:
        return LANGUAGE_MAP[suffix]
    # Also try exact filename match (e.g. "Makefile", "Dockerfile").
    name = path.name
    if name in LANGUAGE_MAP:
        return LANGUAGE_MAP[name]
    if suffix in TEXT_EXTENSIONS or name in TEXT_EXTENSIONS:
        return "text"
    return "unknown"


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _load_gitignore(directory: Path) -> pathspec.PathSpec | None:
    """Read the .gitignore in *directory* and return a PathSpec, or None."""
    gi_path = directory / ".gitignore"
    if not gi_path.is_file():
        return None
    try:
        with gi_path.open("r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
        return pathspec.PathSpec.from_lines("gitignore", lines)
    except OSError as exc:
        logger.debug("Cannot read %s: %s", gi_path, exc)
        return None


def _is_binary(path: Path) -> bool:
    """Return True if *path* appears to be a binary file.

    Reads up to 8192 bytes and checks for null bytes, which are a reliable
    indicator of binary content in most code / text files.
    """
    try:
        with path.open("rb") as fh:
            chunk = fh.read(8192)
        return b"\x00" in chunk
    except OSError:
        # Unreadable file — treat as binary so we skip it.
        return True


def _get_size_limit_bytes(path: Path) -> int:
    """Return the maximum allowed file size in bytes for *path*."""
    suffix = path.suffix.lower()
    if suffix in SOURCE_EXTENSIONS:
        return DEFAULT_SOURCE_FILE_SIZE_KB * 1024
    if suffix in TEXT_EXTENSIONS or path.name in TEXT_EXTENSIONS:
        return DEFAULT_TEXT_FILE_SIZE_KB * 1024
    return DEFAULT_UNKNOWN_FILE_SIZE_KB * 1024


def is_indexable_file(path: Path, root: Path | None = None) -> bool:
    """Return True if *path* is eligible for indexing.

    This mirrors the main discovery filters so watch-based incremental indexing
    does not ingest ignored directories or the project's own `.opencode` data.
    When *root* is provided, directory-ignore checks are scoped to directories inside
    that watched project root rather than ancestors elsewhere on the filesystem.
    """
    return is_indexable_file_with_config(path, root=root, project_config=None)


def is_indexable_file_with_config(
    path: Path,
    root: Path | None = None,
    *,
    project_config: ProjectConfig | None = None,
) -> bool:
    """Like is_indexable_file(), but applies `.opencode-index.(yml|yaml)` when present.

    The config only controls which files are indexed. It must not introduce any
    query rewriting or keyword-based behavior.
    """
    try:
        candidate_path = path.expanduser()
    except OSError:
        return False

    if root is not None and not candidate_path.is_absolute():
        candidate_path = root / candidate_path

    if not candidate_path.is_file():
        return False

    root_resolved: Path | None = None
    if root is not None:
        try:
            root_resolved = root.expanduser().resolve()
        except OSError:
            return False

    if root is not None:
        try:
            relative_parts = candidate_path.relative_to(root_resolved).parts if root_resolved else ()
        except (OSError, ValueError):
            return False
        ignored_dir_parts = relative_parts[:-1]
    else:
        ignored_dir_parts = candidate_path.resolve().parts[:-1]

    # Apply project-level include/exclude config (if any).
    if root_resolved is not None:
        try:
            project_cfg = project_config or load_project_config(root_resolved)
        except Exception:
            project_cfg = ProjectConfig()

        linked_cfg: ProjectConfig | None = None
        linked_name: str | None = None
        if relative_parts:
            linked_name = relative_parts[0]
            try:
                top = root_resolved / linked_name
                # Mirror watcher behavior: only treat top-level symlinks that
                # point *outside* the root as linked project boundaries.
                if top.is_symlink() and top.is_dir():
                    real_target = top.resolve()
                    if str(real_target) != str(root_resolved) and not str(real_target).startswith(
                        str(root_resolved) + os.sep
                    ):
                        linked_cfg = load_project_config(real_target)
                    else:
                        linked_name = None
            except Exception:
                linked_name = None
                linked_cfg = None

        index_cfg = effective_index_config(project_cfg, linked_name=linked_name, linked=linked_cfg)
        match_root = root_resolved if linked_name is None else (root_resolved / linked_name)

        if index_cfg.use_default_ignores:
            if any(part in IGNORED_DIRS for part in ignored_dir_parts):
                return False
            if candidate_path.suffix.lower() in IGNORED_EXTENSIONS:
                return False
        else:
            # Always ignore the search engine's internal state and VCS metadata.
            always_ignored = {".opencode", ".git", ".hg", ".svn"}
            if any(part in always_ignored for part in ignored_dir_parts):
                return False

        # Config include/exclude patterns are evaluated after the coarse
        # directory pruning above.
        if index_cfg.exclude and matches_any_pattern(candidate_path, index_cfg.exclude, match_root) and not (index_cfg.include and matches_any_pattern(candidate_path, index_cfg.include, match_root)):
            return False
    else:
        if any(part in IGNORED_DIRS for part in ignored_dir_parts):
            return False
        if candidate_path.suffix.lower() in IGNORED_EXTENSIONS:
            return False

    try:
        if candidate_path.stat().st_size > _get_size_limit_bytes(candidate_path):
            return False
    except OSError:
        return False

    return not _is_binary(candidate_path)


# ---------------------------------------------------------------------------
# Main iterator
# ---------------------------------------------------------------------------


def iter_files(root: Path, follow_symlinks: bool = False) -> Iterator[Path]:
    """Walk *root* recursively, yielding eligible source / text files.

    Applies:
    - IGNORED_DIRS pruning
    - Per-directory .gitignore stacking (root spec is inherited by children)
    - IGNORED_EXTENSIONS filtering
    - File-size limits (category-based)
    - Binary-content detection
    """
    root = root.resolve()

    project_cfg = load_project_config(root)
    linked_cfgs: dict[str, ProjectConfig] = {}
    try:
        for child in root.iterdir():
            if not child.is_symlink() or not child.is_dir():
                continue
            try:
                real_target = child.resolve()
            except OSError:
                continue
            # Only treat external targets as linked boundaries (mirror watcher).
            if str(real_target) != str(root) and not str(real_target).startswith(str(root) + os.sep):
                linked_cfgs[child.name] = load_project_config(real_target)
    except OSError:
        linked_cfgs = {}

    # Stack of (directory, gitignore_spec | None) pairs.  We build an
    # accumulated spec per directory by combining ancestor specs.
    # Each entry on the walk stack: (dir_path, accumulated_pathspec_lines)
    # We use os.walk for efficiency and to control symlink following.

    # Seed the root-level spec.
    root_spec = _load_gitignore(root)

    # We track accumulated specs as a dict: abs_dir -> PathSpec | None
    # to avoid re-computing for every child.
    _spec_cache: dict[Path, pathspec.PathSpec | None] = {root: root_spec}

    def _get_spec(directory: Path) -> pathspec.PathSpec | None:
        """Return the accumulated PathSpec for *directory* (cached)."""
        if directory in _spec_cache:
            return _spec_cache[directory]

        parent_spec = _get_spec(directory.parent)
        local_spec = _load_gitignore(directory)

        if parent_spec is None and local_spec is None:
            combined = None
        elif parent_spec is None:
            combined = local_spec
        elif local_spec is None:
            combined = parent_spec
        else:
            # Merge by combining all patterns.
            combined_patterns = list(parent_spec.patterns) + list(local_spec.patterns)
            combined = pathspec.PathSpec(combined_patterns)

        _spec_cache[directory] = combined
        return combined

    for dirpath, dirnames, filenames in os.walk(
        root, followlinks=follow_symlinks, topdown=True
    ):
        current_dir = Path(dirpath)

        # Prune ignored directory names in-place (modifies os.walk).
        # If `use_default_ignores` is disabled, keep most directories; still
        # prune internal state and VCS metadata to avoid self-indexing loops.
        if project_cfg.index.use_default_ignores:
            dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS]
        else:
            dirnames[:] = [d for d in dirnames if d not in {".opencode", ".git", ".hg", ".svn"}]

        spec = _get_spec(current_dir)

        for filename in filenames:
            file_path = current_dir / filename

            # 2. Gitignore check (relative to root).
            if spec is not None:
                try:
                    rel = file_path.relative_to(root)
                    if spec.match_file(str(rel)):
                        # Allow config include patterns to override gitignore.
                        rel_parts = rel.parts
                        linked_name = rel_parts[0] if rel_parts else None
                        linked = linked_cfgs.get(linked_name or "")
                        if linked is None:
                            linked_name = None
                        index_cfg = effective_index_config(project_cfg, linked_name=linked_name, linked=linked)
                        match_root = root if linked_name is None else (root / linked_name)
                        if not (index_cfg.include and matches_any_pattern(file_path, index_cfg.include, match_root)):
                            continue
                except ValueError:
                    pass  # Outside root — should not happen but be safe.

            # 2.5. Project-level include/exclude patterns.
            try:
                rel = file_path.relative_to(root)
                rel_parts = rel.parts
            except ValueError:
                rel_parts = ()
            linked_name = rel_parts[0] if rel_parts else None
            linked = linked_cfgs.get(linked_name or "")
            if linked is None:
                linked_name = None
            index_cfg = effective_index_config(project_cfg, linked_name=linked_name, linked=linked)
            match_root = root if linked_name is None else (root / linked_name)

            if index_cfg.use_default_ignores:
                # 1. Ignored extension check.
                suffix = file_path.suffix.lower()
                if suffix in IGNORED_EXTENSIONS:
                    continue

            if index_cfg.exclude and matches_any_pattern(file_path, index_cfg.exclude, match_root) and not (index_cfg.include and matches_any_pattern(file_path, index_cfg.include, match_root)):
                continue

            # 3. Size limit.
            try:
                size = file_path.stat().st_size
            except OSError:
                continue
            limit = _get_size_limit_bytes(file_path)
            if size > limit:
                logger.debug(
                    "Skipping oversized file (%d > %d bytes): %s",
                    size,
                    limit,
                    file_path,
                )
                continue

            # 4. Binary detection.
            if _is_binary(file_path):
                continue

            yield file_path
