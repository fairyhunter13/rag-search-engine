"""Public-repo hygiene guard: legacy brand tokens and absolute home paths must not reappear.

Permanent brand-lock (HR34): the pre-2026-07-09 OSE/OPENCODE/ocs branding was fully retired
in favor of RSE. This guard bans every legacy self-reference token from ever reappearing in the
tracked tree, with a narrow allowlist for genuine external-product references (the external
"OpenCode" CLI product, and the retired ose-docgen package as named in dated audit records) that
must never be renamed.

Device-specific name bans (company names, project codenames, device ids) are enforced here too,
but the *mechanism* is all that ships: the token list arrives via the RSE_NAME_BAN environment
variable, never as a literal in this tree. HR34 forbid shipping the ban-list itself, because
a list of the names you must not publish publishes them. This mirrors RSE_FEDERATION_EXCLUDE
exactly — mechanism in the repo, values in the environment.

That split leaves the mechanism able to pass while enforcing nothing, in two ways, and NB1-NB3
close both: NB1 requires the variable to be *declared* (a machine with nothing to ban says
`RSE_NAME_BAN=none`, which is a statement; unset is silence), and NB2/NB3 are positive controls,
because a correct ban list matches nothing and so a broken search is indistinguishable from a
clean tree.

Runs git grep over the tracked tree and fails on any match.
This guard file is allowlisted automatically. The repo has no submodules since docgen's deletion
(2026-07-28), so there is no .gitmodules to exempt.
"""
from __future__ import annotations

import hashlib
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

# Files exempt from the legacy-token ban entirely. Only this guard file, which has to spell the
# banned tokens out to ban them. The two generated skills that used to sit here left with the
# world model on 2026-08-14; nothing else has ever needed an exemption, and each one added
# narrows the guard by exactly one file.
_LEGACY_TOKEN_ALLOWLIST_FILES = {
    f"src/tests/live/{_THIS_FILE}",
}

