# The full suite was red in the record and green in fact

**2026-08-05** · mechanism: `.github/workflows/ci.yml` `live-slow`

`live-slow` is `workflow_dispatch`-only, deliberately: a nightly cron once fired **both** live jobs
unattended, because on a `schedule` event `github.ref` is `refs/heads/main` and `live-fast`'s gate
passed too. The comment in `ci.yml` is that incident.

The cost of that decision is that the job's state is only as fresh as the last time someone pressed
the button. `gh run list --workflow ci.yml --event workflow_dispatch` returned exactly one run:
**2026-07-14, `failure`** — three weeks old, and the most recent thing CI could say about the
`costly` and `exclusive` tests and the browser suite, none of which any push run reaches
(`live-fast` selects `-m "live and not costly and not exclusive"`).

Dispatched on `b01a7b2`. All four jobs green:

| job | result |
| --- | --- |
| Live Tests Slow — full `-m live` | **913 passed** in 8m20s |
| Live Tests Slow — browser | **35 passed** in 1m46s |
| Live Tests Fast | 891 passed in 5m08s |
| Code Quality / Test Collection | success |

913 against 891 is the 22 tests a push run deselects. So the stale red was bookkeeping: nothing in
the unreached set was broken, and the record was the only thing that said otherwise.

## Why no recency check was added

The obvious follow-through — assert that a successful `live-slow` run exists and is recent, the
shape of the private repo's NBL6 — is **declined here**, and the difference is what the workflow is.
NBL6 guards a *scheduled* workflow: "it should have run and did not" is a real fault with a real
answer. `live-slow` is manual by design, so the same assertion says "a human has not pressed a
button lately", which is not a property of the code and would turn every push red for an
operational habit. It would also fail on any clone with no run history.

What is true instead, and is the reason the gap was survivable: `live-fast` runs on **every push**
and covers 891 of 913 tests. The residue is 22 tests whose own cost — real Claude session quota
(`costly`) and a quiescent daemon (`exclusive`) — is precisely why they are not on the push path.
The honest control for them is to dispatch the run when that set changes, and to read the run's
conclusion rather than the absence of a failure notification.

Recorded so the next person finds the 2026-07-14 red already explained rather than alarming.
