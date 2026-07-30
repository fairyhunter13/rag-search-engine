"""FE — RSE_FEDERATION_EXCLUDE integration tests.

FE1 — federation_exclude_paths() is empty by default.
FE2 — discover_members() includes a symlinked external repo when not excluded.
FE3 — discover_members() skips a symlinked external repo when its resolved path is excluded.
FE4 — federation_exclude_paths() handles ~ expansion, multiple paths, and blank entries.
FE5 — is_federation_excluded() excludes by prefix (subtree, not just exact path).
FE6 — discover_members() skips a member when matched by a glob entry.
FE7 — is_federation_excluded() unit-table: empty / exact / child-of-prefix / glob / non-match.

FE8-FE10 gate the *running daemon*, not a synthetic env. FE1-FE7 all set
RSE_FEDERATION_EXCLUDE themselves, so every one of them stayed green while the only copy of
the real value on this machine lived in an unversioned ~/.config drop-in: deleting it went
unnoticed by the suite, and `register_all_members()` runs synchronously at every daemon start
(daemon/server.py) re-enabling whatever discovery finds. Losing the value therefore restores
541,718 duplicate worktree chunks on a one-core CPUQuota with nothing going red.

FE8  — the daemon's OWN environment carries a non-empty RSE_FEDERATION_EXCLUDE.
FE9  — no enabled registry row is excluded under that value (discovery/exclusion agree).
FE10 — every armed disabled row (indexed_at is None, path still on disk) IS excluded by it,
       so no row is one member-discovery pass away from being re-enabled and re-indexed.
"""
from __future__ import annotations

import os
import subprocess
from contextlib import contextmanager
from pathlib import Path

import pytest

pytestmark = pytest.mark.live

_UNIT = "rag-search-mcp-daemon.service"
_VAR = "RSE_FEDERATION_EXCLUDE"


def _make_ext_repo(tmp_base: Path, root: Path, name: str) -> Path:
    """Create a minimal external repo (has a .go file) OUTSIDE root, symlink it into root."""
    ext = tmp_base / ("_ext_" + name)
    ext.mkdir()
    (ext / "main.go").write_text("package main\n")
    (root / name).symlink_to(ext)
    return ext.resolve()


def test_fe1_exclude_paths_empty_by_default():
    """FE1: no env var → empty frozenset."""
    from rag_search.core.config import federation_exclude_paths
    orig = os.environ.pop("RSE_FEDERATION_EXCLUDE", None)
    try:
        assert federation_exclude_paths() == frozenset()
    finally:
        if orig is not None:
            os.environ["RSE_FEDERATION_EXCLUDE"] = orig


def test_fe2_discover_members_includes_symlinked_repo(tmp_path):
    """FE2: without exclusion, discover_members returns the external symlinked repo."""
    from rag_search.daemon.federation import discover_members
    root = tmp_path / "fed-root"
    root.mkdir()
    ext_path = _make_ext_repo(tmp_path, root, "ext-service")

    orig = os.environ.pop("RSE_FEDERATION_EXCLUDE", None)
    try:
        members = discover_members(str(root))
    finally:
        if orig is not None:
            os.environ["RSE_FEDERATION_EXCLUDE"] = orig

    assert str(ext_path) in members, f"ext-service must be discovered; got {members}"


def test_fe3_discover_members_skips_excluded_repo(tmp_path):
    """FE3: repo in RSE_FEDERATION_EXCLUDE is not returned by discover_members."""
    from rag_search.daemon.federation import discover_members
    root = tmp_path / "fed-root"
    root.mkdir()
    ext_path = _make_ext_repo(tmp_path, root, "ext-service")

    orig = os.environ.get("RSE_FEDERATION_EXCLUDE")
    os.environ["RSE_FEDERATION_EXCLUDE"] = str(ext_path)
    try:
        members = discover_members(str(root))
    finally:
        if orig is None:
            del os.environ["RSE_FEDERATION_EXCLUDE"]
        else:
            os.environ["RSE_FEDERATION_EXCLUDE"] = orig

    assert str(ext_path) not in members, (
        f"excluded ext-service must not appear in members; got {members}"
    )


def test_fe4_exclude_paths_multi_and_blank(tmp_path):
    """FE4: ~ expansion, multiple paths joined by pathsep, blank entries all work."""
    from rag_search.core.config import federation_exclude_paths
    a = str(tmp_path / "svc-a")
    b = str(tmp_path / "svc-b")
    raw = os.pathsep.join(["", a, "", b, ""])
    orig = os.environ.get("RSE_FEDERATION_EXCLUDE")
    os.environ["RSE_FEDERATION_EXCLUDE"] = raw
    try:
        result = federation_exclude_paths()
    finally:
        if orig is None:
            del os.environ["RSE_FEDERATION_EXCLUDE"]
        else:
            os.environ["RSE_FEDERATION_EXCLUDE"] = orig

    assert Path(a).resolve() in {Path(p) for p in result}
    assert Path(b).resolve() in {Path(p) for p in result}
    assert len(result) == 2, f"blanks must be stripped; got {result}"