# Substrings that legitimately contain a legacy-looking token but are genuine external-product
# references, not this project's own branding — any matching line is exempt from the ban.
_EXTERNAL_PRODUCT_ALLOWLIST_SUBSTRINGS = [
    "OpenCode",    # Title-case: the external OpenCode CLI product (integration removed, still named in prose/tests)
    # The docgen vendor package was deleted 2026-07-28; `ose_docgen` survives as the package's
    # real name in one provenance line (core/claude_profiles.py:3), kept as written — renaming
    # history to match a later decision loses the trail of why it changed. The `ose-docgen`
    # entry beside it was dropped 2026-08-14 with its last occurrence: an allowlist entry that
    # matches nothing still widens the hole it opens.
    "ose_docgen",  # retired package import, named in one provenance note
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


_NAME_BAN_VAR = "RSE_NAME_BAN"

# An operator with nothing to ban says so with this value. It is not a synonym for "unset": the
# whole failure mode here is silence, and a declaration of "none" is a statement while an absent
# variable is the absence of one.
_NAME_BAN_NONE = "none"

# No absolute home path here on purpose (HR34) — `~` and the runner's own config file read the
# same on every machine. Mirrors _REINSTALL_HINT in test_federation_exclude.py.
_NAME_BAN_HINT = (
    f"{_NAME_BAN_VAR} is not set, so the two name guards in this file assert nothing and report "
    f"green. Set it (os.pathsep-separated substrings, case-insensitive) where every runner of the "
    f"live suite will see it — for this project that is ~/.config/environment.d/ for the user "
    f"session AND the CI runner's own .env, which is a separate scope and does not read the "
    f"former. A machine with no company/project/device names to ban declares that explicitly with "
    f"{_NAME_BAN_VAR}={_NAME_BAN_NONE}; the list itself must never enter this public tree."
)


def _banned_name_tokens() -> list[str]:
    """Device/company/project name tokens to ban, from RSE_NAME_BAN (os.pathsep-separated).

    Env-driven because the list itself must never enter this public tree (HR34) — a list of
    the names you must not publish publishes them. The private rse-live-audit repo owns the values
    and asserts this device's list is complete.

    `none` parses to zero tokens, and so does an unset variable — but only the first is a legal
    state. See NB1: "no names to ban" is a declaration an operator makes, not the default they
    fall into by doing nothing.
    """
    raw = os.environ.get(_NAME_BAN_VAR, "")
    if raw.strip().lower() == _NAME_BAN_NONE:
        return []
    return [t.strip() for t in raw.split(os.pathsep) if t.strip()]


def _grep_tracked_fixed(token: str) -> list[str]:
    """Tracked lines containing `token`, case-insensitive fixed-string (so no escaping needed)."""
    result = subprocess.run(
        [
            "git", "grep", "-inF", token,
            "--", ".",
            ":(exclude)vendor",
            f":(exclude)src/tests/live/{_THIS_FILE}",
        ],
        cwd=_REPO_ROOT, capture_output=True, text=True,
    )
    return [ln for ln in result.stdout.splitlines() if ln.strip()]


def _tracked_paths_containing(token: str) -> list[str]:
    """Tracked paths containing `token` as a case-insensitive substring."""
    lowered = token.lower()
    return [
        p for p in _tracked_paths()
        if lowered in p.lower()
        and p != f"src/tests/live/{_THIS_FILE}"
        and not p.startswith("vendor/")
    ]


def _path_hash(path: str) -> str:
    """Non-reversible id for a path, so a failure can be located without naming it.

    Same device as index/bounded_parse.py's `_path_hash` and for the same reason (HR34): the
    identifier goes somewhere public, the path does not.
    """
    return hashlib.sha256(path.encode()).hexdigest()[:12]


def _redacting() -> bool:
    """True when this run's output is published — GitHub Actions logs on a public repo are world
    readable, permanently, and are not reached by any history rewrite."""
    return bool(os.environ.get("GITHUB_ACTIONS"))


def _safe_content_hits(hits: list[str]) -> str:
    """Render `git grep` hits for an assertion message that may be read in public.

    Locally the hits print in full — that is the whole value of the message, and the terminal is
    the operator's. Under CI they collapse to `<path-hash>:<lineno>`: the guard exists to keep
    these names off the internet, and printing them into a public build log on the one event it is
    designed to catch would publish exactly the list RSE_NAME_BAN is kept out of the tree to
    protect. The assertion fails identically either way; only the evidence is withheld.
    """
    if not _redacting():
        return "\n".join(hits[:10])
    located = []
    for ln in hits[:10]:
        path, _, rest = ln.partition(":")
        lineno, _, _content = rest.partition(":")
        located.append(f"  {_path_hash(path)}:{lineno or '?'}")
    return (
        "(redacted — this run's log is public; re-run locally for the lines)\n" + "\n".join(located)
    )


def _safe_path_hits(hits: list[str]) -> str:
    """As _safe_content_hits, for the arm whose hits *are* paths — the path is the leak here."""
    if not _redacting():
        return "\n".join(hits[:10])
    return (
        "(redacted — this run's log is public; re-run locally for the paths)\n"
        + "\n".join(f"  {_path_hash(p)}" for p in hits[:10])
    )


def test_nb1_name_ban_variable_is_declared() -> None:
    """NB1: RSE_NAME_BAN must be *present* in the environment running this suite.

    The two guards below are the only enforcement HR34's name facet has (the retired register's `check` was
    a path regex and cannot see a name in a comment). With the variable unset they iterate an
    empty token list and pass — which is exactly how the 48 name sites cleared on 2026-08-04 lived
    for months under a green `[CONFORMS]`. A guard that stands down when its input is missing
    reports the same green either way, so the input's absence is what has to go red.

    This is the FE8 shape from test_federation_exclude.py, minus its device-coupling: no daemon,
    no /proc, no host path, and no token value is asserted here — only that a declaration exists.
    """
    assert _NAME_BAN_VAR in os.environ and os.environ[_NAME_BAN_VAR].strip(), _NAME_BAN_HINT


def test_nb2_content_guard_actually_matches() -> None:
    """NB2: positive control for _grep_tracked_fixed — a correct ban list matches nothing.

    Green therefore proves nothing about the machinery: a bad flag, a renamed helper or a pathspec
    typo produces the same empty result as a clean tree. FE11 one file over exists for this reason
    ("FE8-FE10 would ALL stay green with the drop-in deleted"). So assert the search finds a token
    that is certainly present, using the same helper the ban runs through.
    """
    hits = _grep_tracked_fixed("RAG_search")  # mixed case on purpose: -i must be in force
    assert hits, (
        "the fixed-string tracked-tree search found no hit for a token that is certainly present "
        "and certainly not case-matched — the name ban is searching nothing, and would pass on a "
        "tree full of banned names"
    )


def test_nb3_path_guard_actually_matches() -> None:
    """NB3: positive control for _tracked_paths_containing, for NB2's reason on the path arm."""
    hits = _tracked_paths_containing("CONFtest")  # mixed case on purpose
    assert hits, (
        "the tracked-path search found no hit for a filename that is certainly present — the "
        "path arm of the name ban is inspecting nothing"
    )


def test_nb4_failure_output_is_redacted_under_public_ci() -> None:
    """NB4: the ban's own failure message must not publish the names, and does not, only when
    something checks — this is the arm NB2/NB3 are to the search itself.

    Measured 2026-08-05: the live-fast job runs this file on every push, and its log is public and
    permanent (GitHub's own guidance is that a history rewrite does not reach build logs or cached
    views — those need Support). The same run's log already carries the maintainer's home path via
    pytest's warning summary, which is how the surface was found.
    """
    sample = ["some/private/path.py:42:a line containing a banned name"]

    def _render(*, publishing: bool) -> tuple[str, str]:
        """Render both arms with GITHUB_ACTIONS forced on or off.

        Forcing it *off* matters as much as forcing it on: under real CI the variable is already
        set, so a version of this test that only set it saw redacted output for both arms and
        failed on the local-mode assertion — in CI only, which is the one place it had no local
        run to catch it.
        """
        orig = os.environ.get("GITHUB_ACTIONS")
        if publishing:
            os.environ["GITHUB_ACTIONS"] = "true"
        else:
            os.environ.pop("GITHUB_ACTIONS", None)
        try:
            return _safe_content_hits(sample), _safe_path_hits(["some/private/path.py"])
        finally:
            if orig is None:
                os.environ.pop("GITHUB_ACTIONS", None)
            else:
                os.environ["GITHUB_ACTIONS"] = orig

    content, paths = _render(publishing=True)

    for rendered in (content, paths):
        assert "private" not in rendered and "banned name" not in rendered, (
            f"redacted output still carries the evidence it is redacting: {rendered!r}"
        )
    assert _path_hash("some/private/path.py")[:8] in content, (
        "redacted output must still locate the hit — an unlocatable failure is a failure nobody "
        "can act on, which is how a guard gets disabled instead of fixed"
    )
    assert ":42" in content, "the line number is not sensitive and must survive redaction"
    # And the local path stays legible: redaction that also fires locally would make every real
    # fix a two-step hash lookup, and the operator's own terminal is not a publication.
    local_content, local_paths = _render(publishing=False)
    assert "a line containing a banned name" in local_content
    assert "some/private/path.py" in local_paths


def test_no_banned_device_names() -> None:
    """No company/project/device name from RSE_NAME_BAN may appear in the tracked tree.

    HR34 says the tracked tree carries "no company/project names", but the retired register's check in
    model.yaml is a *path* regex — it cannot see a name written into a comment. That gap is not
    theoretical: 48 such sites across 24 files sat in this tree under a green `[CONFORMS]`
    until the 2026-08-04 sweep, every one of them a provenance note written from a live
    measurement. This is the guard that makes the invariant's own claim testable.

    Matching is case-insensitive and fixed-string (-iF), so a token needs no escaping; the tokens
    are substrings, so `foo` also catches `foo-project` and `foo_ledger`.
    """
    hits: list[str] = []
    for token in _banned_name_tokens():
        hits += _grep_tracked_fixed(token)
    assert not hits, (
        f"Banned device/company/project name(s) found in {len(hits)} tracked line(s) — "
        f"genericize them (the fleet / the largest workspace / <repo>) and keep the numbers:\n"
        + _safe_content_hits(hits)
    )


def test_banned_name_tokens_also_absent_from_tracked_paths() -> None:
    """The name ban must cover file *paths* too, for the reason the brand lock already learned.

    `git grep` only inspects file contents; a directory or filename carrying a customer name is
    just as public, and shows up in the clone, the tarball and every GitHub URL.
    """
    hits: list[str] = []
    for token in _banned_name_tokens():
        hits += _tracked_paths_containing(token)
    assert not hits, (
        f"Banned name(s) found in tracked PATHS ({len(hits)}) — rename or untrack:\n"
        + _safe_path_hits(hits)
    )


_PATH_LITERAL_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*\s*(?::\s*\S+)?\s*=\s*Path\((.*)\)\s*$')


def test_storage_paths_are_env_driven() -> None:
    """Module-level Path(...) storage-root constants in core/ and daemon/ must derive from
    os.environ.get(...) (XDG_DATA_HOME / RSE_*) — never a hardcoded machine-specific literal
    (HR34 device-neutrality). Paths built from other already-derived names (e.g. REGISTRY_PATH)
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


# Runnable-by-anyone contract (HR34): every one of these module-level config.py names is a
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
    (HR34 runnable-by-anyone contract)."""
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
# docs/decisions/ and invariants to the architecture spec. This ceiling keeps it there.
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
        "in docs/decisions/; invariants belong in docs/architecture/. Raise this ceiling "
        "only if the added text changes what an agent does next."
    )
