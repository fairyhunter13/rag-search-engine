"""TK8 + TK9 — licence and load gates for the configured models.

TK8 exists because two drafts of the parent plan recommended a CC-BY-NC model for a commercial
codebase and no test in this repo would have caught it. Licence is read from fastembed's model
description, never the network, so the gate is offline and deterministic.

TK9 exists because registration is metadata bookkeeping: `add_custom_model` succeeds happily for
a model that cannot load. A gate that only checks registration did not raise proves nothing.

TK8a  the configured embedder and reranker are permissively licensed
TK8b  the licence predicate really rejects a known non-commercial model
TK8c  every vetted custom reranker declares a permissive licence
TK9a  the embedder loads on a GPU EP and emits a unit-norm vector of the stored width
TK9b  the reranker returns one finite score per passage, and they discriminate
"""
from __future__ import annotations

import numpy as np
import pytest

pytestmark = pytest.mark.live

# Permissive enough to ship in a commercial codebase. Deliberately short: adding to it should
# take an argument, not a keystroke.
_ALLOWED_LICENCES = frozenset({"apache-2.0", "mit", "bsd-3-clause"})

# In fastembed's own registry as cc-by-nc-4.0, so the red case needs no invented identifier.
_KNOWN_NC_MODEL = "jinaai/jina-embeddings-v3"


def _embed_licence(model: str) -> str | None:
    from fastembed import TextEmbedding
    return getattr(TextEmbedding._get_model_description(model), "license", None)


def _rerank_licence(model: str) -> str | None:
    from fastembed.rerank.cross_encoder import TextCrossEncoder

    from rag_search.embed.embedder import _register_custom_reranker
    _register_custom_reranker(model)
    return getattr(TextCrossEncoder._get_model_description(model), "license", None)


def test_tk8a_configured_models_are_permissively_licensed():
    """TK8a: neither configured model may carry a non-commercial or unknown licence."""
    from rag_search.core.config import EMBED_MODEL, RERANK_MODEL

    for name, lic in ((EMBED_MODEL, _embed_licence(EMBED_MODEL)),
                      (RERANK_MODEL, _rerank_licence(RERANK_MODEL))):
        assert lic in _ALLOWED_LICENCES, (
            f"{name} is licensed {lic!r}, which is not in {sorted(_ALLOWED_LICENCES)} — this "
            "codebase is commercial and cannot ship a model it is not permitted to use"
        )


def test_tk8b_the_licence_predicate_rejects_a_known_nc_model():
    """TK8b: the allowlist must actually exclude something.

    Without this, an allowlist that had silently grown to accept everything -- or a licence
    lookup returning None for every model -- would leave TK8a green while gating nothing
    ([[feedback_guard_tests_must_discriminate]]).
    """
    lic = _embed_licence(_KNOWN_NC_MODEL)
    assert lic == "cc-by-nc-4.0", (
        f"{_KNOWN_NC_MODEL} now reports {lic!r}; this test's red case has moved and TK8b needs a "
        "different non-commercial model to stay meaningful"
    )
    assert lic not in _ALLOWED_LICENCES, "the allowlist admits a non-commercial licence"


def test_tk8c_every_vetted_custom_reranker_declares_a_permissive_licence():
    """TK8c: custom entries carry a licence *we* assert, so make the assertion explicit.

    Honest limitation: for a model fastembed does not ship, the licence in `_CUSTOM_RERANKERS`
    is a declaration by whoever added the row, not something this gate can verify offline. What
    it can do is refuse a row that declares nothing or declares something unusable, so adding a
    model is a decision someone has to write down and a reviewer can see.
    """
    from rag_search.embed.embedder import _CUSTOM_RERANKERS

    for model, (_size, lic, _file) in _CUSTOM_RERANKERS.items():
        assert lic in _ALLOWED_LICENCES, (
            f"custom reranker {model} declares licence {lic!r}, not in "
            f"{sorted(_ALLOWED_LICENCES)}"
        )


def test_tk9a_embedder_loads_on_gpu_and_emits_a_unit_norm_vector(embedder):
    """TK9a: the configured embedder really loads and produces what the store column holds.

    `VectorStore` declares FLOAT[768]; a model swapped for one emitting 1024 would be caught here
    rather than by a sqlite-vec error partway through a fleet re-embed.
    """
    from rag_search.core.gpu import GPU_EP_NAMES

    vecs = embedder.embed(["def handler(request):\n    return validate(request)"])
    arr = np.asarray(vecs)
    assert arr.shape == (1, 768), f"embedder emits {arr.shape}, not (1, 768)"
    assert arr.dtype == np.float32, f"embedder emits {arr.dtype}, not float32"
    norm = float(np.linalg.norm(arr[0]))
    assert abs(norm - 1.0) < 1e-3, (
        f"vector L2 norm is {norm:.5f}, not unit — cosine scores would no longer be comparable "
        "to the vectors already stored, which were normalised"
    )
    providers = embedder._model.model.model.get_providers()
    assert providers[0] in GPU_EP_NAMES, f"embedder not on a GPU EP: {providers}"


def test_tk9b_reranker_returns_one_finite_discriminating_score_per_passage():
    """TK9b: the reranker scores every passage, finitely, and separates relevant from not.

    Registration succeeds for a model that cannot load, and a model that loads can still return
    NaN under a bad ONNX export, so this drives a real rerank and reads the numbers.
    """
    import math

    from rag_search.embed.embedder import Reranker

    passages = [
        "def spawn_worker(job):\n    validate(job)\n    subprocess.Popen(job.argv)",
        "body { display: grid; grid-template-columns: 1fr 2fr; }",
        "def unrelated_math(a, b):\n    return a ** b",
    ]
    scores = Reranker().rerank("worker validation and subprocess spawn of a job", passages)
    assert len(scores) == len(passages), f"got {len(scores)} scores for {len(passages)} passages"
    assert all(math.isfinite(s) for s in scores), f"non-finite score in {scores}"
    assert scores[0] == max(scores), (
        f"the passage that literally spawns a validated subprocess did not rank first: {scores} "
        "— the cross-encoder is loaded but not discriminating"
    )
