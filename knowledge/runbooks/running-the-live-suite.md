---
type: Runbook
resource: src/tests/live/
title: Running the live suite
description: The suite has no mocks, so it needs the real daemon and the real GPU — one run at a time, in the foreground, with VRAM checked before any failure is read as a code problem.
tags: [testing, gpu, daemon, operations]
status: active
generated:
  by: claude/claude-opus-5
  at: 2026-08-18T01:45:55Z
---

# Running the live suite

The invocations live in the `run-tests` and `run-all-tests` skills, so there is one copy of the
flags to keep correct. This is the part the skills cannot hold: what goes wrong, and what it looks
like when it does.

## Before you start

```bash
curl -s localhost:8765/healthz
systemctl --user is-active rag-search-mcp-daemon
```

Read `/healthz` **before** you restart anything — see hazard 1.

## Markers

| Marker | Meaning |
|---|---|
| `live` | needs the daemon at :8765 and a GPU. Every test carries it. |
| `costly` | spends real Claude session quota through `claude -p`. 19 tests. |
| `exclusive` | must not run beside other load — either it measures a quiet daemon (CB3, CB4, CB6) or it destroys quiet (the reload restart). 4 tests. |

`slow` is retired and `conftest.py` no longer registers it, so `--strict-markers` refuses a re-add.
**Skipping is forbidden** (`test_no_skip_markers_in_live_suite`): a marker chooses which suite a
test belongs to; it never lets one pass without running.

Inner loop, not a CI gate:

```bash
.venv/bin/pytest $(.venv/bin/python scripts/affected_tests.py)
```

A missed impact edge would silently shrink that set, so CI always runs everything.

## Hazard 1 — a restart silently unpauses the suite

The suite holds a sweeps pause lease so the daemon does not compete with it for the GPU.
`POST /api/reload` respects that lease with a 409. **`systemctl --user restart` does not**, and the
damage is silent and permanent for the run: the lease lives in module globals, so a restart clears
it with no refusal and no log naming the suite it just unpaused.

The suite renews after every test, but only while the lease is still non-zero — a lease at zero
means the mechanism decided to resume, and re-arming would race that decision. So an externally
cleared lease never comes back. Measured 2026-07-31: a run spent its whole remainder at
`sweeps_pause_lease_s: 0.0` with sweeps competing for the same GPU throughout.

If you cleared one, re-`POST /api/sweeps/pause`. `previously_paused: false` on the re-pause is what
proves the lease was gone rather than merely unreported.

## Hazard 2 — one live suite at a time, and no `flock`

`pytest_configure` aborts naming the other run's pid and profile. Do not wrap runs in `flock`; the
reasoning is
[one live suite at a time](../../docs/decisions/2026-07-30-one-live-suite-at-a-time.md). Three agent
profiles share this checkout, so overlapping runs are the normal failure, not an exotic one.

## Hazard 3 — foreground only

The suite loads a real embedder in-process: about 1 GB RSS and a full core, intrinsic to the no-mock
invariant. As an unattended background task it stacks on whatever else is running and pushes the
machine into swap.

## When dozens of tests fail inside onnxruntime

**Check free VRAM before you read the diff.** `CUBLAS failure 3` and `BFCArena` errors name neither
the GPU nor the daemon, so they read as a code regression. They are usually the daemon holding its
arena while the suite tries to load its own embedder and reranker on the same card.

```bash
curl -s -X POST localhost:8765/api/gpu/release
```

Then re-run. Measured: 60 failures became 623 passed with nothing else changed. Background:
[inference runs on the GPU or it fails](../constraints/inference-runs-on-the-gpu-or-it-fails.md) and
[VRAM starvation](../../docs/decisions/2026-07-29-vram-starvation.md).

## After a stamp move

Different procedure — see [moving the pipeline stamp](moving-the-pipeline-stamp.md).
