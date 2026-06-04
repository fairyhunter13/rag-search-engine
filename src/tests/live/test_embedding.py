"""Live GPU embedding tests — require CUDA RTX 5080.

All tests skip automatically if GPU embedding is unavailable.
"""
from __future__ import annotations

import subprocess
import sys

import pytest

pytestmark = pytest.mark.live

_EMBED_SCRIPT_HEADER = (
    "from opencode_search.embeddings import embed_query; "
    "from opencode_search.config import DEFAULT_EMBED_MODEL, DEFAULT_DIMS; "
)


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
        _EMBED_SCRIPT_HEADER +
        "import numpy as np; "
        "v = embed_query('def authenticate(user, password): return check(user, password)', "
        "    model=DEFAULT_EMBED_MODEL, dimensions=DEFAULT_DIMS); "
        "norm = float(np.linalg.norm(v)); "
        "assert norm > 0, f'zero vector: norm={norm}'; "
        "print(f'OK norm={norm:.4f} shape={len(v)}')"
    )
    assert result.returncode == 0, f"Embedding failed:\n{result.stderr}"
    assert "OK" in result.stdout


def test_embed_shape_is_consistent():
    """Two different queries produce vectors of identical shape."""
    result = _run_embed_script(
        _EMBED_SCRIPT_HEADER +
        "v1 = embed_query('authentication handler', model=DEFAULT_EMBED_MODEL, dimensions=DEFAULT_DIMS); "
        "v2 = embed_query('database connection pool', model=DEFAULT_EMBED_MODEL, dimensions=DEFAULT_DIMS); "
        "assert len(v1) == len(v2), f'{len(v1)} != {len(v2)}'; "
        "print(f'OK shape={len(v1)}')"
    )
    assert result.returncode == 0, f"Shape test failed:\n{result.stderr}"
    assert "OK" in result.stdout


def test_different_queries_produce_different_vectors():
    """Semantically unrelated queries must not produce identical vectors."""
    result = _run_embed_script(
        _EMBED_SCRIPT_HEADER +
        "import numpy as np; "
        "v1 = np.array(embed_query('HTTP server handler', model=DEFAULT_EMBED_MODEL, dimensions=DEFAULT_DIMS)); "
        "v2 = np.array(embed_query('database migration schema', model=DEFAULT_EMBED_MODEL, dimensions=DEFAULT_DIMS)); "
        "cos = float(np.dot(v1, v2) / (np.linalg.norm(v1) * np.linalg.norm(v2))); "
        "assert cos < 0.99, f'vectors suspiciously similar: cos={cos:.4f}'; "
        "print(f'OK cosine={cos:.4f}')"
    )
    assert result.returncode == 0, f"Similarity test failed:\n{result.stderr}"
    assert "OK" in result.stdout


def test_cpu_fallback_raises_fatal_error():
    """Setting OPENCODE_ONNX_PROVIDER=cpu must crash, not silently fall back."""
    result = _run_embed_script(
        "import os; os.environ['OPENCODE_ONNX_PROVIDER'] = 'cpu'; "
        "from opencode_search.embeddings import embed_query; "
        "from opencode_search.config import DEFAULT_EMBED_MODEL, DEFAULT_DIMS; "
        "embed_query('test', model=DEFAULT_EMBED_MODEL, dimensions=DEFAULT_DIMS)"
    )
    assert result.returncode != 0, (
        "CPU embedding must raise a fatal error — got returncode 0 (silent CPU fallback is forbidden)"
    )
