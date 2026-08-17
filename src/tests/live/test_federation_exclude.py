"""FE — RSE_FEDERATION_EXCLUDE integration tests.

The exclusion has two sources: `federation.exclude` in a federation root's `.rse-index.yaml`
carries the layout patterns, RSE_FEDERATION_EXCLUDE the host-specific absolute paths HR34 keeps
out of the tracked tree. Every gate below that names a value states which source supplied it.

FE1 — the exclusion parses to nothing when no source is consulted.
FE2 — discover_members() includes a symlinked external repo when not excluded.
FE3 — discover_members() skips a repo whose resolved path is excluded, from either source.
FE4 — the env string grammar: ~ expansion, pathsep-joined paths, blank entries.
FE4b — the config list carries the same grammar.
FE5 — is_federation_excluded() excludes by prefix (subtree, not just exact path).
FE6 — discover_members() skips a member matched by a glob entry, from either source.
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
FE11 — sufficiency: FE8-FE10 would ALL stay green with the drop-in deleted, because a member
       that was never registered appears in no row FE9/FE10 inspect. So FE11 runs the real
       discovery walker twice — with the daemon's value and with none — and requires the
       second to return strictly more. That is the fact the fleet actually rests on.
"""
from __future__ import annotations

import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

pytestmark = pytest.mark.live

_UNIT = "rag-search-mcp-daemon.service"
_VAR = "RSE_FEDERATION_EXCLUDE"
_ALL_SOURCES = ("env", "config")


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


@contextmanager
def _exclusion_from(source: str, entries: list[str], where: Path):
    """Install `entries` as the exclusion through exactly one source, and yield that source.

    The two sources are not interchangeable — env carries host-specific absolute paths, config
    carries layout patterns — but the *grammar* is meant to be identical, and a test that only
    ever exercised env is what let the config half be written without one.
    """
    import rag_search.core.config as cfg
    if source == "env":
        with _under_exclusion(os.pathsep.join(entries)):
            yield source
        return
    where.parent.mkdir(parents=True, exist_ok=True)
    where.write_text(
        "federation:\n  exclude:\n" + "".join(f"    - {e!r}\n" for e in entries)
    )
    orig_fn, orig_cache = cfg._federation_root_configs, cfg._CFG_EXCLUDE_CACHE
    cfg._federation_root_configs = lambda: [where]
    cfg._CFG_EXCLUDE_CACHE = None
    try:
        with _under_exclusion(""):
            yield source
    finally:
        cfg._federation_root_configs = orig_fn
        cfg._CFG_EXCLUDE_CACHE = orig_cache


def _make_ext_repo(tmp_base: Path, root: Path, name: str) -> Path:
    """Create a minimal external repo (has a .go file) OUTSIDE root, symlink it into root."""
    ext = tmp_base / ("_ext_" + name)
    ext.mkdir()
    (ext / "main.go").write_text("package main\n")
    (root / name).symlink_to(ext)
    return ext.resolve()


def test_fe1_exclude_is_empty_when_no_source_is_consulted():
    """FE1: with neither source consulted, the exclusion parses to nothing.

    Popping the env var is no longer enough to express that. There are two sources now, and the
    config one is read from whatever federation roots the registry happens to hold — so an
    env-only arm asserts a property of this machine's filesystem, not of the parser, and would
    go red or green depending on which fleet the suite runs on.
    """
    from rag_search.core.config import _federation_exclude_entries
    orig = os.environ.pop("RSE_FEDERATION_EXCLUDE", None)
    try:
        assert _federation_exclude_entries(sources=()) == (frozenset(), ())
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


@pytest.mark.parametrize("source", ["env", "config"])
def test_fe3_discover_members_skips_excluded_repo(tmp_path, source):
    """FE3: a repo the exclusion names is not returned by discover_members, from either source."""
    from rag_search.daemon.federation import discover_members
    root = tmp_path / "fed-root"
    root.mkdir()
    ext_path = _make_ext_repo(tmp_path, root, "ext-service")

    with _exclusion_from(source, [str(ext_path)], tmp_path / "cfg" / ".rse-index.yaml"):
        members = discover_members(str(root))

    assert str(ext_path) not in members, (
        f"excluded ext-service must not appear in members ({source} source); got {members}"
    )


