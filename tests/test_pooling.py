"""Pooling is per-model, and every pooling produces a plausible unit vector.

That is the whole reason these are written: a CLS-trained model read at the
masked mean does not fail, it scores -- and it scores low enough to look like a
verdict on the model. The fixture below makes the two poolings disagree by
construction, so an assertion cannot be satisfied by both.
"""

import numpy as np
import pytest

from coderag import config, embed


@pytest.fixture
def hidden():
    """CLS carries a signal the rest of the sequence does not.

    Row 0's mean is the zero-ish average of its tail; its CLS is the one-hot
    first dimension. A uniform fixture would let a broken dispatch pass both
    ways, which is what makes this shape load-bearing rather than arbitrary.
    """
    h = np.zeros((2, 4, 3), dtype=np.float32)
    h[:, 0, :] = [1.0, 0.0, 0.0]  # CLS
    h[:, 1:, :] = [0.0, 1.0, 0.0]  # every real token
    return h


@pytest.fixture
def mask():
    # The last position is padding, and it is the position a broken mean would
    # average in. Row 1 pads two, so the two rows disagree under a plain mean.
    return np.array([[1, 1, 1, 0], [1, 1, 0, 0]], dtype=np.int64)


def test_cls_reads_the_first_position(hidden, mask, monkeypatch):
    monkeypatch.setattr(config, "EMBED_POOLING", "cls")
    out = embed._pool(hidden, mask)
    assert np.allclose(out, [[1.0, 0.0, 0.0]] * 2)


def test_mean_reads_the_tokens_and_not_the_cls(hidden, mask, monkeypatch):
    monkeypatch.setattr(config, "EMBED_POOLING", "mean")
    out = embed._pool(hidden, mask)
    # Dimension 1 dominates because the real tokens carry it; dimension 0 is
    # the CLS contribution, present but never the whole vector.
    assert out[0][1] > out[0][0] > 0.0


def test_the_two_poolings_disagree_on_this_fixture(hidden, mask, monkeypatch):
    """The negative control on the fixture itself.

    If these ever came out equal, every other test in this file would pass with
    the dispatch deleted.
    """
    monkeypatch.setattr(config, "EMBED_POOLING", "cls")
    cls = embed._pool(hidden, mask)
    monkeypatch.setattr(config, "EMBED_POOLING", "mean")
    mean = embed._pool(hidden, mask)
    assert not np.allclose(cls, mean)


def test_mean_ignores_padding(monkeypatch):
    """The same two tokens embed identically at two different pad widths.

    Padding has to be compared across batches, not across rows of one batch:
    two rows of one array with different mask sums are two different texts.
    The pad positions carry a large junk activation, because a real padding
    token does -- a zero-filled pad passes with the mask ignored entirely.
    """
    monkeypatch.setattr(config, "EMBED_POOLING", "mean")
    real = np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]], dtype=np.float32)

    tight = embed._pool(real[None, :, :], np.ones((1, 2), dtype=np.int64))
    padded = np.concatenate([real, np.full((3, 3), 7.0, dtype=np.float32)])
    loose = embed._pool(padded[None, :, :], np.array([[1, 1, 0, 0, 0]], dtype=np.int64))

    assert np.allclose(tight, loose)


def test_every_pooling_returns_unit_vectors(hidden, mask, monkeypatch):
    for mode in ("cls", "mean"):
        monkeypatch.setattr(config, "EMBED_POOLING", mode)
        out = embed._pool(hidden, mask)
        assert np.allclose(np.linalg.norm(out, axis=1), 1.0)


def test_truncation_composes_with_cls(hidden, mask, monkeypatch):
    """Matryoshka's renormalise has to run on the CLS branch too.

    The truncation lives after the dispatch for exactly this reason; a copy
    inside the mean branch would leave CLS vectors un-normalised after the cut.
    """
    monkeypatch.setattr(config, "EMBED_POOLING", "cls")
    monkeypatch.setattr(config, "EMBED_TRUNCATE_DIMS", 2)
    out = embed._pool(hidden, mask)
    assert out.shape == (2, 2)
    assert np.allclose(np.linalg.norm(out, axis=1), 1.0)


def test_default_is_mean(monkeypatch):
    """The incumbent's pooling is the default, so an unset env is not a change."""
    monkeypatch.delenv("CODERAG_EMBED_POOLING", raising=False)
    assert config._env("EMBED_POOLING", "mean") == "mean"
