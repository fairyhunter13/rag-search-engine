import pytest


def pytest_configure(config):
    config.addinivalue_line("markers", "live: requires CUDA GPU + daemon at :8765 + Ollama")
    config.addinivalue_line("markers", "slow: LLM-heavy (>30s)")


@pytest.fixture(scope="session")
def cuda_ep():
    import onnxruntime as ort
    if "CUDAExecutionProvider" not in ort.get_available_providers():
        pytest.fail("CUDAExecutionProvider unavailable — CPU fallback is forbidden")


@pytest.fixture(scope="session")
def embedder(cuda_ep):
    from opencode_search.embed.embedder import Embedder
    e = Embedder()
    e.warmup()
    return e
