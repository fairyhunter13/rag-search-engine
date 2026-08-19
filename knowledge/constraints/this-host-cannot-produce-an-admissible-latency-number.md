---
type: Constraint
resource: src/coderag/gpu.py, tests/test_thermal.py
title: This host cannot produce an admissible latency number while it throttles
description: The laptop's GPU runs at a sixth of rated clock, so every timing figure measured here measures the host rather than the engine -- and the cause is a power cap, not the heat the first reading blamed.
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

# Amendment, 2026-08-19 — the claim holds, the cause was wrong

Everything above about *admissibility* stands. The **diagnosis does not**, and the way it failed is
the part worth keeping.

Re-measured the same day under the same sustained load:

| | first reading | re-measured | live throttle reason |
|---|---|---|---|
| GPU temp | 88–89 °C, pinned | **66–70 °C** | — |
| CPU / chassis | 96 °C / 97 °C | **68–71 °C / 69–71 °C** | — |
| `SW Thermal Slowdown` | "Active, 330,867 s" | — | **Not Active** |
| `HW Thermal Slowdown` | — | — | **Not Active** |
| `SW Power Cap` | — | — | **Active** |
| power | 50–71 W | 75–80 W against an **80 W** cap | `power.max_limit` = **175 W** |

**`nvidia-smi` prints two blocks that look alike and are not.** `Clocks Event Reasons` is
instantaneous state; `Clocks Event Reasons Counters` is a lifetime accumulator in microseconds. The
"Active, 330,867 s" above is the *counter* — about 93 hours summed over the life of the card — read
as if it described the current moment. The live block said Not Active the whole time. An error of
this shape does not read as a guess: it arrives with a unit and six significant figures, and that is
exactly why it survived unchallenged.

The real constraint is that the board is pinned to its **80 W default cap on a 175 W part** — 46%.
`Notebook Dynamic Boost: Supported`, but nothing negotiates the budget, so the card sits on its floor.

So the sentence above — "the card is not power-limited" — is precisely inverted. It is *only*
power-limited.

# Amendment: two fixes that cannot work here, and one that can

The closing paragraph proposes `nvidia-smi -pl`. **It cannot work on this machine.** Laptop GPUs
return *"Changing power management limit is not supported in current scope"*; the knob is not
root-gated, it is absent. `power-profiles-daemon` is the other dead end —
`/sys/firmware/acpi/platform_profile` does not exist here, which is why PPD reports
`PlatformDriver: placeholder`, and it therefore has no channel to the dGPU at all.

The lever that exists is **`nvidia-powerd`**, the daemon that negotiates Dynamic Boost. Ubuntu 24.04's
`nvidia-kernel-common-595` ships the binary at `/usr/bin/nvidia-powerd` but installs **neither** its
systemd unit (doc-only, under `/usr/share/doc/`) **nor** any D-Bus policy — so the service does not
exist until it is assembled by hand, and without the policy it starts and then fails to own
`nvidia.powerd.server`. Both files placed, the enforced limit went **80 W → 130 W** against a 175 W
maximum. See [restoring Dynamic Boost](../runbooks/restoring-dynamic-boost.md), which also names the
`nvidia-smi` field that reads `[N/A]` on a working system.

What this changes for measurement: **recall verdicts taken before the fix remain valid** — all arms
were capped equally and recall@k is not a timing metric, which is what the original entry got right
for the wrong reason. **Latency verdicts stay inadmissible**, and the reason changed under the fix.

# Second amendment — lifting the cap moved the constraint, it did not remove it

Sampled over three minutes at the new 130 W limit, under the same sustained load:

| | at 80 W | at 130 W |
|---|---|---|
| power draw | 75–80 W (pinned at the cap) | **83–90 W** |
| SM clock | 435–1507 MHz | 945–1492 MHz |
| GPU temp | 66–70 °C | **87 °C** |
| `SW Power Cap` | **Active** | Not Active |
| `SW Thermal Slowdown` | Not Active | **Active** |

Draw above 80 W is the proof the fix engaged. But the card spends the new headroom into heat and
runs into the thermal limit, so the throttle is now the one the original entry named — at 87 °C,
against a 3090 MHz rating it still does not approach.

So the first amendment's correction was right about **why the card was slow in that window**, and
wrong to imply heat was never a constraint here. The truthful statement is sequential: **power was
binding at 80 W; heat is binding at 130 W.** A diagnosis that names one throttle reason is only
valid for the power limit it was taken at, and both readings above are correct at their own.

The consequence for this repo is unchanged from the original entry, by a different route: **this
host still cannot produce an admissible latency number**, and the sqlite-vec kill criterion stays
un-testable here. What it can produce is arm-to-arm recall, and that is what the bake-off reads.
