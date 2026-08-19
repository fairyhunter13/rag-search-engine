"""Paired comparison of bake-off arms, from the ranks rather than the aggregates.

Two arms differing by 0.02 recall@10 differ by ~7 queries in 300, which is well
inside what an unpaired eyeball on four decimals can resolve. The arms are run
over the *same* queries, so the pairing is free and it is the whole power of the
test: McNemar reads only the queries where the two arms disagree, and the
bootstrap resamples queries rather than treating the two runs as independent.

Every arm is compared against one baseline, so this is k tests and not one --
Benjamini-Hochberg over the p-values, reported alongside the raw ones.

    python tests/significance.py /tmp/coderag-eval7/results.json --baseline nomic
"""

from __future__ import annotations

import argparse
import json
import random
from math import comb
from pathlib import Path

K = 10
RESAMPLES = 10_000


def _hits(ranks: list[int | None]) -> list[int]:
    return [1 if r is not None and r <= K else 0 for r in ranks]


def mcnemar(a: list[int], b: list[int]) -> tuple[int, int, float]:
    """Exact two-sided binomial on the discordant pairs.

    Exact rather than the chi-square approximation: the discordant count here is
    tens, not hundreds, and the approximation is the one that overstates
    significance in exactly that regime.
    """
    won = sum(1 for x, y in zip(a, b, strict=True) if x and not y)
    lost = sum(1 for x, y in zip(a, b, strict=True) if y and not x)
    n = won + lost
    if n == 0:
        return won, lost, 1.0
    tail = sum(comb(n, i) for i in range(min(won, lost) + 1)) / 2**n
    return won, lost, min(1.0, 2 * tail)


def bootstrap(a: list[int], b: list[int], seed: int = 0) -> tuple[float, float, float]:
    """Percentile CI on the paired difference in recall@10.

    Resampling *queries* and re-scoring both arms on the same draw is what keeps
    the pairing; resampling each arm separately would measure two independent
    runs and widen the interval for no reason.
    """
    rng = random.Random(seed)
    n = len(a)
    deltas = []
    for _ in range(RESAMPLES):
        idx = [rng.randrange(n) for _ in range(n)]
        deltas.append(sum(a[i] - b[i] for i in idx) / n)
    deltas.sort()
    point = sum(x - y for x, y in zip(a, b, strict=True)) / n
    return point, deltas[int(0.025 * RESAMPLES)], deltas[int(0.975 * RESAMPLES) - 1]


def benjamini_hochberg(pvalues: list[float]) -> list[float]:
    """Step-up adjusted p-values, monotone from the largest down."""
    order = sorted(range(len(pvalues)), key=lambda i: pvalues[i])
    adjusted = [0.0] * len(pvalues)
    running = 1.0
    for rank, i in reversed(list(enumerate(order, start=1))):
        running = min(running, pvalues[i] * len(pvalues) / rank)
        adjusted[i] = running
    return adjusted


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", type=Path)
    parser.add_argument("--baseline", default="nomic")
    args = parser.parse_args(argv)

    rows = {r["arm"]: r for r in json.loads(args.results.read_text()) if "ranks" in r}
    if args.baseline not in rows:
        parser.error(f"{args.baseline} carries no ranks in {args.results}; re-run that arm")
    base = _hits(rows[args.baseline]["ranks"])

    comparisons = []
    for arm, row in rows.items():
        if arm == args.baseline:
            continue
        hits = _hits(row["ranks"])
        won, lost, p = mcnemar(hits, base)
        point, low, high = bootstrap(hits, base)
        comparisons.append((arm, point, low, high, won, lost, p))

    adjusted = benjamini_hochberg([c[-1] for c in comparisons])
    print(f"baseline {args.baseline}: recall@{K} {sum(base) / len(base):.4f}, n={len(base)}")
    print(f"{'arm':<16}{'delta':>9}{'95% CI':>20}{'win/lose':>11}{'p':>9}{'p(BH)':>9}")
    for (arm, point, low, high, won, lost, p), q in zip(comparisons, adjusted, strict=True):
        ci = f"[{low:+.4f},{high:+.4f}]"
        print(f"{arm:<16}{point:>+9.4f}{ci:>20}{f'{won}/{lost}':>11}{p:>9.3f}{q:>9.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
