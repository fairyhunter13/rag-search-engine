"""Which files a project offers the indexer, and what they contain.

Gitignore is delegated to git. `git ls-files --cached --others
--exclude-standard` returns exactly the tracked and not-ignored-untracked
files, honouring the whole chain -- repo `.gitignore`, nested ones, `.git/info/
exclude`, and the user's global excludesfile -- including the negation rules
that hand-rolled matchers get wrong. Reimplementing that is a few hundred lines
whose failure mode is indexing a `node_modules` nobody asked for.

`ls-files` lists a gitlink as one entry and never descends into it, so a
populated submodule contributed nothing. `--recurse-submodules` is not the fix:
git refuses it beside `--others`. So the command runs once per gitlink instead.
"""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import config, filters, ignores
from .projcfg import ProjectConfig


@dataclass(slots=True)
class FileMeta:
    rel: str
    mtime: float
    size: int
    sha256: str
    lang: str
    n_lines: int
    text: str


GITLINK_MODE = "160000"
MAX_SUBMODULE_DEPTH = 4


def _gitlinks(project: Path) -> list[str]:
    """Relative paths of the submodule entries in this index.

    Read from the index and not from `.gitmodules`, because `.gitmodules`
    declares a submodule the tree may never have checked out.
    """
    try:
        out = subprocess.run(
            ["git", "ls-files", "--stage", "-z"],
            cwd=project,
            capture_output=True,
            timeout=120,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    names: list[str] = []
    for entry in out.stdout.decode("utf-8", "replace").split("\0"):
        if not entry.startswith(GITLINK_MODE):
            continue
        _, _, name = entry.partition("\t")
        if name:
            names.append(name)
    return names


def _git_files(
    project: Path, *, depth: int = 0, seen: set[str] | None = None
) -> list[str] | None:
    """Relative paths from git, or None when this is not a git work tree.

    A populated submodule is enumerated by the same command run inside it, and
    its paths are prefixed back to the outer project, so every caller still
    receives one flat list relative to that project and nothing else changes.
    """
    try:
        out = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=project,
            capture_output=True,
            timeout=120,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    found = [p for p in out.stdout.decode("utf-8", "replace").split("\0") if p]
    if depth >= MAX_SUBMODULE_DEPTH:
        return found
    # A visited-realpath set, shared across the whole recursion: a link back to
    # an ancestor and two links to one target are both enumerated once.
    if seen is None:
        seen = set()
    seen.add(str(project.resolve()))
    for name in _gitlinks(project):
        sub = project / name
        if sub.is_symlink() or not sub.is_dir():
            continue
        real = str(sub.resolve())
        if real in seen:
            continue
        try:
            if not any(sub.iterdir()):
                continue
        except OSError:
            continue
        inner = _git_files(sub, depth=depth + 1, seen=seen)
        if inner is None:
            inner = _walked_files(sub)
        found.extend(f"{name}/{rel}" for rel in inner)
    return found


def git_ignored(project: Path, rels: list[str]) -> set[str]:
    """Which of these git excludes, for a caller that has no `ls-files` to filter.

    `candidates` gets this free from `--exclude-standard`; the watcher does not,
    and the gap is a build cache that no pass will ever index waking the indexer
    on every write to it.
    """
    if not rels:
        return set()
    try:
        out = subprocess.run(
            ["git", "check-ignore", "--stdin", "-z"],
            cwd=project,
            input="\0".join(rels).encode(),
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    return {p for p in out.stdout.decode("utf-8", "replace").split("\0") if p}


def _walked_files(project: Path) -> list[str]:
    """Fallback for a directory git does not manage.

    Prunes ignored directories during the walk rather than filtering after: on
    a tree with `node_modules`, descending first and discarding later is the
    difference between a second and a minute.
    """
    found: list[str] = []
    stack = [project]
    while stack:
        here = stack.pop()
        try:
            entries = list(here.iterdir())
        except OSError:
            continue
        for entry in entries:
            rel = str(entry.relative_to(project))
            if entry.is_symlink():
                continue  # members are registered and walked under their own path
            if entry.is_dir():
                if entry.name not in ignores.IGNORE_DIRS and not filters.matches_any(
                    rel, ignores.DEFAULT_IGNORES
                ):
                    stack.append(entry)
            else:
                found.append(rel)
    return found


def indexable(rel: str, cfg: ProjectConfig) -> bool:
    """Every filter that needs no disk read, ordered by cost.

    Shared with the watcher rather than restated there: a watcher that decides
    differently from the indexer wakes it for files it will then refuse, and the
    two copies drift on the first pattern anyone adds.
    """
    if cfg.use_default_ignores and (
        filters.in_ignored_dir(rel)
        or filters.is_ignored_name(rel)
        or filters.matches_any(rel, ignores.DEFAULT_IGNORES)
    ):
        return False
    if filters.matches_any(rel, cfg.exclude) and not filters.matches_any(rel, cfg.include):
        return False
    if filters.is_secret_path(rel) or filters.is_image_path(rel):
        return False
    return not filters.is_binary_ext(rel)


def candidates(project: Path | str, cfg: ProjectConfig) -> list[str]:
    """Relative paths worth opening, after every filter that needs no read.

    Order of the checks is by cost: the pattern lists are strings, the secret
    and extension tests are string tests, and only `stat` touches the disk.
    """
    project = Path(project)
    if filters.is_forbidden_root(project):
        raise ValueError(f"refusing to index {project}: not a project directory")

    paths = _git_files(project) if cfg.respect_gitignore else None
    if paths is None:
        paths = _walked_files(project)

    kept: list[str] = []
    for rel in paths:
        if not indexable(rel, cfg):
            continue
        full = project / rel
        try:
            if full.is_symlink():
                # `git ls-files` lists a committed symlink as an ordinary path
                # and `is_file()` follows it, so `notes.md -> ~/private/notes.md`
                # was read and attributed to this project. The walk lane has
                # always refused one; the lanes have to agree.
                continue
            if not full.is_file() or full.stat().st_size > config.MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        kept.append(rel)
    return sorted(kept)


def read(project: Path | str, rel: str) -> FileMeta | None:
    """Content plus its hash, or None when the file is not indexable text.

    Returns None rather than raising for every ordinary reason a file goes
    away mid-walk. A reconcile over 61,714 files across 142 repos races the
    user's own editor, git checkouts and build output constantly; a walk that
    aborts on the first vanished file never finishes.
    """
    full = Path(project) / rel
    try:
        if full.is_symlink():
            return None
        stat = full.stat()
        raw = full.read_bytes()
    except (OSError, ValueError):
        return None

    if filters.looks_binary(raw):
        return None
    text = raw.decode("utf-8", "replace")
    if not text.strip():
        return None

    return FileMeta(
        rel=rel,
        mtime=stat.st_mtime,
        size=stat.st_size,
        sha256=hashlib.sha256(raw).hexdigest(),
        lang=filters.lang_of(rel),
        n_lines=text.count("\n") + 1,
        text=text,
    )


def changed(
    project: Path | str, cfg: ProjectConfig, known: dict[str, str]
) -> tuple[list, list[str]]:
    """The content-hash diff: (files to write, paths to delete).

    This one idempotent comparison replaces the old engine's ten staleness
    predicates and its reconcile state machine. It is correct after a crash,
    after a missed inotify event, and after the daemon was down for a week,
    because it asks the only question that matters -- does the store match the
    disk -- rather than trying to track how they diverged.
    """
    present = candidates(project, cfg)
    write, seen = [], set()

    for rel in present:
        meta = read(project, rel)
        if meta is None:
            continue
        seen.add(rel)
        if known.get(rel) != meta.sha256:
            write.append(meta)

    return write, sorted(set(known) - seen)
