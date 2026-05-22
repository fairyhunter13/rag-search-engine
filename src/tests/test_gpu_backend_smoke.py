"""Real GPU backend smoke tests for embeddings and reranking."""

from __future__ import annotations

import math

import pytest

pytest.importorskip("fastembed")
pytest.importorskip("onnxruntime")

from opencode_search.config import get_tier_dims, get_tier_models
from opencode_search.embeddings import assert_gpu_available, embed_passages, embed_query, rerank

pytestmark = [pytest.mark.integration, pytest.mark.runtime_deps, pytest.mark.gpu]


def test_real_gpu_embedding_and_rerank_smoke():
    assert_gpu_available()

    embed_model, rerank_model = get_tier_models("budget")
    dims = get_tier_dims("budget")

    vector = embed_query(
        "authorization bearer token refresh flow",
        model=embed_model,
        dimensions=dims,
    )
    assert len(vector) == dims
    assert all(math.isfinite(value) for value in vector)
    assert any(value != 0.0 for value in vector)

    passages = embed_passages(
        [
            "middleware refreshes expired bearer token and retries the request",
            "grid layout for settings screen",
        ],
        model=embed_model,
        dimensions=dims,
    )
    assert len(passages) == 2
    assert all(len(row) == dims for row in passages)
    assert all(all(math.isfinite(value) for value in row) for row in passages)

    ranked = rerank(
        "token refresh middleware",
        [
            "css grid layout for dashboard cards",
            "middleware to refresh expired access token and retry request",
            "postgres vacuum tuning and autovacuum thresholds",
        ],
        model=rerank_model,
        top_k=2,
    )
    assert ranked
    assert ranked[0][0] == 1
    assert ranked[0][1] >= ranked[-1][1]
