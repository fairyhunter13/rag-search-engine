# The bit-lane threshold, and the three times it was re-validated

**`BIN_MIN_CHUNKS = 12_000`** in `src/rag_search/index/store.py` — below it, `search` stays on the
exact float32 scan instead of the coarse-bit-index-then-rescore two-stage lane.

The rule is in the code. This is the evidence behind it, and the record of why it has been left
alone twice since.

## Why there is a threshold at all

The rescore is priced per candidate and is near-constant in store size: vec0 fetches each
shortlisted vector by rowid, and one batched statement measured identical to 40 point queries
(13.6 ms either way), so it cannot be amortised. The exact scan grows with the store. Measured:
**3.0 ms exact against 8.5 ms two-stage at 2,536 chunks; 152 ms against 24 ms at 111,918.**

Ignoring that made a 193-member federated query **36% slower** even as its largest member got 1.20x
faster — 97 of 139 stores were paying the overhead to lose. That regression is what commit
`ba1bc86` was written for.

12,000 sits above the 6k–12k band, where repeated runs put the two lanes within ±30% of each other
in both directions. The crossover is real but not sharp, and the wrong side of it costs ~5 ms.

## Re-validation, 2026-07-30

The fleet had shrunk from 2,203,331 chunks to 376,672, retiring every store the numbers above were
taken on — the largest was now 27,974, down from 106,685. ABA over a real 193-member federated
query, medians of 5, drift-adjudicated:

- **The gate is load-bearing.** Ungated (bit lane on all 139 stores) measured **7.24 s against
  4.12 s gated — 1.85x slower**, a gap 26x the A-to-A' drift. `ba1bc86`'s regression is still live
  if the threshold goes away.
- **The lane itself is now unmeasurable.** Gated against exact-only came back "no claim" on 3 of 4
  queries (gap under drift), and on the one member store above the threshold all four arms tied on
  medians (0.598 / 0.595 / 0.597 / 0.634). A first pass reporting min-of-5 on one query looked like
  a clean 0.91x win for exact; four queries showed that as the session's warming trend — A' beat B
  on 3 of 4 — so it was withdrawn rather than shipped.

So 12,000 is kept and **not** re-tuned. With no store big enough to make two-stage win, there is no
signal left to tune against, and the value's only remaining job is to keep small stores off the
lane, which it does. Deleting the lane is not supported either: nothing measured shows it costing
anything, and removal would re-expose the next store above 100k.

## Re-check, 2026-07-31

The corpus-hygiene purge took 56,978 chunks out (13.42% of the fleet), which looked like grounds to
re-derive the threshold a fourth time. It was not. The purge landed the fleet on 367,718 chunks
against the 376,672 the section above was measured at — 2.4% apart — with the largest store at
28,251 against 27,974, 1.0% apart, and still nothing above 100k. 8 stores sat above the threshold
and 11 in the 6k–12k band.

The distribution the numbers were taken on is the distribution we still have, so re-running the
sweep would have spent an hour reproducing the documented "no claim". **Checking where the stores
are is the cheap question; re-measuring the crossover is only worth it once one of them moves** —
specifically, once a store in the 100k range comes back, which is the regime the 152 ms-against-24 ms
figure describes.
