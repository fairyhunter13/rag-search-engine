"""Matryoshka truncation, which is pure numpy and needs no card.

The failure this guards is silent by construction: a truncated vector still has
the right shape, still ranks, and is only wrong by how much norm each chunk
happened to keep in its first 256 dimensions. Shape assertions pass through it,
so every assertion here is about the norm.
"""

from __future__ import annotations

import numpy as np
import pytest

from coderag import config
from coderag.embed import _mean_pool


def _hidden(rows: int = 3, tokens: int = 5, dims: int = 768) -> tuple:
    rng = np.random.default_rng(0)
    # Front-loaded so the first 256 dims carry a different share of the norm in
    # every row; a uniform fixture would pass with the renormalise removed.
    hidden = rng.normal(size=(rows, tokens, dims)).astype(np.float32)
    hidden[:, :, :256] *= np.array([[0.1], [1.0], [9.0]], dtype=np.float32)[:, :, None]
    return hidden, np.ones((rows, tokens), dtype=np.int64)


def test_untruncated_output_keeps_its_width(monkeypatch):
    monkeypatch.setattr(config, "EMBED_TRUNCATE_DIMS", 0)
    assert _mean_pool(*_hidden()).shape == (3, 768)


def test_truncation_cuts_to_the_requested_width(monkeypatch):
    monkeypatch.setattr(config, "EMBED_TRUNCATE_DIMS", 256)
    assert _mean_pool(*_hidden()).shape == (3, 256)


def test_every_truncated_vector_is_renormalised(monkeypatch):
    """The one that fails if the cut lands after the normalise instead of
    before it -- which is the natural way to write this change."""
    monkeypatch.setattr(config, "EMBED_TRUNCATE_DIMS", 256)
    norms = np.linalg.norm(_mean_pool(*_hidden()), axis=1)
    assert np.allclose(norms, 1.0, atol=1e-5), norms


def test_the_fixture_would_expose_a_missing_renormalise(monkeypatch):
    """A negative control on the fixture itself. If truncating this input left
    near-unit norms anyway, the test above would pass over the broken code."""
    monkeypatch.setattr(config, "EMBED_TRUNCATE_DIMS", 0)
    full = _mean_pool(*_hidden())
    naive = np.linalg.norm(full[:, :256], axis=1)
    assert not np.allclose(naive, 1.0, atol=1e-2), naive


def test_truncation_is_a_prefix_of_the_full_vector(monkeypatch):
    """Matryoshka's actual claim. A cut that took any other slice would satisfy
    both the shape and the norm assertions above."""
    monkeypatch.setattr(config, "EMBED_TRUNCATE_DIMS", 0)
    full = _mean_pool(*_hidden())
    monkeypatch.setattr(config, "EMBED_TRUNCATE_DIMS", 256)
    cut = _mean_pool(*_hidden())

    expected = full[:, :256] / np.linalg.norm(full[:, :256], axis=1, keepdims=True)
    assert np.allclose(cut, expected, atol=1e-5)


@pytest.mark.parametrize("dims", [64, 128, 512])
def test_the_trained_rungs_all_round_trip(dims, monkeypatch):
    monkeypatch.setattr(config, "EMBED_TRUNCATE_DIMS", dims)
    out = _mean_pool(*_hidden())
    assert out.shape == (3, dims)
    assert np.allclose(np.linalg.norm(out, axis=1), 1.0, atol=1e-5)