def test_fe4_env_string_grammar_multi_and_blank(tmp_path):
    """FE4: ~ expansion, multiple paths joined by pathsep, blank entries — the env grammar."""
    from rag_search.core.config import _federation_exclude_entries
    a = str(tmp_path / "svc-a")
    b = str(tmp_path / "svc-b")
    raw = os.pathsep.join(["", a, "", b, ""])
    orig = os.environ.get("RSE_FEDERATION_EXCLUDE")
    os.environ["RSE_FEDERATION_EXCLUDE"] = raw
    try:
        exact, globs = _federation_exclude_entries(sources=("env",))
    finally:
        if orig is None:
            del os.environ["RSE_FEDERATION_EXCLUDE"]
        else:
            os.environ["RSE_FEDERATION_EXCLUDE"] = orig

    assert Path(a).resolve() in {Path(p) for p in exact}
    assert Path(b).resolve() in {Path(p) for p in exact}
    assert len(exact) == 2, f"blanks must be stripped; got {exact}"
    assert globs == (), f"no entry here has a glob character; got {globs}"


def test_fe4b_config_form_carries_the_same_grammar(tmp_path):
    """FE4b: the file source parses the same entry kinds, so `~` is not an env-only asymmetry.

    A YAML list has no pathsep, which is the whole point of moving layout patterns there — but
    the *entries* must still expand `~` and still split into globs and resolved paths, or the
    two sources mean different things by the same string.
    """
    from rag_search.core.config import _federation_exclude_entries

    entries = ["~/never-indexed", "*/_worktrees/*"]
    with _exclusion_from("config", entries, tmp_path / ".rse-index.yaml"):
        exact, globs = _federation_exclude_entries(sources=("config",))
    assert str(Path("~/never-indexed").expanduser().resolve()) in exact, (
        f"config entries must expanduser and resolve like env ones; got {exact}"
    )
    assert "*/_worktrees/*" in globs, f"glob entry must stay a glob; got {globs}"


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