def test_fe5_prefix_dir_excludes_subtree(tmp_path):
    """FE5: is_federation_excluded() returns True for a child of an excluded prefix dir."""
    from rag_search.core.config import is_federation_excluded

    parent = tmp_path / "excluded-parent"
    parent.mkdir()
    child = parent / "nested-child"
    child.mkdir()

    orig = os.environ.get("RSE_FEDERATION_EXCLUDE")
    os.environ["RSE_FEDERATION_EXCLUDE"] = str(parent)
    try:
        assert is_federation_excluded(str(child)), "child of excluded prefix dir must be excluded"
        assert not is_federation_excluded(str(tmp_path / "other-dir")), (
            "unrelated path must not be excluded"
        )
    finally:
        if orig is None:
            del os.environ["RSE_FEDERATION_EXCLUDE"]
        else:
            os.environ["RSE_FEDERATION_EXCLUDE"] = orig


def test_fe6_glob_entry_excludes_member(tmp_path):
    """FE6: discover_members() skips a member matched by a glob in RSE_FEDERATION_EXCLUDE."""
    from rag_search.daemon.federation import discover_members

    root = tmp_path / "fed-root"
    root.mkdir()
    ext = tmp_path / "vendor-cache-svc"
    ext.mkdir()
    (ext / "main.go").write_text("package main\n")
    (root / "vendor-cache-svc").symlink_to(ext)
    ext_resolved = str(ext.resolve())

    orig = os.environ.get("RSE_FEDERATION_EXCLUDE")
    os.environ["RSE_FEDERATION_EXCLUDE"] = "*/vendor-cache-*"
    try:
        members = discover_members(str(root))
    finally:
        if orig is None:
            del os.environ["RSE_FEDERATION_EXCLUDE"]
        else:
            os.environ["RSE_FEDERATION_EXCLUDE"] = orig

    assert ext_resolved not in members, (
        f"glob-excluded vendor-cache-svc must not appear in members; got {members}"
    )


def test_fe7_is_federation_excluded_unit_table(tmp_path):
    """FE7: is_federation_excluded() truth table: empty/exact/prefix/glob/non-match."""
    from rag_search.core.config import is_federation_excluded

    exact_dir = (tmp_path / "exact-svc").resolve()
    exact_dir.mkdir()
    child_dir = exact_dir / "sub"
    child_dir.mkdir()
    other_dir = (tmp_path / "other-svc").resolve()
    other_dir.mkdir()

    orig = os.environ.get("RSE_FEDERATION_EXCLUDE")
    os.environ["RSE_FEDERATION_EXCLUDE"] = os.pathsep.join([
        str(exact_dir), "*/glob-svc/*", "",
    ])
    try:
        assert not is_federation_excluded(""), "empty string → False"
        assert is_federation_excluded(str(exact_dir)), "exact match → True"
        assert is_federation_excluded(str(child_dir)), "child of prefix → True"
        assert is_federation_excluded("/nonexistent/glob-svc/anything"), "glob match → True"
        assert not is_federation_excluded(str(other_dir)), "non-matching → False"
    finally:
        if orig is None:
            del os.environ["RSE_FEDERATION_EXCLUDE"]
        else:
            os.environ["RSE_FEDERATION_EXCLUDE"] = orig


# ------------------------------------------------- FE8-FE10: the live daemon's own exclusion

# No absolute home path here on purpose (HR34/P18): systemd expands %h itself, so this text
# reads the same on every machine and can live in the tracked tree.
_REINSTALL_HINT = (
    f"{_VAR} is not in the running daemon's environment. It is configured by a systemd "
    f"drop-in — ~/.config/systemd/user/{_UNIT}.d/federation-exclude.conf containing\n"
    "    [Service]\n"
    f"    Environment={_VAR}=%h/git/github.com/<owner>/<repo>:*/_worktrees/*\n"
    f"then `systemctl --user daemon-reload && systemctl --user restart {_UNIT}`. Without it, "
    "member discovery re-enables every excluded repo at the next daemon start (§15.1 of "
    "docs/architecture/federation-ops-and-invariants.md)."
)


def _daemon_env() -> dict[str, str]:
    """The daemon's real environment, read from /proc/<MainPID>/environ.

    Not `os.environ`: this pytest process is not production. A shell probe that read its own
    env once reported all 58 `_worktrees` members as *not* excluded, producing a false
    "reconcile is about to re-index 541k deleted chunks" alarm — the unit sets the variable,
    that shell did not. `systemctl show -p Environment` shows what systemd *would* set on the
    next start; only /proc shows what the running process actually got.
    """
    r = subprocess.run(
        ["systemctl", "--user", "show", _UNIT, "-p", "MainPID", "--value"],
        capture_output=True, text=True, timeout=5,
    )
    pid = int(r.stdout.strip() or "0")
    assert pid > 0, (
        f"{_UNIT} is not running, so what the fleet's exclusion is actually set to cannot be read. "
        "Hard failure, not a skip: the whole live suite requires the daemon (HR15's no-skip rule), "
        "and a gate that quietly stands down when its subject is absent is how the exclusion got to "
        "live in one unversioned file with no test in the first place."
    )
    raw = Path(f"/proc/{pid}/environ").read_bytes().decode("utf-8", "replace")
    env: dict[str, str] = {}
    for item in raw.split("\0"):
        key, sep, value = item.partition("=")
        if sep:
            env[key] = value
    return env


