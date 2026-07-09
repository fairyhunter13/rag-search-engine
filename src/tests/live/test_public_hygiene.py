"""Public-repo hygiene guard: legacy brand tokens and absolute home paths must not reappear.

Permanent brand-lock (P18/HR34): the pre-2026-07-09 OSE/OPENCODE/ocs branding was fully retired
in favor of RSE. This guard bans every legacy self-reference token from ever reappearing in the
tracked tree, with a narrow allowlist for genuine external-product references (the external
"OpenCode" CLI product, the vendored ose-docgen package/repo) that must never be renamed.
Device-specific name bans (company names, project codenames, device ids) live in the private
rse-live-audit repo to avoid shipping the ban-list in the public tree.

Runs git grep over the tracked tree and fails on any match.
Guard file and .gitmodules are allowlisted automatically.
"""
from __future__ import annotations

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
    ".claude/skills/world-model.md",
    ".claude/skills/info-hierarchy.md",
}

# Substrings that legitimately contain a legacy-looking token but are genuine external-product
# references, not this project's own branding — any matching line is exempt from the ban.
_EXTERNAL_PRODUCT_ALLOWLIST_SUBSTRINGS = [
    "OpenCode",    # Title-case: the external OpenCode CLI product (integration removed, still named in prose/tests)
    "ose_docgen",  # vendored ose-docgen package import (vendor/docgen submodule)
    "ose-docgen",  # vendored ose-docgen repo/dir name
]


def _git_grep_legacy(pattern: str) -> list[str]:
    result = subprocess.run(
        [
            "git", "grep", "-nE", pattern,
            "--",
            ".",
            ":(exclude).gitmodules",
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


def _git_grep_re(pattern: str, exclude_file: str) -> list[str]:
    result = subprocess.run(
        [
            "git", "grep", "-nE", pattern,
            "--",
            ".",
            ":(exclude).gitmodules",
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
    "QUERY_LLM_PROVIDER", "QUERY_LLM_MODEL",
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
