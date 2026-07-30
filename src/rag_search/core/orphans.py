"""Refuse an orphan sweep whose own answer says its input has already gone wrong.

Two sites delete index dirs: `maintenance()`'s 6-hourly sweep and `rag-search clean-orphans --yes`.
Both compute the same set difference — dirs under `INDEX_ROOT` minus the dirs the registry claims —
and that subtraction has produced a fleet-wide deletion twice, for two unrelated reasons:

  * the comparison was wrong (a registry *path* tested against a dir *name*, which can never match,
    so all 179 dirs read as orphans and `--yes` deleted the fleet's index — `cli.py`'s own comment);
  * the registry was wrong (07-30: a test helper removed 198 rows in-process, which leaves every
    surviving store genuinely unowned by the only authority the sweep has).

The set difference was faithful on both occasions. Nothing at the point of subtraction could have
noticed, because in each case the sweep was given a false premise and reasoned from it correctly.

So the check here is not on the arithmetic, it is on the *shape of the answer*. A sweep that has
concluded it should delete nearly everything it can see is reporting a broken premise, not a dirty
disk: real orphans accumulate a few at a time from interrupted runs, never as the majority of a
fleet. That heuristic cannot prove the premise right, and it is not meant to — it only has to make
the two failures that actually happened loud instead of silent, which a floor does and a more
careful subtraction does not.

Living here rather than at either call site is the point. Two copies of a rule with nothing
detecting divergence is how those sites came to disagree in the first place: `sweeps.py` compared
`.name` while `cli.py` compared `.resolve()`, one of them corrected after an incident and the other
never told. This module keeps the corrected form.
"""
from __future__ import annotations

from pathlib import Path

# A blast cap alone would refuse the ordinary case: a run that leaves 1 orphan beside 1 live store
# is 50% of the tree and completely routine. Small absolute deletions are therefore always allowed —
# the cap exists to catch "everything is an orphan", not to police cleanup. 5 is above every orphan
# count observed from real interrupted runs (19 dirs accumulated over a full day, but across many
# sweeps) and far below the 138 and 179 of the two incidents.
_ALWAYS_ALLOWED = 5
_MAX_FRACTION = 0.5


class OrphanSweepRefusedError(RuntimeError):
    """The orphan set is not credible. Raised before anything is deleted, never after."""


def orphan_dirs(*, allow_bulk: bool = False) -> list[Path]:
    """Index dirs under `INDEX_ROOT` that no registry row owns, or refuse to answer.

    `allow_bulk` lifts the majority cap for an operator who has read the refusal and decided the
    tree really is that dirty. It deliberately does **not** lift the empty-registry refusal: with
    zero rows there is nothing left to check the decision against, so consent there would be
    consent without information. An operator who genuinely wants an empty `INDEX_ROOT` emptied can
    say so directly with `rm -rf`, which is one explicit act rather than a sweep that mistook a
    wiped registry for a finished cleanup.
    """
    from rag_search.core.config import INDEX_ROOT, index_dir
    from rag_search.core.registry import list_projects

    if not INDEX_ROOT.exists():
        return []
    stores = [d for d in INDEX_ROOT.iterdir() if d.is_dir()]
    if not stores:
        return []

    # Re-derived here, at the point of deletion, rather than accepted from the caller — the same
    # principle as `assert_under_test_base`. Every guard that lived in a caller was intact on 07-30.
    rows = list_projects()
    if not rows:
        raise OrphanSweepRefusedError(
            f"registry holds 0 projects while {INDEX_ROOT} holds {len(stores)} store dir(s). "
            "That state is self-contradictory, not a finished cleanup: stores are only ever "
            "written for a registered project, so an empty registry beside a full index tree means "
            "the registry was lost, not that the projects were. Refusing to delete "
            f"{len(stores)} store dir(s); restore the registry first "
            "(`projects.json.session-snapshot`, or `projects.json.bak.*`).")

    known = {index_dir(p.path).resolve() for p in rows}
    orphans = sorted(d for d in stores if d.resolve() not in known)
    if (not allow_bulk
            and len(orphans) > _ALWAYS_ALLOWED
            and len(orphans) > _MAX_FRACTION * len(stores)):
        raise OrphanSweepRefusedError(
            f"{len(orphans)} of {len(stores)} store dir(s) under {INDEX_ROOT} have no registry "
            f"row — more than half the tree, against {len(rows)} registered project(s). Orphans "
            "accumulate a few at a time; a majority means the registry or the ownership test is "
            "wrong, and re-embedding is the cost of being wrong. Refusing.")
    return orphans
