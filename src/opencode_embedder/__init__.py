"""Compatibility package for legacy E2E tests.

Historically this repo had a separate `opencode_embedder` service. The current
implementation lives under `opencode_search.*`, but some E2E tests (and tools)
still expect `python -m opencode_embedder.server` and HTTP endpoints on :9998.
"""

