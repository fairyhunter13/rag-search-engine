"""Root conftest — registers the 'live' marker."""


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "live: requires daemon at :8765, Ollama at :11434, CUDA GPU — skipped if absent",
    )