@contextmanager
def _under_exclusion(value: str):
    """Evaluate the config predicates against `value`, then restore this process's own."""
    orig = os.environ.get(_VAR)
    os.environ[_VAR] = value
    try:
        yield value
    finally:
        if orig is None:
            os.environ.pop(_VAR, None)
        else:
            os.environ[_VAR] = orig


def _enabled_but_excluded(projects, value: str) -> list[str]:
    """Enabled rows the exclusion `value` covers — discovery and exclusion contradicting."""
    from rag_search.core.config import is_federation_excluded
    with _under_exclusion(value):
        return [p.path for p in projects if p.enabled and is_federation_excluded(p.path)]


def _armed_but_uncovered(projects, value: str) -> list[str]:
    """Disabled+never-indexed rows that `value` leaves un-excluded, i.e. one pass from re-index."""
    from rag_search.core.config import is_federation_excluded
    with _under_exclusion(value):
        return [
            p.path for p in projects
            if not p.enabled and p.indexed_at is None and Path(p.path).exists()
            and not is_federation_excluded(p.path)
        ]


def test_fe8_daemon_env_carries_federation_exclude():
    """FE8: the live daemon process has a non-empty, parseable RSE_FEDERATION_EXCLUDE."""
    from rag_search.core.config import _federation_exclude_entries

    value = _daemon_env().get(_VAR, "")
    assert value.strip(), _REINSTALL_HINT
    with _under_exclusion(value):
        exact, globs = _federation_exclude_entries()
    assert exact or globs, (
        f"{_VAR}={value!r} is set on the daemon but parses to zero entries "
        f"(all blanks/separators). {_REINSTALL_HINT}"
    )


def test_fe9_no_enabled_project_is_federation_excluded():
    """FE9: enabled rows and the exclusion in force must not contradict each other.

    An enabled row that the daemon's own value excludes means discovery re-enabled something
    the operator excluded — reconcile will then index it, which is the state the exclusion
    exists to prevent.
    """
    from rag_search.core.registry import list_projects

    bad = _enabled_but_excluded(list_projects(), _daemon_env().get(_VAR, ""))
    assert not bad, (
        f"{len(bad)} ENABLED registry rows are federation-excluded — reconcile will index them "
        f"anyway: {bad[:5]}. Disable them or drop them from the exclusion."
    )


def test_fe10_armed_disabled_rows_are_covered_by_the_exclusion():
    """FE10: no disabled-and-never-indexed row is left un-excluded.

    `enabled=False, indexed_at=None` is an *armed* row: `_needs_index()` returns True, and
    `register_all_members()` re-enables any discovered member at every daemon start. The only
    thing between such a row and a re-index is the exclusion, so the covering set is derived
    from live registry state rather than hand-written here — drop the worktree glob from the
    drop-in and this goes red without anyone having to remember to update a list in this file.
    """
    from rag_search.core.registry import list_projects

    armed = _armed_but_uncovered(list_projects(), _daemon_env().get(_VAR, ""))
    assert not armed, (
        f"{len(armed)} disabled rows are armed (indexed_at=None) yet nothing excludes them; "
        f"member discovery can re-enable and index each one: {armed[:5]}. Either extend the "
        f"exclusion or remove the rows outright. {_REINSTALL_HINT}"
    )


def test_fe11_the_gates_fire_when_the_exclusion_is_lost(tmp_path):
    """FE11 sufficiency: prove FE9/FE10 can go red — without touching the live drop-in.

    The drop-in is the only copy of the production value, so "unset it and watch" is not an
    available experiment. Losing it is modelled as the empty value (exactly what the daemon
    would receive if the file were deleted and the unit reloaded) fed to the same detectors.
    Real registry rows for FE10, injected rows for FE9: injection, not patching.
    """
    from rag_search.core.config import ProjectEntry
    from rag_search.core.registry import list_projects

    assert _armed_but_uncovered(list_projects(), ""), (
        "FE10 is vacuous: with the exclusion gone, not one armed disabled row is reported, so "
        "FE10 would stay green with the drop-in deleted. Either nothing in the fleet depends "
        "on the exclusion any more (retire FE10 with it) or the filter is wrong."
    )
    covered = tmp_path / "excluded-repo"
    covered.mkdir()
    rows = [ProjectEntry(path=str(covered), enabled=True),
            ProjectEntry(path=str(covered), enabled=False)]
    both = str(covered)
    assert _enabled_but_excluded(rows, both) == [both], "must flag an enabled+excluded row"
    assert _enabled_but_excluded(rows, "") == [], "must not flag when nothing is excluded"
    assert _armed_but_uncovered(rows, "") == [both], "must flag an armed row nothing covers"
    assert _armed_but_uncovered(rows, both) == [], "must not flag a covered armed row"