@pytest.mark.parametrize("source", ["env", "config"])
def test_fe6_glob_entry_excludes_member(tmp_path, source):
    """FE6: discover_members() skips a member matched by a glob, from either source."""
    from rag_search.daemon.federation import discover_members

    root = tmp_path / "fed-root"
    root.mkdir()
    ext = tmp_path / "vendor-cache-svc"
    ext.mkdir()
    (ext / "main.go").write_text("package main\n")
    (root / "vendor-cache-svc").symlink_to(ext)
    ext_resolved = str(ext.resolve())

    with _exclusion_from(source, ["*/vendor-cache-*"], tmp_path / "cfg" / ".rse-index.yaml"):
        members = discover_members(str(root))

    assert ext_resolved not in members, (
        f"glob-excluded vendor-cache-svc must not appear in members ({source}); got {members}"
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

# No absolute home path here on purpose (HR34): systemd expands %h itself, so this text
# reads the same on every machine and can live in the tracked tree.
_REINSTALL_HINT = (
    "The federation exclusion resolves to nothing. Layout patterns belong in the federation "
    "root's .rse-index.yaml:\n"
    "    federation:\n"
    "      exclude:\n"
    "        - '*/_worktrees/*'\n"
    f"and host-specific absolute paths in {_VAR}, set by the systemd drop-in "
    f"~/.config/systemd/user/{_UNIT}.d/federation-exclude.conf, then "
    f"`systemctl --user daemon-reload && systemctl --user restart {_UNIT}`. With neither, "
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


def _enabled_but_excluded(projects, value: str, sources=_ALL_SOURCES) -> list[str]:
    """Enabled rows the exclusion covers — discovery and exclusion contradicting."""
    from rag_search.core.config import is_federation_excluded
    with _under_exclusion(value):
        return [p.path for p in projects
                if p.enabled and is_federation_excluded(p.path, sources)]


def _armed_but_uncovered(projects, value: str, sources=_ALL_SOURCES) -> list[str]:
    """Disabled+never-indexed rows the exclusion leaves uncovered, i.e. one pass from re-index."""
    from rag_search.core.config import is_federation_excluded
    with _under_exclusion(value):
        return [
            p.path for p in projects
            if not p.enabled and p.indexed_at is None and Path(p.path).exists()
            and not is_federation_excluded(p.path, sources)
        ]


def _members_the_exclusion_removes(root: str, value: str) -> list[str]:
    """Members `discover_members` returns with no source consulted that the exclusion keeps out.

    Asks the production walker instead of re-deriving its filter here, so it measures the path
    that actually creates registry rows: `index_members` upserts every returned member as
    `enabled=True`, and reconcile indexes it from there. Re-deriving the filter would only prove
    the filter agrees with itself.

    "No exclusion" is `sources=()`, not an empty env var. With the layout patterns in config,
    an empty env var leaves the config source in force, both arms return the same members, and
    this returns [] — turning the sufficiency proof into a silent pass.
    """
    from rag_search.daemon.federation import discover_members
    without = set(discover_members(root, sources=()))
    with _under_exclusion(value):
        return sorted(without - set(discover_members(root)))


def test_fe8_the_effective_exclusion_is_non_empty():
    """FE8: the exclusion the daemon actually resolved parses to at least one entry.

    Asserting on the env var was never the point — the point was the *running process's* real
    value rather than this shell's, which is why `_daemon_env` reads /proc. Now that the value
    has two sources, an env-only assertion would red on a correct fleet that keeps every pattern
    in config, so this asserts the union and reports which source supplied it. SE1's docstring
    makes the same argument for effective configuration over file comparison.
    """
    from rag_search.core.config import _federation_exclude_entries

    value = _daemon_env().get(_VAR, "")
    with _under_exclusion(value):
        exact, globs = _federation_exclude_entries()
        from_env = _federation_exclude_entries(sources=("env",))
        from_cfg = _federation_exclude_entries(sources=("config",))
    assert exact or globs, (
        f"the exclusion in force parses to zero entries — env {_VAR}={value!r}, config "
        f"contributed {from_cfg}. {_REINSTALL_HINT}"
    )
    assert from_env != (frozenset(), ()) or from_cfg != (frozenset(), ()), (
        "neither source contributed, yet the union is non-empty — the union lost track of where "
        "its entries came from, and FE11's loss experiment cannot mean anything."
    )


def test_fe9_no_enabled_project_is_federation_excluded(tmp_path):
    """FE9: enabled rows and the exclusion in force must not contradict each other.

    An enabled row that the effective exclusion covers means discovery re-enabled something the
    operator excluded — reconcile will then index it, which is the state the exclusion exists to
    prevent. "In force" is the union of both sources, so a fleet that keeps every pattern in
    `.rse-index.yaml` is graded on the same rule as one that keeps them in the environment.

    The second half is not decoration. Moving the patterns into config does not turn this gate
    red, it turns it *green and vacuous*: an assertion that no row contradicts the exclusion
    reads identically when the exclusion covers nothing at all. So a contradiction is injected —
    an enabled row under one added entry — and the predicate is required to name it.
    """
    from rag_search.core.config import ProjectEntry
    from rag_search.core.registry import list_projects

    value = _daemon_env().get(_VAR, "")
    bad = _enabled_but_excluded(list_projects(), value)
    assert not bad, (
        f"{len(bad)} ENABLED registry rows are federation-excluded — reconcile will index them "
        f"anyway: {bad[:5]}. Disable them or drop them from the exclusion."
    )
    canary = str(tmp_path / "canary-svc")
    injected = _enabled_but_excluded(
        [ProjectEntry(path=canary, enabled=True)],
        os.pathsep.join([p for p in (value, canary) if p]),
    )
    assert injected == [canary], (
        f"the predicate FE9 rests on did not flag an enabled row the exclusion covers: {injected}"
    )


def test_fe10_armed_disabled_rows_are_covered_by_the_exclusion(tmp_path):
    """FE10: no disabled-and-never-indexed row is left un-excluded.

    `enabled=False, indexed_at=None` is an *armed* row: `_needs_index()` returns True, and
    `register_all_members()` re-enables any discovered member at every daemon start. The only
    thing between such a row and a re-index is the exclusion, so the covering set is derived from
    live registry state rather than hand-written here, against the union of both sources.

    It does NOT follow that dropping a pattern turns this red — that claim stood here until
    2026-07-30 and is measurably false. Discovery would register the worktrees as `enabled=True`,
    which this test does not look at; the registry currently holds zero disabled rows, so the
    first assertion is vacuously true today. FE11's live half is what actually catches a lost
    exclusion; the injected armed row below is what keeps the predicate itself honest, since a
    fleet with no armed rows and a fleet with a broken predicate look the same from here.
    """
    from rag_search.core.config import ProjectEntry
    from rag_search.core.registry import list_projects

    value = _daemon_env().get(_VAR, "")
    armed = _armed_but_uncovered(list_projects(), value)
    assert not armed, (
        f"{len(armed)} disabled rows are armed (indexed_at=None) yet nothing excludes them; "
        f"member discovery can re-enable and index each one: {armed[:5]}. Either extend the "
        f"exclusion or remove the rows outright. {_REINSTALL_HINT}"
    )
    canary = tmp_path / "canary-svc"
    canary.mkdir()
    injected = _armed_but_uncovered([ProjectEntry(path=str(canary), enabled=False)], value)
    assert injected == [str(canary)], (
        f"the predicate FE10 rests on did not flag an armed row nothing covers: {injected}"
    )


def test_fe11_the_gates_fire_when_the_exclusion_is_lost(tmp_path):
    """FE11 sufficiency: prove the exclusion is load-bearing — without touching the live drop-in.

    The drop-in is the only copy of the production value, so "unset it and watch" is not an
    available experiment. Losing it is modelled as the empty value (exactly what the daemon would
    receive if the file were deleted and the unit reloaded) fed to the same production code.

    The live half used to ask a different question — "does any armed disabled row go un-excluded
    with the value gone?" — and it was **flaky by construction**, passing locally and failing on
    the runner 40 min later with no code change between. Measured 2026-07-30: 152 registry rows,
    every one enabled, **zero disabled and zero armed**. That is not the exclusion decaying, it is
    the exclusion *working* — it filters at discovery time, so the rows FE10 looks for are never
    created. FE10 is a downstream guard whose subject is empty precisely because the upstream one
    holds, and an assertion about an empty set is a coin flip, not a gate.

    Worse, the pair is not sufficient without this: delete the drop-in and discovery registers the
    58 worktrees as `enabled=True` (`index_members`), which FE10 does not look at and FE9 does not
    flag because nothing excludes them any more. **Both would stay green.** So the live half now
    asserts the fact that actually holds them out — that member discovery, run for real, returns
    strictly fewer members under the daemon's value than under none. Measured: 193 -> 135, i.e. 58
    `_worktrees` members and ~10 s of walking, which is what this test costs. It self-retires
    honestly: when nothing on disk needs excluding any more, the count goes to zero and the message
    says to retire FE8-FE10 with it.
    """
    from rag_search.core.config import ProjectEntry
    from rag_search.core.registry import list_projects

    roots = [p.path for p in list_projects() if p.federation]
    kept_out = [m for r in roots
                for m in _members_the_exclusion_removes(r, _daemon_env().get(_VAR, ""))]
    assert kept_out, (
        f"the exclusion keeps nothing out of member discovery across {len(roots)} federation "
        "root(s), so FE9 and FE10 would BOTH stay green with the drop-in deleted — neither "
        "looks at a member that was never registered. Either nothing on disk depends on the "
        f"exclusion any more (retire FE8-FE10 with it) or discovery changed. Roots: {roots}"
    )
    covered = tmp_path / "excluded-repo"
    covered.mkdir()
    rows = [ProjectEntry(path=str(covered), enabled=True),
            ProjectEntry(path=str(covered), enabled=False)]
    both = str(covered)
    assert _enabled_but_excluded(rows, both) == [both], "must flag an enabled+excluded row"
    assert _enabled_but_excluded(rows, "", sources=()) == [], (
        "must not flag when nothing is excluded"
    )
    assert _armed_but_uncovered(rows, "", sources=()) == [both], (
        "must flag an armed row nothing covers"
    )
    assert _armed_but_uncovered(rows, both) == [], "must not flag a covered armed row"


_FE12_CHILD = """
import json, sys
from pathlib import Path
from rag_search.core.config import ProjectEntry
from rag_search.core.registry import get_project, upsert_project
from rag_search.daemon.federation import register_all_members

base, source = Path(sys.argv[1]), sys.argv[2]
kept, excluded = base / "kept", base / "_worktrees" / "excluded"
for p in (kept, excluded):
    p.mkdir(parents=True)
    (p / "main.go").write_text("package main\\n")
    upsert_project(ProjectEntry(path=str(p), enabled=True))
if source == "config":
    (base / ".rse-index.yaml").write_text("federation:\\n  exclude:\\n    - '*/_worktrees/*'\\n")
    upsert_project(ProjectEntry(path=str(base), enabled=True, federation=[str(kept)]))
register_all_members()
print(json.dumps({"kept": get_project(str(kept)).enabled,
                  "excluded": get_project(str(excluded)).enabled}))
"""


@pytest.mark.parametrize("source", ["env", "config"])
def test_fe12_register_all_members_disables_an_excluded_row(safe_tmp_path, source):
    """FE12: the daemon repairs the contradiction FE9 reports, instead of only reporting it.

    FE9 asserts a *state*, and a state assertion cannot distinguish "nothing can break this" from
    "nothing has broken it lately". It broke: 58 `_worktrees` rows sat enabled on 2026-07-30 with
    the drop-in installed, in force, and the daemon restarted since. The reason is structural —
    `RSE_FEDERATION_EXCLUDE` reaches only processes that were handed it, and the daemon's copy
    comes from a systemd drop-in, so the CLI, this suite and any script run `index_members` with
    it unset and upsert the excluded members straight back as `enabled=True`. Nothing then undid
    it, because exclusion filters at discovery time: those paths are members of nothing any more,
    and a row nothing discovers is a row nothing revisits.

    **The `config` arm is the fix under test**, and it is the arm that runs with the variable
    unset: a file every process reads is what closes the hole the `env` arm can only paper over
    for processes systemd spawned. The `env` arm stays because host-specific absolute paths still
    travel that way and must keep working.

    Subprocess because `core.registry` binds `REGISTRY_PATH` at import (same reason as CO3/CO4),
    so this drives the real `register_all_members` against a real registry file without going near
    the fleet's. Under `safe_tmp_path` rather than `tmp_path` because `upsert_project` refuses a
    forbidden root, `/tmp` included. `kept` is asserted alongside `excluded` on purpose: a repair
    that disables everything would satisfy the interesting half on its own.
    """
    import json
    tmp = safe_tmp_path / f"fe12-{source}"
    (tmp / "ws").mkdir(parents=True)
    env = {**os.environ,
           "RSE_REGISTRY_PATH": str(tmp / "projects.json"),
           _VAR: "*/_worktrees/*" if source == "env" else ""}
    r = subprocess.run([sys.executable, "-c", _FE12_CHILD, str(tmp / "ws"), source],
                       capture_output=True, text=True, env=env, timeout=180)
    assert r.returncode == 0, f"child exit {r.returncode}: {r.stdout}\n{r.stderr}"
    got = json.loads(r.stdout.strip().splitlines()[-1])
    assert got == {"kept": True, "excluded": False}, (
        f"register_all_members left the registry contradicting the exclusion ({source}): {got}")
