# The idle gate was measuring a neighbour, and underneath that it has almost no headroom

**2026-07-31** · P16 / P17 / HR37 · guards: `test_cpu_budget.py` CB3, `test_watcher_dispatch.py` HL7

CB3 asserts the daemon's own ΔCPU/Δwall stays under 1% of one core with sweeps quiescent. It was
failing. Two separate things were true, and only the first is a defect in this repo.

## The probe could not see the work it was charging for

CB3 discards a window in which the watcher was busy, and "busy" meant `dispatched`, `inflight`,
`pending` — three counters that between them describe only work the watcher **accepted**. An event
`_filter` *rejects* is invisible to all three. It is still an event the kernel delivered and still
cost a full `is_ignored_path` to classify, so a root churning entirely inside an ignored subtree
read as perfectly idle and the CPU went on P16's bill.

Measured across CB3's own five windows while a neighbouring agent profile wrote into an ignored
worktree subtree of a watched root:

| `filtered` Δ | cgroup, 20 s window |
|---:|---:|
| 1 | 0.0211 core |
| 124 | 0.1033 core |
| 166 | 0.1410 core |
| 156 | 0.1409 core |
| 52 | 0.0541 core |

Near-linear, and up to **fourteen times the gate**. Across all five, `dispatched` moved by at most
4 and `inflight`/`pending` never left zero — every one of those windows read as quiet on the old
three-counter probe. A separate 40 s capture put 414 of 416 delivered events in a **single** ignored
directory; none of them reached an index. With the neighbour quiet the same daemon's reader thread
ran at 0.0010 of a core over 60 s against 24 events.

So `filtered` joins the tuple and the 1% threshold is untouched. That is the whole shape of the fix:
it corrects what the test can *see*, never what it demands. Where the churn does not stop, the five
attempts now run out and CB3 reports "no quiet window" rather than a bogus CPU figure — still red,
because the measurement could not be taken.

`filtered` is a **lower bound**, not a census: watchfiles coalesces each batch by path in Rust
before anything crosses into Python, so a tight rewrite of one file arrives as one event. Enough for
a contamination probe, which has to answer "was anything happening", not "how much".

It is incremented without taking `_cv`, deliberately. That is the per-event reader hot path, and
taking the dispatch lock once per inotify event is the storm HR37 exists to prevent. A bare `int`
increment is atomic under the GIL, which is all a monitoring counter needs.

## Underneath it, the floor is 0.9% against a 1.0% gate

With the probe fixed and the host genuinely quiet — `filtered`, `dispatched`, `inflight`, `pending`
all flat — CB3 still measured **0.0111 of a core**. That one is real, and it is not contamination.

Per-thread attribution over five windows, taking the two in which every watcher counter stayed flat
(both totalled 0.0090 in threads against a 0.0092–0.0104 cgroup reading):

| thread | core | what it is |
|---|---:|---|
| main / event loop | 0.0055 | uvicorn + the daemon's own loop |
| `notify-rs inotify` | ~0.0020 | steady state; far higher while arming |
| watcher reader | 0.0023 | |
| scheduler | 0.0014 | |
| `cuda-EvtHandlr` | 0.0006 | |
| **total** | **~0.0090** | against a 0.0100 gate |

A bare uvicorn app with a two-line ASGI handler, measured in the same interpreter, idles at
**0.0020 of a core**: `Server.main_loop` runs an unconditional `await asyncio.sleep(0.1)` + `on_tick`
forever. Roughly a fifth of the floor is the web server ticking, and it is not ours to remove.

**Two candidate reductions were measured and both refused.** Raising watchfiles' `step` from its
50 ms default looked obvious — the reader crosses into Python to check the stop event each tick — but
the caller thread reads 0.0000 at every step value from 50 ms to 1000 ms, so there is nothing there
to buy. An earlier apparent `step` curve (0.0275 → 0.0035) did not replicate and was an artifact:
arming 111,800 inotify watches across 152 roots is itself substantial work, and a 6 s settle does not
cover it, so the measurement was reading the tail of arming rather than steady state.

## What this leaves

P16 is met, and CB3 is now able to say so. But the gate sits about 10% above the floor on a
152-root fleet, and the floor is dominated by a dependency's fixed tick and by inotify bookkeeping
that scales with watch count — neither of which is the daemon's own logic doing anything wasteful.

That is a judgment about the invariant, not a bug to fix, so nothing here changes the threshold.
The transferable points:

- **A contamination probe must count rejected work, not just accepted work.** Any counter that only
  describes what a component chose to do will charge its neighbours' costs to the component.
- **Watch-count growth erodes this gate silently.** The floor moves with the fleet; a re-derive or a
  large re-arm is not measurable idle at all.
- **Re-measure a suspiciously clean curve before acting on it.** The `step` result was monotone,
  plausible, and entirely an artifact of arming.
