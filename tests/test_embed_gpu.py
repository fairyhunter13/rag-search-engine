"""The real models on the real GPU. No mocks, by policy.

These are slow and they are the only tests that can prove the inference path
works: every failure this module has ever had -- CPU fallback, a missing
prefix, a session that loaded and would not run -- is invisible to a fake.
"""

from __future__ import annotations

import subprocess

import numpy as np
import pytest
from onnxruntime.capi.onnxruntime_pybind11_state import Fail as OrtFail

from coderag import config, embed, gpu

pytestmark = pytest.mark.gpu

DOCS = [
    "def parseUserConfig(path):\n    return json.load(open(path))",
    "func renderTable(rows []Row) string { return html }",
    "SELECT id, email FROM users WHERE active = 1",
]
QUERY = "how do I read the user configuration file"
PREFIX = "search_query: "


def _vram_mib() -> int:
    out = subprocess.run(
        ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
        capture_output=True,
        text=True,
        check=True,
    )
    return int(out.stdout.strip().splitlines()[0])


def test_the_embedder_runs_on_the_gpu():
    assert embed.get_embedder().session.get_providers()[0] in gpu.GPU_EPS


def test_the_reranker_runs_on_the_gpu():
    """Checked separately: it is the larger model and the one that quietly
    landed on CPU when only the embedder was verified."""
    assert embed.get_reranker().session.get_providers()[0] in gpu.GPU_EPS


def test_vectors_are_the_declared_width_and_normalised():
    vectors = embed.get_embedder().embed(DOCS, side="document")
    assert vectors.shape == (3, 768) and vectors.dtype == np.float32
    assert np.allclose(np.linalg.norm(vectors, axis=1), 1.0, atol=1e-4)


def test_the_right_document_wins_on_a_natural_language_query():
    e = embed.get_embedder()
    sims = e.embed(DOCS, side="document") @ e.embed([QUERY], side="query")[0]
    assert int(sims.argmax()) == 0


def test_the_sides_differ_exactly_when_the_prefixes_do():
    """The shipping model is trained with no prefixes, so the two sides must
    agree -- and must stop agreeing the moment a prefix is reintroduced. Half
    of this is the old +0.062 assertion, kept: if a prefix could not move the
    vector, that gain would be unobtainable and nothing would report it."""
    assert config.QUERY_PREFIX == "" and config.DOCUMENT_PREFIX == ""
    e = embed.get_embedder()
    same = e.embed([QUERY], side="query")[0] @ e.embed([QUERY], side="document")[0]
    assert float(same) > 0.999
    with_prefix = e.embed([f"{PREFIX}{QUERY}"], side="query")[0]
    assert float(with_prefix @ e.embed([QUERY], side="query")[0]) < 0.999


@pytest.mark.parametrize("side", ["banana", "", "Document", "passage", None])
def test_an_unknown_side_raises_rather_than_guessing(side):
    """Embedding a query into the document space is silent, plausible, and
    costs a full re-embed to discover."""
    with pytest.raises(ValueError, match="side must be"):
        embed.get_embedder().embed(["x"], side=side)


def test_empty_input_is_an_empty_matrix_not_a_crash():
    assert embed.get_embedder().embed([], side="query").shape == (0, 768)


def test_batching_matches_a_single_pass(monkeypatch):
    """A chunk's vector must not depend on how much padding its batch needed."""
    e = embed.get_embedder()
    texts = DOCS * 3
    one = e.embed(texts, side="document")
    monkeypatch.setattr(e, "batch", 2)
    many = e.embed(texts, side="document")
    assert np.allclose(one, many, atol=1e-3)


def test_an_allocator_failure_halves_the_batch_instead_of_ending_the_build(monkeypatch):
    """The batch is sized from a per-item guess about a model this code has not
    seen; on nomic that guess was ~10x under and the first batch OOMed. Without
    the retry an overnight fleet index dies on file one."""
    e = embed.get_embedder()
    monkeypatch.setattr(e, "batch", 8)
    real, calls = e._forward, []

    def once_too_big(window):
        calls.append(len(window))
        if len(calls) == 1:
            raise OrtFail("Failed to allocate memory for requested buffer of size 1207959552")
        return real(window)

    monkeypatch.setattr(e, "_forward", once_too_big)
    out = e.embed(DOCS * 4, side="document")

    assert out.shape == (12, 768), "every input must still come back"
    assert calls[0] == 8 and calls[1] == 4, f"the batch must halve, got {calls}"
    assert e.batch == 4, "and the smaller batch has to stick, or the next call OOMs too"


def test_a_failure_that_is_not_an_oom_is_raised_rather_than_retried(monkeypatch):
    """Retrying a shape error four times reports the fourth one."""
    e = embed.get_embedder()

    def broken(_window):
        raise OrtFail("right operand cannot broadcast on dim 1")

    monkeypatch.setattr(e, "_forward", broken)
    with pytest.raises(OrtFail):
        e.embed(DOCS, side="document")


def test_the_reranker_separates_a_match_from_a_miss():
    scores = embed.get_reranker().score(QUERY, DOCS)
    assert scores.shape == (3,)
    assert int(scores.argmax()) == 0
    assert scores[0] > scores[1]


def test_release_models_returns_most_of_the_vram():
    """The 12.2 GB idle daemon, asserted as a number rather than a status.

    The old /api/gpu/release returned 200 without freeing anything, so this
    asserts the reclaimed megabytes. No session is held in a local here -- one
    reference defeats the whole unload.
    """
    embed.release_models()
    baseline = _vram_mib()

    embed.get_embedder().embed(DOCS, side="document")
    embed.get_reranker().score(QUERY, DOCS)
    loaded = _vram_mib()
    assert loaded - baseline > 500, "the models should be resident before we free them"

    embed.release_models()
    assert embed.loaded() is False
    reclaimed = loaded - _vram_mib()
    assert reclaimed > (loaded - baseline) * 0.7, f"only reclaimed {reclaimed} MiB of {loaded}"


def test_models_reload_after_a_release():
    """An unload that cannot be undone is a daemon that answers once."""
    embed.release_models()
    assert embed.get_embedder().embed(["x"], side="query").shape == (1, 768)
