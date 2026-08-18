---
type: Runbook
resource: .github/workflows/ci.yml
title: Running CI on the self-hosted runner
description: The GPU jobs have no checkout — they run the maintainer's live indexed tree — so the rules are about which trigger is allowed to reach them and how the run proves the bytes it tested.
tags: [ci, github-actions, self-hosted, gpu, operations]
status: active
generated:
  by: claude/claude-opus-5
  at: 2026-08-18T01:45:55Z
---

# Running CI on the self-hosted runner

Three jobs. `quality` (ruff + compile) runs on a hosted Ubuntu image and is uninteresting. The two
live jobs run `runs-on: self-hosted`, which here means the maintainer's own device with the CUDA GPU
and the daemon on it.

| Job | Trigger | Marker set | Bound |
|---|---|---|---|
| `live-fast` | push to `main` | `live and not costly and not exclusive` | 45 min, `--maxfail=5` |
| `live-slow` | `workflow_dispatch` only | `live`, then the browser step | 60 min, no failure cap |

## Runner prerequisites

`RSE_DEV_REPO` must be set in the runner's `.env` to the indexed repo path. Every step opens with
`: "${RSE_DEV_REPO:?…}"` and dies immediately without it. The path itself is deliberately not in
the workflow file — this repo is public, and a device path is exactly what
[the tracked tree is publishable](../constraints/the-tracked-tree-is-publishable.md) keeps out.

The runner also needs a CUDA GPU with no CPU fallback
([inference runs on the GPU or it fails](../constraints/inference-runs-on-the-gpu-or-it-fails.md))
and the daemon live at 127.0.0.1:8765.

## There is no checkout, on purpose

The live jobs `cd "$RSE_DEV_REPO"` and use that tree's `.venv`. They must: the suite assumes "this
repo" is a live, indexed project, and the indexed tree is the one the daemon serves. A fresh
checkout would be a different, unindexed directory.

The consequence is the thing to internalise: **the SHA GitHub labels the run with is a claim about a
commit, not a statement about the bytes that ran.** A green tick was never evidence that
`github.sha` passed. So the first step asserts it rather than logging it — it fails if `HEAD`
differs from the workflow SHA, and fails again if `git status --porcelain` is non-empty.

**The dirty-tree failure will fire in normal use, and that is correct.** Several agent sessions
develop directly in this checkout, so a push landing while any of them holds uncommitted work turns
CI red. Commit or stash, then re-dispatch. The alternative is a green tick attesting to a commit
whose code never ran.

## Why the trigger list is so short

`push` to `main`, and `workflow_dispatch`. Both require write access. There is deliberately no
`pull_request` trigger — a fork PR must never be able to start a job on the maintainer's device —
and both live jobs additionally hard-gate on the owner account, the branch, and the repository.

There is no `schedule` trigger either. A nightly cron fired **both** live jobs unattended: on a
schedule event `github.ref` is `refs/heads/main`, so `live-fast`'s gate passed too, and the run
spent real Claude session quota mid-workday. See
[no generative model runs inside the package](../constraints/no-generative-model-runs-inside-the-package.md)
for why `costly` costs money at all.

A commit-message tag used to be a second path into `live-slow`. It was removed because
`contains()` matches the whole message *including the body*, so a commit that merely mentioned the
tag in prose fired a 60-minute real-model run. That misfire happened. `workflow_dispatch` already
means "run it deliberately" and cannot be triggered by accident.

## Readiness is polled, not probed once

A cold daemon and a broken one both fail `curl -sf` with exit 7, so a single check reported "still
warming up" as "CI is red". Post-restart recovery is measured at ~21 s, ~17 s of it GPU embedder
warm-up. The step polls to 30 s and then dumps `curl -sv` output, so the two cases are
distinguishable in the log.

## Reading a red run

`live-fast` caps at `--maxfail=5`, not `-x`. Eight of the twelve runs before that change were red,
and `-x` meant each reported exactly one failure — a push that broke five things looked identical to
one that broke one, and the count you read was the count `-x` chose to show you.

`live-slow` has no cap at all, because it includes the `exclusive` tests that need a quiescent
daemon (CB3's idle-CPU window). Those are the ones most likely to fail for a reason that says
nothing about the diff, and aborting on them would hide every result behind them.

Both print `--durations=25`, and `live-fast` banks its elapsed time to the step summary on success
*and* on failure. The job's `timeout-minutes` was once set against a test set that no longer
existed, and the only reason nobody noticed is that its duration was never recorded anywhere
durable.

Running the same suite by hand: [running the live suite](running-the-live-suite.md).
