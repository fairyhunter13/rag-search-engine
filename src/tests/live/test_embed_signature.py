"""Live gates for the vector-space identity (Phase 4, Flow 2).

`embed_signature` records what produced a stored vector. It was four fields — model, token
budget, dim, chunker rev — and said nothing about *pooling* or *prefix*, both of which move
every vector without changing any of the four. fastembed picks its pooling from the model
name alone, so a swap to a CLS-pooled model would have left the stamp byte-identical and the
fleet quietly serving two vector spaces at once. This must be right before R4 moves the
embedder.

ES1  pooling is resolved from fastembed's registry, and really varies by model
ES2  an unregistered model does not silently pass as the current pooling
ES3  the era clause keeps the live signature byte-identical to what the fleet is stamped with
ES4  a real pooling change expands the signature (and so invalidates every stamp and hash)
ES5  a real prefix change does the same, independently
ES6  a store stamped with the legacy four-field signature reads as current, not stale
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.live

# Registry-resolved, not asserted from memory: fastembed serves this one through
# `OnnxTextEmbedding` (CLS), where the current model goes through `PooledNormalizedEmbedding`
# (mean + L2). Two models, two vector spaces, four identical signature fields.
_CLS_POOLED_MODEL = "BAAI/bge-base-en-v1.5"


def test_es1_pooling_is_derived_per_model():
    """ES1: `pooling_id` reads the implementation class, so a model swap moves it.

    A constant would satisfy any single-model assertion, which is why this asks two models
    and compares them ([[feedback_guard_tests_must_discriminate]]).
    """
    from rag_search.core.config import EMBED_MODEL
    from rag_search.index.store import _ERA_POOLING, pooling_id

    live = pooling_id(EMBED_MODEL)
    other = pooling_id(_CLS_POOLED_MODEL)
    assert live == _ERA_POOLING, (
        f"live model {EMBED_MODEL} resolves to {live!r}, not the era's {_ERA_POOLING!r} — the "
        "fleet's stored vectors were built with the era pooling, so this must not drift silently"
    )
    assert other != live, (
        f"{_CLS_POOLED_MODEL} resolved to {other!r}, the same as the current model — pooling is "
        "not being read off the registry, so a differently-pooled embedder would look identical"
    )


def test_es2_unregistered_model_is_not_mistaken_for_the_era():
    """ES2: a model fastembed does not serve must not resolve to the era pooling.

    R4's candidate (`Alibaba-NLP/gte-modernbert-base`) is not in the registry — it needs
    `CustomTextEmbedding`. If an unresolvable name fell back to the era value, that swap would
    keep the legacy stamp: the exact failure this whole field exists to prevent.
    """
    from rag_search.index.store import _ERA_POOLING, pooling_id

    assert pooling_id("not-a-real-org/not-a-real-model") != _ERA_POOLING


def test_es3_era_signature_is_byte_identical_to_the_stamped_fleet():
    """ES3: while the pipeline matches the era, the signature keeps its four-field form.

    The two new fields describe what the existing runs already did, so emitting them would
    invalidate all 160 stored indexes and — because `indexer._content_hash` folds the
    signature in — defeat the byte-identical re-embed skip on every file at once, recomputing
    identical vectors at fleet scale for no change in any result.
    """
    from rag_search.core.config import EMBED_MAX_TOKENS, EMBED_MODEL
    from rag_search.index.store import CHUNKER_REV, embed_signature

    legacy = f"{EMBED_MODEL}|{EMBED_MAX_TOKENS}|768|{CHUNKER_REV}"
    assert embed_signature(768) == legacy, (
        f"signature is {embed_signature(768)!r}, not the stamped {legacy!r} — every fleet index "
        "would read stale and re-embed to identical vectors"
    )


def test_es4_a_real_pooling_change_expands_the_signature():
    """ES4: swap pooling for real and both fields appear, so nothing reads as current.

    Uses a pooling id resolved from the registry for a genuinely CLS-pooled model, not an
    invented string, so this fails if the era clause ever swallows a real change.
    """
    from rag_search.index.store import (
        _ERA_PREFIX_REV,
        _compose_signature,
        embed_signature,
        pooling_id,
    )

    other = _compose_signature(768, pooling_id(_CLS_POOLED_MODEL), _ERA_PREFIX_REV)
    assert other != embed_signature(768), (
        "a CLS-pooled model produced the same signature as the mean-pooled one in force — the "
        "two vector spaces are indistinguishable and a migration would skip every file"
    )
    assert other.endswith(f"|{pooling_id(_CLS_POOLED_MODEL)}|{_ERA_PREFIX_REV}"), (
        f"expanded signature {other!r} does not carry pooling and prefix in their own slots"
    )


def test_es5_a_real_prefix_change_expands_the_signature():
    """ES5: the prefix revision moves the signature on its own.

    The current model's card says prefixes are unnecessary, so the pipeline prepends nothing —
    but "none" is a choice. An e5- or bge-style model wants `query: ` / `passage: `, and adding
    one shifts every vector while leaving model, budget, dim, chunker and pooling untouched.
    """
    from rag_search.index.store import _ERA_POOLING, _compose_signature, embed_signature

    prefixed = _compose_signature(768, _ERA_POOLING, "e5-query-passage-1")
    assert prefixed != embed_signature(768), (
        "changing the embed prefix left the signature unchanged, so stored vectors and queries "
        "could be embedded into different spaces with no stamp disagreeing"
    )
    assert prefixed.endswith("|e5-query-passage-1")


def test_es6_legacy_stamped_store_reads_current(safe_tmp_path):
    """ES6: the compatibility claim, at the store level and not just the string level.

    ES3 proves the string; this proves what the fleet actually does with it — a real store
    carrying the four-field stamp the 160 live indexes hold must report no staleness, or the
    next reconcile re-embeds all of them.
    """
    from rag_search.core.config import EMBED_MAX_TOKENS, EMBED_MODEL
    from rag_search.index.store import CHUNKER_REV, VectorStore

    vs = VectorStore(safe_tmp_path / "vectors.db")
    try:
        vs.set_meta(
            "embed_signature", f"{EMBED_MODEL}|{EMBED_MAX_TOKENS}|768|{CHUNKER_REV}"
        )
        assert vs.stale_signature() is None, (
            f"a store stamped as the fleet is reads stale ({vs.stale_signature()!r}) — this "
            "change would trigger a full fleet re-embed producing identical vectors"
        )
    finally:
        vs.close()
