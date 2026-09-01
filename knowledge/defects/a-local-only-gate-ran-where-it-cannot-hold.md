---
type: Defect
resource: tests/test_okf_bundle.py, .github/workflows/ci.yml
title: A gate that grades the developer's clone ran on every CI run, and reddened all 36
description: "`test_something_on_this_checkout_actually_invokes_the_hook` asserts that the checkout has `core.hooksPath` set or a planted `.git/hooks/pre-push`. A GitHub runner has neither and pushes nothing, so the arm failed by construction the day it was written. Every CI run since 2026-08-21 was red on it, and the six arms that grade the bundle and the pin passed in all of them."
tags: [tests, ci, gates, resolved]
status: stable
generated: { by: claude/opus-5, at: 2026-09-01T17:05:00Z }
---

# Two commits crossed, a day apart

`6c4eef3e` moved the bundle tests into the job that installs the checker, on 2026-08-20 — correct,
and the reason is in its own comment. `f6b0232a` added this arm on 2026-08-21, to a file that by
then ran in two places. It was written for one of them.

The last green run is `Move the checker pin to okf v0.5.3`, the run before. **36** consecutive red
runs followed, and the pin bump to `v0.6.1` failed with a signature byte-identical to the other 35.

# What a red that never moves costs

Nothing in eleven days of CI could report a new failure, because the job was already failing. The
`Knowledge bundle warnings` step runs after this one and never executed at all, and the whole `Unit
tests` job is `needs:` this one, so it was reported **skipped** on all 36.

The first green run after the fix found what that had been hiding.
`test_runledger.py::test_a_pass_carries_its_phase_timings` calls `index.index_project`, which is a
full pass against the real embedder, and carried no `gpu` marker — so the no-GPU lane selected it
and it died on `CPU inference is forbidden`. The marker was the fix; the test itself is sound and
passes on a GPU box. It is the only `index.index_project` caller in the suite that lacked the
marker, and `test_index.py`'s own docstring already states the rule it broke: split on the GPU
marker, not on mocks.

# The skip is scoped to the runner and to nothing else

`skipif(GITHUB_ACTIONS == "true")`, because the property is absent on a runner rather than violated
there. Proven in four states: green locally with the hook wired, skipped under a simulated runner,
and — the one that matters — still **failing** locally when `core.hooksPath` is unset, so the local
gate still bites.

CI's own copy of the gate is graded by `test_the_gate_is_wired_where_this_repo_says_it_is`, which
reads `ci.yml` for the steps and refuses a `continue-on-error` on any of them. Neither arm covers
the other, and each now runs where it can hold.
