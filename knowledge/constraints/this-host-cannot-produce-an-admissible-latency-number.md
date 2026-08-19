---
type: Constraint
resource: src/coderag/gpu.py, tests/test_thermal.py
title: This host cannot produce an admissible latency number while it throttles
description: The laptop runs its GPU three degrees past its own throttle point at roughly a sixth of rated clock, so every timing figure measured here is a thermal measurement rather than an engine one.
tags: [thermal, measurement, latency, kill-criterion]
status: stable
generated: { by: claude/opus-5, at: 2026-08-19T12:45:00Z }
---

# Constraint

Measured 2026-08-19 mid-bake-off, sampled over a minute rather than read once:

| | measured | rated |
|---|---|---|
| SM clock | 285–1050 MHz, mean ~460 | **3090 MHz** |
| Power draw | 50–71 W | **175 W max** (80 W default cap) |
| GPU temp | 88–89 °C, pinned | — |
| `GPU T.Limit` | **−3 °C** | 0 = at the throttle point |
| `SW Thermal Slowdown` | **Active**, 330,867 s lifetime | — |
| CPU (`coretemp`) / chassis (`acpitz`) | **96 °C / 97 °C** | — |
| fans | 3,934 / 4,000 RPM | — |

`T.Limit` reports *headroom*, so −3 means three degrees past the throttle point, with Slowdown at
−2 and Shutdown at −5. The card is not power-limited — it draws 60 W of an available 175 and cannot
spend the rest, because the chassis is saturated and the CPU is at 96 °C competing for the same two
fans.

The consequence is not "it is slow". It is that **a timing number taken here measures the laptop's
cooling, not this engine.** Utilization still reads 100%, which is what makes the state easy to
mistake for a busy card: utilization counts whether the SMs have work queued, never how fast they
retire it.

# What this invalidates, by name

* the **sqlite-vec kill criterion** — a scoped p95 above ~200 ms reverses that decision, and this
  host cannot currently tell a slow scan from a throttled one. See
  [the sqlite-vec decision](../decisions/sqlite-vec-survives-only-because-search-is-scoped.md).
* the **live suite's** `scoped_p50_ms` / `scoped_p95_ms`, recorded via `record_property` in
  `tests/test_live_results.py`. They are still worth recording; they are not worth acting on.
* every **per-arm wall clock** in the embedder bake-off (~33 min per arm).

What survives is arm-to-arm **recall**: all arms throttle equally, and recall@k is not a timing
metric. A quality verdict from this machine is admissible; a latency verdict is not.

# Why the fix is not a lighter model

A smaller encoder cuts total joules, linearly and genuinely. It cannot make a saturated chassis
dissipate more heat, and it does not move 89 °C. The throttle costs roughly **6×**, which is an
order of magnitude more than the spread between any two encoders on the shortlist — so a model swap
argued on thermal grounds is optimising the smaller term.

`gpu.cool_down()` is the half this repo owns: a bounded wait between flushes, outside every
transaction and the GPU lock, because an index has no deadline and a hot card should index slowly
rather than not at all. The half it does not own is the power cap — `nvidia-smi -pl` needs root, and
2026's power-capping literature converges on ~70% TDP holding ~93% of throughput. A *deliberate* cap
also beats this involuntary one for measurement, because an erratic clock is what makes a number
unrepeatable.
