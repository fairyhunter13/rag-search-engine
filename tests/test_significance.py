"""The paired tests that decide the bake-off, checked against hand values."""

from __future__ import annotations

import significance


def test_mcnemar_reads_only_the_disagreements():
    """The whole reason the arms share a query set: 290 queries both arms get
    right carry no information about which arm is better."""
    a = [1] * 5 + [0] * 5 + [1] * 290
    b = [0] * 5 + [0] * 5 + [1] * 290

    won, lost, p = significance.mcnemar(a, b)

    assert (won, lost) == (5, 0)
    assert round(p, 4) == 0.0625, "exact two-sided binomial on 5 discordant pairs"


def test_two_identical_arms_are_not_distinguishable():
    """The null the plan expects, and the one a four-decimal eyeball fails."""
    ranks = [1, None, 3, 11, 2] * 60
    hits = significance._hits(ranks)

    _won, _lost, p = significance.mcnemar(hits, hits)
    point, low, high = significance.bootstrap(hits, hits)

    assert p == 1.0 and point == 0.0
    assert low == high == 0.0, "a paired resample of an arm against itself has no spread"


def test_a_rank_past_k_is_a_miss():
    """recall@10 and not recall@whatever-the-run-returned: `search` is called
    with k=K, but a rank of 11 in a stored run must not read as a hit."""
    assert significance._hits([1, 10, 11, None]) == [1, 1, 0, 0]


def test_the_bootstrap_is_paired_not_two_independent_runs():
    """Resampling each arm separately loses the pairing, and the interval it
    produces spans zero on data where every single query moved one way."""
    a, b = [1] * 200, [0] * 100 + [1] * 100

    point, low, high = significance.bootstrap(a, b)

    assert round(point, 3) == 0.5
    assert low > 0.4 and high < 0.6, (low, high)


def test_the_correction_is_across_the_arms_not_within_one():
    """Five arms against one baseline is five tests, and the raw p-values are
    the ones that would call one of them a winner by arithmetic alone."""
    raw = [0.01, 0.02, 0.03, 0.04, 0.05]

    adjusted = significance.benjamini_hochberg(raw)

    assert adjusted == sorted(adjusted), "step-up must stay monotone"
    assert all(q >= p for q, p in zip(adjusted, raw, strict=True))
    assert round(adjusted[0], 4) == 0.05
