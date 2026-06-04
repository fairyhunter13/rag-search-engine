"""Live GPU embedding tests — require CUDA RTX 5080.

All tests skip automatically if GPU embedding is unavailable.
"""
from __future__ import annotations

import subprocess
import sys

import pytest

pytestmark = pytest.mark.live


@pytest.fixture(scope="module", autouse=True)
def require_gpu(gpu):
    """All tests in this module require working GPU embedding."""


def _run_embed_script(code: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd="/home/user/git/github.com/fairyhunter13/opencode-search-engine",
    )


def test_embed_returns_nonzero_vector():
    """Embedding a real query returns a non-zero vector with positive norm."""
    result = _run_embed_script(
        "from opencode_search.embeddings import get_embed_model; "
        "import numpy as np; "
        "m = get_embed_model(); "
        "v = m.embed(['def authenticate(user, password): return check(user, password)']); "
        "norm = float(np.linalg.norm(v[0])); "
        "assert norm > 0, f'zero vector: norm={norm}'; "
        "print(f'OK norm={norm:.4f} shape={v[0].shape}')"
    )
    assert result.returncode == 0, f"Embedding failed:\n{result.stderr}"
    assert "OK" in result.stdout


def test_embed_shape_is_consistent():
    """Two different queries produce vectors of identical shape."""
    result = _run_embed_script(
        "from opencode_search.embeddings import get_embed_model; "
        "m = get_embed_model(); "
        "v1 = m.embed(['authentication handler']); "
        "v2 = m.embed(['database connection pool']); "
        "assert v1[0].shape == v2[0].shape, f'{v1[0].shape} != {v2[0].shape}'; "
        "print(f'OK shape={v1[0].shape}')"
    )
    assert result.returncode == 0, f"Shape test failed:\n{result.stderr}"


def test_different_queries_produce_different_vectors():
    """Semantically unrelated queries must not produce identical vectors."""
    result = _run_embed_script(
        "from opencode_search.embeddings import get_embed_model; "
        "import numpy as np; "
        "m = get_embed_model(); "
        "v1 = m.embed(['HTTP server handler']); "
        "v2 = m.embed(['database migration schema']); "
        "cos = float(np.dot(v1[0], v2[0]) / (np.linalg.norm(v1[0]) * np.linalg.norm(v2[0]))); "
        "assert cos < 0.99, f'vectors suspiciously similar: cos={cos:.4f}'; "
        "print(f'OK cosine={cos:.4f}')"
    )
    assert result.returncode == 0, f"Similarity test failed:\n{result.stderr}"


def test_cpu_fallback_raises_fatal_error():
    """Setting OPENCODE_EMBED_DEVICE=cpu must crash, not silently fall back."""
    result = _run_embed_script(
        "import os; os.environ['OPENCODE_EMBED_DEVICE'] = 'cpu'; "
        "from opencode_search.embeddings import get_embed_model; "
        "m = get_embed_model(); "
        "m.embed(['test'])"
    )
    assert result.returncode != 0, (
        "CPU embedding must raise a fatal error — got returncode 0 (silent CPU fallback is forbidden)"
    )
