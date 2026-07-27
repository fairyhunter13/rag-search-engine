"""Live gates: BPRE's source walk must hoist per-directory work — without changing what it sees.

`_bpre_source_sig` over the 194-member inosoft federation measured **83.6s**, and it runs at the
top of `_reconstruct_processes_locked` *before* the stamp comparison it feeds — including on the
calls that immediately early-return. `sweeps.py` calls that "a cheap no-op on an unchanged root".

Profiling said the cost is not I/O: ~5s of syscalls against 2.6M `Path.__init__`, 1.8M parts
lookups and 1.2M `posixpath.join` calls. `_source_files` asked `is_ignored_path` to re-derive, for
every single file, three things that are constant across a directory — the effective config, the
ancestor `.gitignore` chain, and (via `p.is_dir()`) a `stat()` the `os.walk` already answered.

The plan this replaces was to persist the memo in `process_graph.db` instead. That was dropped on
measurement: `_bpre_code_sig`'s memo is keyed on the member root's *coarse* mtime and is only ever
invalidated by `sweeps.on_change`, i.e. by a live filesystem event. Recomputing it on restart is
the only thing that catches an edit made while the daemon was down, so persisting it would have
bought speed with the same silent-staleness bug WG7 exists to prevent.

A hoist is only worth anything if it is invisible, so the gates come in pairs:

BW1  the hoisted walk returns exactly what per-file derivation returned (the load-bearing one)
BW2  a caller-supplied chain must not survive `respect_gitignore: false`
BW3  the hoist is materially faster on a realistic tree (else it is not worth the parameters)
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.live


def _reference_source_files(member_path: str) -> list[Path]:
    """The pre-hoist body, verbatim: derives cfg, is_dir and the chain once per *file*.

    Kept here rather than in the fixture so the comparison is against real prior behaviour, not
    against a paraphrase of it. This is the oracle BW1 checks against.
    """
    from rag_search.core.registry import get_project
    from rag_search.index.discover import detect_language, is_code_language, is_ignored_path
    from rag_search.kb.bpre import _is_test_file
    root = Path(member_path)
    entry = get_project(member_path)
    nested = {Path(m).resolve() for m in (entry.federation if entry and entry.federation else [])}
    out: list[Path] = []
    try:
        for dirpath, dirs, files in os.walk(str(root)):
            dp = Path(dirpath)
            dirs[:] = [
                d for d in dirs
                if (dp / d).resolve() not in nested and not is_ignored_path(dp / d, root)
            ]
            for f in files:
                p = dp / f
                if (is_code_language(detect_language(p))
                        and not _is_test_file(str(p))
                        and not is_ignored_path(p, root)):
                    out.append(p)
    except OSError:
        pass
    return out


@pytest.fixture()
def gnarly_tree(safe_tmp_path):
    """A tree whose every decision branch is exercised: nested .gitignore, hidden dirs, an
    ignored dir name, generated files, tests, and a deep path so the ancestor chain is real."""
    root = safe_tmp_path / "member"
    (root / ".gitignore").parent.mkdir(parents=True, exist_ok=True)
    (root / ".gitignore").write_text("*.pyc\nbuild/\n")
    for depth in range(6):
        d = root / "/".join(f"lvl{i}" for i in range(depth + 1))
        d.mkdir(parents=True, exist_ok=True)
        # One at every level, so the ancestor chain a deep file must consult is genuinely deep —
        # that chain is exactly what the hoist stops rebuilding per file.
        (d / ".gitignore").write_text(f"skip{depth}_*.py\n")
        for i in range(25):
            (d / f"mod{i}.py").write_text(f"def m{i}():\n    return {i}\n")
            (d / f"skip{depth}_{i}.py").write_text("def gone():\n    pass\n")
        # BPRE's rule is the `_test.py` *suffix* (plus _TEST_DIRS), not a `test_` prefix.
        (d / f"thing{depth}_test.py").write_text("def test_x():\n    pass\n")
        (d / f"bundle{depth}.generated.js").write_text("var x=1;\n")
        (d / f"dead{depth}.pyc").write_text("x")
    for junk in (".hidden", "node_modules", "build"):
        j = root / junk
        j.mkdir(parents=True, exist_ok=True)
        (j / "buried.py").write_text("def buried():\n    return 0\n")
    return root


def test_bw1_the_hoisted_walk_sees_exactly_what_per_file_derivation_saw(gnarly_tree):
    """BW1: same files, or the speedup bought nothing but a silent change in what BPRE indexes.

    This is the only gate that matters. `is_dir` and `chain` are now supplied by the caller, so a
    wrong hoist — the chain taken one directory too high, `is_dir` inverted — would still produce
    a plausible file list and a plausible signature. Nothing downstream would notice; the process
    graph would just quietly describe a different codebase.

    The oracle is the pre-hoist body itself, so this compares against real prior behaviour.
    """
    from rag_search.kb.bpre import _source_files

    got = sorted(str(p) for p in _source_files(str(gnarly_tree)))
    want = sorted(str(p) for p in _reference_source_files(str(gnarly_tree)))
    assert got == want, (
        "BW1: hoisted walk diverged from per-file derivation.\n"
        f"  only hoisted: {sorted(set(got) - set(want))[:5]}\n"
        f"  only per-file: {sorted(set(want) - set(got))[:5]}"
    )
    # A gate that passes on an empty list would pass on a broken walk too.
    assert len(got) > 100, f"BW1: fixture produced only {len(got)} files — it cannot discriminate"
    assert not any("skip" in Path(p).name for p in got), "BW1: nested .gitignore was not applied"
    assert not any(Path(p).name.endswith("_test.py") for p in got), "BW1: test files leaked in"
    assert not any(".generated." in Path(p).name for p in got), "BW1: codegen output leaked in"


def test_bw2_a_supplied_chain_cannot_override_respect_gitignore_false(gnarly_tree):
    """BW2: opting out of .gitignore must still opt out when the caller passes a chain.

    The hoist made `chain` a parameter, and the obvious way to write that (`if chain is None:`)
    silently lets a caller's chain apply to a project whose config set `respect_gitignore: false`
    — a config that would then be ignored only on the fast path. Same inputs, different answer
    depending on which caller you came through, which is the worst kind of divergence.
    """
    from rag_search.core.index_config import ProjectConfig
    from rag_search.index.discover import gitignore_chain_for_dir, is_ignored_path

    d = gnarly_tree / "lvl0"
    chain = gitignore_chain_for_dir(d / "lvl1", gnarly_tree)
    opted_out = ProjectConfig(
        exclude=[], include=[], use_default_ignores=True,
        respect_gitignore=False, max_pending_files=0,
    )
    honoured = ProjectConfig(
        exclude=[], include=[], use_default_ignores=True,
        respect_gitignore=True, max_pending_files=0,
    )
    target = d / "lvl1" / "skip1_0.py"
    assert is_ignored_path(target, gnarly_tree, honoured, is_dir=False, chain=chain), (
        "BW2: fixture is wrong — this file must be gitignored when the chain is honoured"
    )
    assert not is_ignored_path(target, gnarly_tree, opted_out, is_dir=False, chain=chain), (
        "BW2: a caller-supplied chain overrode respect_gitignore=false"
    )


def test_bw3_the_hoist_actually_costs_less_cpu(gnarly_tree):
    """BW3: if the hoisted walk isn't measurably cheaper, the extra parameters aren't worth it.

    BW1 proves the walk still sees the same files; on its own that is also what a hoist that
    hoisted nothing would prove. Output can't show the difference, so this is a CPU-time
    assertion — `process_time`, not wall clock, since this box runs a live daemon beside the
    suite. Best-of-3 both ways; the threshold is deliberately well under the 21% measured on the
    real 194-member federation (83.6s → 65.6s), because a synthetic tree is shallower.
    """
    from rag_search.kb.bpre import _source_files

    def best(fn) -> float:
        return min(_cpu_cost(fn, str(gnarly_tree)) for _ in range(3))

    ref, hoisted = best(_reference_source_files), best(_source_files)
    assert hoisted < ref * 0.92, (
        f"BW3: hoisted walk cost {hoisted:.3f}s CPU vs {ref:.3f}s for per-file derivation "
        f"({100 * (1 - hoisted / ref):.0f}% saved) — the hoist is not doing its job"
    )


def _cpu_cost(fn, arg) -> float:
    t0 = time.process_time()
    fn(arg)
    return time.process_time() - t0
