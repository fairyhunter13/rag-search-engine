"""Public-repo hygiene guard: legacy brand tokens and absolute home paths must not reappear.

Permanent brand-lock (P18/HR34): the pre-2026-07-09 OSE/OPENCODE/ocs branding was fully retired
in favor of RSE. This guard bans every legacy self-reference token from ever reappearing in the
tracked tree, with a narrow allowlist for genuine external-product references (the external
"OpenCode" CLI product, and the retired ose-docgen package as named in dated audit records) that
must never be renamed.

Device-specific name bans (company names, project codenames, device ids) are enforced here too,
but the *mechanism* is all that ships: the token list arrives via the RSE_NAME_BAN environment
variable, never as a literal in this tree. P18/HR34 forbid shipping the ban-list itself, because
a list of the names you must not publish publishes them. This mirrors RSE_FEDERATION_EXCLUDE
exactly — mechanism in the repo, values in the environment.

Runs git grep over the tracked tree and fails on any match.
This guard file is allowlisted automatically. The repo has no submodules since docgen's deletion
(2026-07-28), so there is no .gitmodules to exempt.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.live

_REPO_ROOT = Path(__file__).parents[3]
_THIS_FILE = Path(__file__).name

# Legacy self-reference token forms (case-sensitive), retired 2026-07-09 in favor of RSE.
# Each must never reappear in the tracked tree outside the allowlists below.
_LEGACY_TOKEN_PATTERNS = [
    r"OPENCODE_",
    r"\bOSE_",
    r"\bOSE\b",
    r"\bopencode\b",
    r"\bocs[-_]",
    r"[-_]ocs\b",
    r"\bose[-_]",
    r"[-_]ose\b",
    r"opencode-index",
]

# Files exempt from the legacy-token ban entirely (this guard file + generated skills, which
# are regenerated from already-renamed sources via scripts/gen_world_model_skills.py).
_LEGACY_TOKEN_ALLOWLIST_FILES = {
    f"src/tests/live/{_THIS_FILE}",
    ".claude/skills/world-model/SKILL.md",
    ".claude/skills/info-hierarchy/SKILL.md",
}

# Substrings that legitimately contain a legacy-looking token but are genuine external-product
# references, not this project's own branding — any matching line is exempt from the ban.
_EXTERNAL_PRODUCT_ALLOWLIST_SUBSTRINGS = [
    "OpenCode",    # Title-case: the external OpenCode CLI product (integration removed, still named in prose/tests)
    # The docgen vendor package was deleted 2026-07-28. These two survive only as the package's
    # real name inside dated audit records (docs/CONFORMANCE_EVALUATION.md §5), which are kept as
    # written — renaming history to match a later decision loses the trail of why it changed.
    "ose_docgen",  # retired package import, named in dated audit records only
    "ose-docgen",  # retired repo/dir name, named in dated audit records only
]


def _git_grep_legacy(pattern: str) -> list[str]:
    result = subprocess.run(
        [
            "git", "grep", "-nE", pattern,
            "--",
            ".",
            ":(exclude)vendor",
            *[f":(exclude){f}" for f in _LEGACY_TOKEN_ALLOWLIST_FILES],
        ],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
    )
    lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
    return [
        ln for ln in lines
        if not any(allowed in ln for allowed in _EXTERNAL_PRODUCT_ALLOWLIST_SUBSTRINGS)
    ]


@pytest.mark.parametrize("pattern", _LEGACY_TOKEN_PATTERNS)
def test_no_legacy_ose_opencode_tokens_reappear(pattern: str) -> None:
    """Legacy OSE/OPENCODE/ocs brand tokens must never reappear (permanent brand lock)."""
    hits = _git_grep_legacy(pattern)
    assert not hits, (
        f"Legacy token pattern {pattern!r} found in tracked files "
        f"({len(hits)} occurrence(s)) — rename to the RSE brand:\n" + "\n".join(hits[:5])
    )


def _tracked_paths() -> list[str]:
    return subprocess.run(
        ["git", "ls-files"], cwd=_REPO_ROOT, capture_output=True, text=True
    ).stdout.splitlines()


@pytest.mark.parametrize("pattern", _LEGACY_TOKEN_PATTERNS)
def test_no_legacy_tokens_in_tracked_paths(pattern: str) -> None:
    """The brand lock must cover file *paths*, not just file contents.

    The content guard above runs `git grep`, which only ever inspects what is inside a file.
    A directory or filename carrying a banned token passed it silently — `.opencode/skill/...`
    sat in the tracked tree that way until the 2026-07-31 sweep. A path is as public as a
    line of text: it shows up in the clone, the tarball, and every GitHub URL.
    """
    rx = re.compile(pattern)
    hits = [
        p for p in _tracked_paths()
        if rx.search(p)
        and p not in _LEGACY_TOKEN_ALLOWLIST_FILES
        and not any(allowed in p for allowed in _EXTERNAL_PRODUCT_ALLOWLIST_SUBSTRINGS)
        and not p.startswith("vendor/")
    ]
    assert not hits, (
        f"Legacy token pattern {pattern!r} found in tracked PATHS "
        f"({len(hits)}) — rename or untrack:\n" + "\n".join(hits[:5])
    )


def _git_grep_re(pattern: str, exclude_file: str) -> list[str]:
    result = subprocess.run(
        [
            "git", "grep", "-nE", pattern,
            "--",
            ".",
            f":(exclude){exclude_file}",
        ],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
    )
    return [ln for ln in result.stdout.splitlines() if ln.strip()]


def test_no_absolute_home_paths() -> None:
    """Absolute /home/<user>/, /root/<path>/, /Users/<user>/, or Windows C:\\Users\\<user>\\
    paths must not appear anywhere in the tracked tree (source, tests, docs, scripts,
    generated artifacts)."""
    hits = _git_grep_re(
        r"/home/[a-zA-Z0-9_.-]+/|/root/[a-zA-Z0-9_.-]+/|/Users/[a-zA-Z0-9_.-]+/|C:\\\\Users\\\\[a-zA-Z0-9_.-]+",
        f"src/tests/live/{_THIS_FILE}",
    )
    assert not hits, (
        f"Absolute home paths found in tracked files ({len(hits)} occurrence(s)):\n"
        + "\n".join(hits[:5])
    )


def _banned_name_tokens() -> list[str]:
    """Device/company/project name tokens to ban, from RSE_NAME_BAN (os.pathsep-separated).

    Deliberately env-driven and empty by default. A fresh clone has no company names to ban, so
    an unset variable asserting nothing is the correct behaviour, not a gap — and it is not a
    skip, so test_no_skip_markers_in_live_suite stays satisfied. The private rse-live-audit repo
    owns the list and asserts the variable is actually set on a device that has one.
    """
    raw = os.environ.get("RSE_NAME_BAN", "")
    return [t.strip() for t in raw.split(os.pathsep) if t.strip()]


def test_no_banned_device_names() -> None:
    """No company/project/device name from RSE_NAME_BAN may appear in the tracked tree.

    P18 says the tracked tree carries "no company/project names", but its machine check in
    model.yaml is a *path* regex — it cannot see a name written into a comment. That gap is not
    theoretical: 48 such sites across 24 files sat in this tree under a green `[CONFORMS] P18`
    until the 2026-08-04 sweep, every one of them a provenance note written from a live
    measurement. This is the guard that makes the invariant's own claim testable.

    Matching is case-insensitive and fixed-string (-iF), so a token needs no escaping; the tokens
    are substrings, so `foo` also catches `foo-project` and `foo_ledger`.
    """
    tokens = _banned_name_tokens()
    hits: list[str] = []
    for token in tokens:
        result = subprocess.run(
            [
                "git", "grep", "-inF", token,
                "--", ".",
                ":(exclude)vendor",
                f":(exclude)src/tests/live/{_THIS_FILE}",
            ],
            cwd=_REPO_ROOT, capture_output=True, text=True,
        )
        hits += [ln for ln in result.stdout.splitlines() if ln.strip()]
    assert not hits, (
        f"Banned device/company/project name(s) found in {len(hits)} tracked line(s) — "
        f"genericize them (the fleet / the largest workspace / <repo>) and keep the numbers:\n"
        + "\n".join(hits[:10])
    )


def test_banned_name_tokens_also_absent_from_tracked_paths() -> None:
    """The name ban must cover file *paths* too, for the reason the brand lock already learned.

    `git grep` only inspects file contents; a directory or filename carrying a customer name is
    just as public, and shows up in the clone, the tarball and every GitHub URL.
    """
    tokens = [t.lower() for t in _banned_name_tokens()]
    hits = [
        p for p in _tracked_paths()
        if any(t in p.lower() for t in tokens)
        and p != f"src/tests/live/{_THIS_FILE}"
        and not p.startswith("vendor/")
    ]
    assert not hits, (
        f"Banned name(s) found in tracked PATHS ({len(hits)}) — rename or untrack:\n"
        + "\n".join(hits[:10])
    )


_PATH_LITERAL_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*\s*(?::\s*\S+)?\s*=\s*Path\((.*)\)\s*$')


def test_storage_paths_are_env_driven() -> None:
    """Module-level Path(...) storage-root constants in core/ and daemon/ must derive from
    os.environ.get(...) (XDG_DATA_HOME / RSE_*) — never a hardcoded machine-specific literal
    (P18/HR34 device-neutrality). Paths built from other already-derived names (e.g. REGISTRY_PATH)
    are allowed."""
    targets = [
        _REPO_ROOT / "src/rag_search/core/config.py",
        _REPO_ROOT / "src/rag_search/core/registry.py",
        *sorted((_REPO_ROOT / "src/rag_search/daemon").glob("*.py")),
    ]
    violations: list[str] = []
    for f in targets:
        if not f.exists():
            continue
        for lineno, line in enumerate(f.read_text().splitlines(), start=1):
            m = _PATH_LITERAL_RE.match(line.strip())
            if not m:
                continue
            inner = m.group(1)
            if "os.environ.get(" in inner:
                continue
            if re.search(r'"[^"]*/[^"]*"|\'[^\']*/[^\']*\'', inner):
                violations.append(f"{f.relative_to(_REPO_ROOT)}:{lineno}: {line.strip()}")
    assert not violations, (
        f"Hardcoded storage-root literal(s) found ({len(violations)}):\n" + "\n".join(violations[:5])
    )


# Runnable-by-anyone contract (P18/HR34): every one of these module-level config.py names is a
# machine/deployment-specific value (model, host, port, device, timeout) that a fresh clone must be
# able to override without a source edit — each must be produced by an `os.environ.get(...)` call.
_ENV_DRIVEN_CONFIG_NAMES = [
    "EMBED_MODEL", "RERANK_MODEL", "EMBED_DEVICE",
    "DAEMON_HOST", "DAEMON_PORT",
    # QUERY_LLM_PROVIDER left this list with the constant on 2026-07-31. It was the one name here
    # that no production code read, so this guard was asserting that a dead knob stayed env-driven.
    "QUERY_LLM_MODEL",
    "RSE_GPU_DEVICE",
]


def test_runtime_config_is_env_driven() -> None:
    """Model/host/port/device constants in core/config.py must derive from os.environ.get(...) —
    a fresh clone should need zero source edits to point at a different model, host, port, or GPU
    (P18/HR34 runnable-by-anyone contract)."""
    src = (_REPO_ROOT / "src/rag_search/core/config.py").read_text()
    lines = src.splitlines()
    missing: list[str] = []
    for name in _ENV_DRIVEN_CONFIG_NAMES:
        assign_lines = [ln for ln in lines if ln.strip().startswith(f"{name} ") or ln.strip().startswith(f"{name}:")]
        if not assign_lines or not any("os.environ.get(" in ln for ln in assign_lines):
            missing.append(name)
    assert not missing, (
        f"Config name(s) not env-driven via os.environ.get(...) in core/config.py: {missing}"
    )


# CLAUDE.md is loaded verbatim into every agent session, so its size is a per-turn tax paid by
# every profile. It grew 4.5 KB -> 23 KB between 2026-06-26 and 2026-07-30 because each change
# appended its own post-mortem; the 2026-07-31 trim took it to ~5.5 KB by moving narrative to
# docs/decisions/ and invariants to docs/world-model/model.yaml. This ceiling keeps it there.
_CLAUDE_MD_MAX_BYTES = 8_000


def test_the_repo_ships_the_license_its_metadata_declares() -> None:
    """A public tree must carry the license text its package metadata claims.

    Not a formality. `src/pyproject.toml` declared `license = { text = "MIT" }` from the start
    and no LICENSE file was ever tracked — `git log --all -- LICENSE` was empty — so for the
    whole life of this public repo the default applied instead: all rights reserved, nobody
    permitted to use, fork or redistribute it. Metadata is a claim about rights; only the file
    grants them, and nothing here could tell the two apart.

    Checks agreement, not a fixed license: change the declaration and the file together and this
    stays green, change either alone and it goes red. The SPDX identifier is read from the
    metadata rather than pinned, so this guard has no opinion about which license is right.
    """
    pyproject = (_REPO_ROOT / "src" / "pyproject.toml").read_text(encoding="utf-8")
    declared = re.search(r"^license\s*=\s*\{\s*text\s*=\s*\"([^\"]+)\"", pyproject, re.M)
    assert declared, (
        "src/pyproject.toml declares no license. A public repo without one grants no rights at "
        "all, whatever the README says."
    )
    name = declared.group(1)
    license_file = _REPO_ROOT / "LICENSE"
    assert license_file.is_file(), (
        f"src/pyproject.toml declares the {name} license but no LICENSE file is tracked. Until "
        f"one is, default copyright applies and this repo is not usable by anyone."
    )
    body = license_file.read_text(encoding="utf-8")
    assert name.lower() in body.lower(), (
        f"LICENSE does not name the {name} license that src/pyproject.toml declares — the "
        f"metadata and the grant disagree, and only the grant is binding."
    )
    assert re.search(r"Copyright \(c\) \d{4}", body), (
        "LICENSE carries no copyright line, which most licenses (MIT included) require to be "
        "reproduced in redistributions."
    )


def test_claude_md_stays_an_instruction_file() -> None:
    """CLAUDE.md must stay an instruction file, not drift back into a decision log."""
    size = (_REPO_ROOT / "CLAUDE.md").stat().st_size
    assert size <= _CLAUDE_MD_MAX_BYTES, (
        f"CLAUDE.md is {size} bytes (ceiling {_CLAUDE_MD_MAX_BYTES}). It loads into every "
        "session, so growth here is paid on every turn. Incident narrative and rationale belong "
        "in docs/decisions/; invariants belong in docs/world-model/model.yaml. Raise this ceiling "
        "only if the added text changes what an agent does next."
    )
