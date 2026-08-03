"""Live gates for the vector-space identity (Phase 4, Flow 2).

`embed_signature` records what produced a stored vector. It was four fields — model, token
budget, dim, chunker rev — and said nothing about *pooling* or *prefix*, both of which move
every vector without changing any of the four. fastembed picks its pooling from the model
name alone, so a swap to a CLS-pooled model would have left the stamp byte-identical and the
fleet quietly serving two vector spaces at once. This had to be right before the embedder
moved, and the embedder has now moved: nomic + task prefixes, so the live pipeline is neither
the pooling nor the prefix the four-field era described.

ES3 and ES6 are the same claims they always were, read the other way round. They asserted the
era clause held the signature byte-identical to the stamped fleet so nothing re-embedded for
free; with the era clause deleted they assert the stamp really did move, which is what makes
the fleet migrate instead of serving the old vectors against nomic-embedded queries.

ES1  pooling is resolved from fastembed's registry, and really varies by model
ES2  an unregistered model does not silently pass as the live pooling
ES3  the legacy four-field signature is no longer what this pipeline emits
ES4  a real pooling change moves the signature (and so invalidates every stamp and hash)
ES5  a real prefix change does the same, independently
ES6  a store stamped with the legacy four-field signature reads stale, not current
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.live

# Registry-resolved, not asserted from memory: fastembed serves this one through
# `OnnxTextEmbedding` (CLS), where the current model goes through `PooledEmbedding` (mean, no
# L2). Two models, two vector spaces, four identical signature fields.
_CLS_POOLED_MODEL = "BAAI/bge-base-en-v1.5"


def test_es1_pooling_is_derived_per_model():
    """ES1: `pooling_id` reads the implementation class, so a model swap moves it.

    A constant would satisfy any single-model assertion, which is why this asks two models
    and compares them ([[feedback_guard_tests_must_discriminate]]).
    """
    from rag_search.core.config import EMBED_MODEL
    from rag_search.index.store import pooling_id

    live = pooling_id(EMBED_MODEL)
    other = pooling_id(_CLS_POOLED_MODEL)
    assert live != "unregistered", (
        f"the configured model {EMBED_MODEL} resolves to no fastembed implementation, so the "
        "signature names a pooling nobody can act on and a swap to a differently-pooled model "
        "would look identical to this one"
    )
    assert other != live, (
        f"{_CLS_POOLED_MODEL} resolved to {other!r}, the same as the current model — pooling is "
        "not being read off the registry, so a differently-pooled embedder would look identical"
    )


def test_es2_unregistered_model_is_not_mistaken_for_the_live_pooling():
    """ES2: a model fastembed does not serve must not resolve to the live pooling.

    `Alibaba-NLP/gte-modernbert-base` is not in the registry — it needs `CustomTextEmbedding`.
    If an unresolvable name fell back to whatever the current model resolves to, that swap
    would keep the current stamp: the exact failure this whole field exists to prevent.
    """
    from rag_search.core.config import EMBED_MODEL
    from rag_search.index.store import pooling_id

    assert pooling_id("not-a-real-org/not-a-real-model") != pooling_id(EMBED_MODEL)


def test_es3_the_signature_names_pooling_and_prefix_in_their_own_slots():
    """ES3: the four-field form is gone — pooling and prefix are always emitted.

    This assertion used to run the other way: the era clause suppressed both fields so the
    live signature stayed byte-identical to the fleet's stamp, because the fields described
    what those runs already did. The nomic switch moved model and token budget for real, so
    there is nothing left to protect, and the suppression branch was deleted. What has to hold
    now is that neither field can go missing — `indexer._content_hash` folds the signature in,
    so a signature that stopped naming the prefix would let prefixed and unprefixed vectors
    share a stamp and a content hash.
    """
    from rag_search.core.config import EMBED_MAX_TOKENS, EMBED_MODEL
    from rag_search.index.store import (
        CHUNKER_REV,
        EMBED_PREFIX_REV,
        embed_signature,
        pooling_id,
    )

    live = embed_signature(768)
    assert live != f"{EMBED_MODEL}|{EMBED_MAX_TOKENS}|768|{CHUNKER_REV}", (
        f"signature {live!r} is still the four-field form — a store built before the prefixes "
        "would read current and keep serving unprefixed vectors against prefixed queries"
    )
    assert live.endswith(f"|{pooling_id()}|{EMBED_PREFIX_REV}"), (
        f"signature {live!r} does not carry pooling and prefix in their own slots — a "
        "suppressed field is a vector space two stores disagree about while their stamps agree"
    )


def test_es4_a_real_pooling_change_moves_the_signature():
    """ES4: swap pooling for real and nothing reads as current.

    Uses a pooling id resolved from the registry for a genuinely CLS-pooled model, not an
    invented string, so this fails if a real change is ever swallowed.
    """
    from rag_search.index.store import (
        EMBED_PREFIX_REV,
        _compose_signature,
        embed_signature,
        pooling_id,
    )

    other = _compose_signature(768, pooling_id(_CLS_POOLED_MODEL), EMBED_PREFIX_REV)
    assert other != embed_signature(768), (
        "a CLS-pooled model produced the same signature as the mean-pooled one in force — the "
        "two vector spaces are indistinguishable and a migration would skip every file"
    )
    assert other.endswith(f"|{pooling_id(_CLS_POOLED_MODEL)}|{EMBED_PREFIX_REV}"), (
        f"signature {other!r} does not carry pooling and prefix in their own slots"
    )


def test_es5_a_real_prefix_change_expands_the_signature():
    """ES5: the prefix revision moves the signature on its own.

    The pipeline prepends nomic's `search_document: ` / `search_query: `. An e5- or bge-style
    model wants `query: ` / `passage: ` instead, and swapping to those shifts every vector
    while leaving model, budget, dim, chunker and pooling untouched.
    """
    from rag_search.index.store import _compose_signature, embed_signature, pooling_id

    prefixed = _compose_signature(768, pooling_id(), "e5-query-passage-1")
    assert prefixed != embed_signature(768), (
        "changing the embed prefix left the signature unchanged, so stored vectors and queries "
        "could be embedded into different spaces with no stamp disagreeing"
    )
    assert prefixed.endswith("|e5-query-passage-1")


def test_es6_legacy_stamped_store_reads_stale(safe_tmp_path):
    """ES6: the migration claim, at the store level and not just the string level.

    ES3 proves the string; this proves what the fleet actually does with it. A real store
    carrying the four-field stamp the live indexes were built with must report *stale* — those
    vectors were embedded with no prefix by a different model, and a store that read current
    would answer nomic-prefixed queries out of jina's vector space forever, with nothing
    logging a disagreement.
    """
    from rag_search.core.config import EMBED_MAX_TOKENS, EMBED_MODEL
    from rag_search.index.store import CHUNKER_REV, VectorStore

    vs = VectorStore(safe_tmp_path / "vectors.db")
    try:
        vs.set_meta(
            "embed_signature", f"{EMBED_MODEL}|{EMBED_MAX_TOKENS}|768|{CHUNKER_REV}"
        )
        assert vs.stale_signature() is not None, (
            "a four-field stamp reads current, so the pre-prefix fleet would never migrate and "
            "would keep answering prefixed queries out of the vector space it was built in"
        )
    finally:
        vs.close()
