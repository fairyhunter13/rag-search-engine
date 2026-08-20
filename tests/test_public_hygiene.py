"""The tracked tree is publishable, and an unset ban is the failing state.

This repo is MIT and public; the machine it was built on is not. The guard scans
every tracked path and every tracked line for a set of substrings supplied out of
band -- out of band because writing the banned names into the repo to check for
them is the leak it exists to prevent.

`NAME_BAN` unset fails. A guard that stands down when its input is missing
reports the same green as a clean tree, which is the one outcome that must not
be reachable by accident. A clean clone declares itself with `NAME_BAN=none`.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
from _pytest.outcomes import Failed

from coderag import config

REPO = Path(__file__).resolve().parents[1]

# `/home/<user>/` is how this tree is *supposed* to write a host path, so the
# pattern has to exclude the placeholder or the archive that documents the rule
# is the thing that breaks it.
HOME_PATH = re.compile(r"/(?:home|Users)/(?!<)[A-Za-z0-9._-]+")


def terms() -> list[str]:
    raw = config.NAME_BAN.strip()
    if not raw:
        pytest.fail("CODERAG_NAME_BAN is unset; export the ban list, or =none for a clean clone")
    if raw.lower() == "none":
        return []
    split = [t.strip().lower() for t in raw.split(",") if t.strip()]
    # A colon-joined list survives the split as one token that matches nothing,
    # and an armed-but-inert guard reads exactly like a clean tree. Unset is
    # already fatal; mis-separated has to be too, or fail-closed only covers
    # the half that announces itself.
    if bad := [t for t in split if ":" in t or ";" in t]:
        pytest.fail(f"CODERAG_NAME_BAN has {len(bad)} token(s) holding a separator; the list is "
                    "comma-separated and this one was joined with something else")
    return split


def hits(text: str, banned: list[str]) -> list[str]:
    lowered = text.lower()
    return [t for t in banned if t in lowered]


def tracked() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPO, capture_output=True, text=True, check=True
    )
    return [p for p in out.stdout.split("\0") if p]


# ------------------------------------------------- the guard, tested as a guard


def test_an_unset_ban_fails_rather_than_standing_down(monkeypatch):
    monkeypatch.setattr(config, "NAME_BAN", "   ")
    with pytest.raises(Failed):
        terms()


def test_a_mis_separated_list_fails_rather_than_arming_inert(monkeypatch):
    """The shape that actually happened: the installer joined with `os.pathsep`
    while the guard split on a comma, so seven tests passed over one token that
    was in nothing."""
    monkeypatch.setattr(config, "NAME_BAN", "alpha:beta:gamma")
    with pytest.raises(Failed):
        terms()


def test_a_clean_clone_can_declare_itself(monkeypatch):
    monkeypatch.setattr(config, "NAME_BAN", "none")
    assert terms() == []


def test_the_scan_catches_a_planted_name():
    """Without this the three scans below pass just as well against an empty
    ban list, an empty tree, or a `hits` that always returns nothing."""
    assert hits("the AcmeCorp deploy script", ["acmecorp"]) == ["acmecorp"]
    assert hits("PATH/AcmeCorp/x", ["acmecorp"]) == ["acmecorp"]
    assert hits("nothing to see", ["acmecorp"]) == []


# ------------------------------------------------------------------- the sweep


def test_no_tracked_path_carries_a_banned_name():
    banned = terms()
    assert (found := [p for p in tracked() if hits(p, banned)]) == [], found


def test_no_tracked_line_carries_a_banned_name():
    banned = terms()
    found = []
    for rel in tracked():
        path = REPO / rel
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # a binary blob carries no readable name
        for number, line in enumerate(text.splitlines(), 1):
            if matched := hits(line, banned):
                found.append(f"{rel}:{number}: {matched}")
    assert found == [], found


def test_no_tracked_line_leaks_a_home_directory_path():
    """A machine-local absolute path is not a name, but it identifies the
    machine just as well, and it is the one that survives a rename."""
    # Assembled rather than written out: a literal here is a tracked line that
    # this test then finds, and the guard failing on its own fixture is the
    # noise that gets a guard disabled.
    real = "see /home/" + "realuser/git/x"
    assert HOME_PATH.search(real), "the pattern must catch a real one"
    assert not HOME_PATH.search("see /home/<user>/git/x"), "and must allow the placeholder"
    found = []
    for rel in tracked():
        path = REPO / rel
        if not path.is_file() or path.suffix in {".lock"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if HOME_PATH.search(line):
                found.append(f"{rel}:{number}")
    assert found == [], found


def test_the_registry_is_not_tracked():
    """It is the one file that carries every repo path on this machine, and it
    has no reason to be in git at all."""
    assert not [p for p in tracked() if p.endswith("projects.json")]
