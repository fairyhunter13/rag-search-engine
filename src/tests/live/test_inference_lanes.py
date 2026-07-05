"""R6 inference-lane guards: DeepSeek-only KB; haiku-only chat; no local generative LLM."""
import inspect
from pathlib import Path

import pytest

pytestmark = pytest.mark.live

_SRC = Path(__file__).parents[2] / "rag_search"


# ---------------------------------------------------------------------------
# R6a — KB enrichment → DeepSeek-only (no local generative LLM)
# ---------------------------------------------------------------------------


def test_kb_enrich_is_deepseek_only():
    """R6a static: enrich.py KB enrichment uses deepseek_extract (no local ollama/generative fallback)."""
    import rag_search.graph.enrich as enrich_mod
    src = inspect.getsource(enrich_mod)
    assert "deepseek_extract" in src, "enrich.py must use deepseek_extract for KB enrichment"
    assert "ollama" not in src.lower(), "enrich.py must not reference ollama"


def test_enrich_project_crashes_without_key():
    """R6a static: sweeps._enrich_project precondition raises RuntimeError when DEEPSEEK_API_KEY absent."""
    from rag_search.daemon import sweeps

    src = inspect.getsource(sweeps._enrich_project)
    assert "deepseek_key()" in src, "_enrich_project must check deepseek_key() before proceeding"
    assert "RuntimeError" in src, "_enrich_project must raise RuntimeError when key is absent"
    # The check must appear early — within the first 15 non-blank lines of the function
    lines = [ln for ln in src.splitlines() if ln.strip()]
    key_line = next((i for i, ln in enumerate(lines) if "deepseek_key()" in ln), None)
    assert key_line is not None, "_enrich_project must call deepseek_key()"
    assert key_line < 10, (
        f"deepseek_key() check is at line {key_line} — must be near the top of _enrich_project"
    )


# ---------------------------------------------------------------------------
# R6b — Dashboard chat: haiku-only (EC2 — chat-lane purity)
# ---------------------------------------------------------------------------


def test_chat_lane_is_haiku_only():
    """EC2 / R6b static: routes_chat.py has no DeepSeek symbols — chat lane is Haiku-only."""
    from rag_search.server import routes_chat

    src = inspect.getsource(routes_chat)
    assert "deepseek_chat" not in src, (
        "routes_chat.py must NOT reference deepseek_chat (chat is Haiku-only; DeepSeek is KB-enrichment-only)"
    )
    assert "QUERY_LLM_FALLBACK_MODEL" not in src, (
        "routes_chat.py must NOT reference QUERY_LLM_FALLBACK_MODEL (fallback lane removed)"
    )
    assert "deepseek_key()" not in src, (
        "routes_chat.py must NOT call deepseek_key() (no DeepSeek in chat lane)"
    )
    assert "codex" not in src.lower(), "routes_chat.py must not reference codex (removed)"
    assert "QUERY_LLM_MODEL" in src, "routes_chat.py must reference QUERY_LLM_MODEL (haiku)"


def test_chat_primary_model_is_haiku():
    """R6b static: QUERY_LLM_MODEL defaults to claude-haiku-4-5 (dashboard chat primary lane)."""
    from rag_search.core.config import QUERY_LLM_MODEL

    assert "haiku" in QUERY_LLM_MODEL.lower(), (
        f"QUERY_LLM_MODEL must be a haiku model; got {QUERY_LLM_MODEL!r}"
    )


# ---------------------------------------------------------------------------
# R6c — Decommission local generative LLM (qwen3 / ollama)
# ---------------------------------------------------------------------------


def test_no_local_generative_llm_in_llm_module():
    """R6c static: graph/llm.py has no deleted chat(), _OLLAMA_URL, or assert_ollama_gpu."""
    text = (_SRC / "graph" / "llm.py").read_text()
    assert "_OLLAMA_URL" not in text, "graph/llm.py still defines _OLLAMA_URL (decommissioned)"
    assert "assert_ollama_gpu" not in text, "graph/llm.py still references assert_ollama_gpu (decommissioned)"
    assert "def chat(" not in text, "graph/llm.py still defines chat() (local generative LLM decommissioned)"
    # Positive: DeepSeek must remain
    assert "def deepseek_chat" in text, "graph/llm.py must still define deepseek_chat"
    assert "def deepseek_key" in text, "graph/llm.py must still define deepseek_key"


def test_config_has_no_ollama_knobs():
    """R6c static: core/config.py contains no ollama/qwen3 build-LLM config knobs."""
    text = (_SRC / "core" / "config.py").read_text()
    for forbidden in ("LLM_PROVIDER", "LLM_MODEL", "LLM_BASE_URL", "LLM_NUM_CTX", "LLM_CONCURRENCY"):
        # QUERY_LLM_* variants are the allowed dashboard-chat config; bare LLM_* are the removed ollama knobs
        non_query = [
            line for line in text.splitlines()
            if forbidden in line
            and not line.strip().startswith("#")
            and ("QUERY_" + forbidden) not in line
        ]
        assert not non_query, (
            f"core/config.py still has non-QUERY_ reference to {forbidden}: {non_query[:2]}"
        )


def test_gpu_module_has_no_ollama_guard():
    """R6c static: core/gpu.py no longer contains assert_ollama_gpu; assert_gpu_available is the guard."""
    text = (_SRC / "core" / "gpu.py").read_text()
    assert "assert_ollama_gpu" not in text, "core/gpu.py still defines assert_ollama_gpu (decommissioned)"
    assert "assert_gpu_available" in text, (
        "core/gpu.py must define assert_gpu_available (GPU guard for embeddings+reranking)"
    )


def test_setup_llm_services_is_stub():
    """R6c static: scripts/setup_llm_services.py is a tombstone — not a functional provisioner."""
    stub = Path(__file__).parents[3] / "scripts" / "setup_llm_services.py"
    assert stub.exists(), "scripts/setup_llm_services.py must exist (as a tombstone stub)"
    text = stub.read_text()
    assert "REMOVED" in text or "raise SystemExit" in text, (
        "scripts/setup_llm_services.py must be a tombstone (not a functional ollama provisioner)"
    )
    assert "subprocess" not in text, "tombstone stub must not invoke subprocess"
    assert "systemctl" not in text, "tombstone stub must not manage systemd services"


# ---------------------------------------------------------------------------
# B1 — tree-wide forbidden-token grep (closes the 4-file R6c coverage gap)
# ---------------------------------------------------------------------------

_FORBIDDEN_TOKENS = (
    "ollama",
    "qwen",
    "llama_cpp",
    "llama.cpp",
    ":11434",
    ":11435",
    "_OLLAMA_URL",
    "assert_ollama_gpu",
    "OLLAMA_",
    "def chat(",
)

# Lines that name the prohibition are allowed in guard/comment text.
_B1_ALLOWED_CONTEXTS = (
    "no local generative LLM",
    "decommissioned",
    "ollama.service",   # uninstall note
    "remove ollama",    # uninstall note
)


def test_no_local_llm_tokens_anywhere_in_src():
    """B1 tree-wide: src/rag_search/**/*.py must not contain any local-LLM token.

    R6c only checked 4 named files; this scans the entire package so a new module
    cannot silently reintroduce Ollama, qwen3, llama.cpp, or a bare 'def chat('.
    Lines that are pure comments or name the prohibition are exempted.
    """
    base = Path(__file__).parents[2] / "rag_search"
    violations: list[str] = []
    for py in base.rglob("*.py"):
        text = py.read_text(errors="replace")
        for token in _FORBIDDEN_TOKENS:
            if token not in text:
                continue
            for lineno, line in enumerate(text.splitlines(), 1):
                if token not in line:
                    continue
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if any(ctx in line for ctx in _B1_ALLOWED_CONTEXTS):
                    continue
                violations.append(
                    f"{py.relative_to(base.parent)}:{lineno}: "
                    f"forbidden token {token!r}: {stripped[:80]}"
                )
    assert not violations, (
        "Local-LLM tokens found in src/rag_search "
        "(Ollama/qwen3 decommissioned 2026-06-20):\n"
        + "\n".join(violations[:20])
    )


# ---------------------------------------------------------------------------
# B2 — positive lane assertion: rerank_passages only in the GPU lane
# ---------------------------------------------------------------------------

# Canonical allowlist: the ONLY files permitted to use rerank_passages.
# query/search.py    — defines rerank_passages (GPU cross-encoder)
# query/ask.py       — calls it for AXIS-B community context ranking
# kb/resolve_rerank.py — Tier-1.75 bridge (single kb/ delegation point)
_RERANK_ALLOWLIST = frozenset({
    "query/search.py",
    "query/ask.py",
    "kb/resolve_rerank.py",
})


def test_rerank_passages_only_in_gpu_lane():
    """B2 positive lane: rerank_passages appears ONLY in the known GPU-lane files.

    Proves 'local GPU = embedding + reranking ONLY' structurally: no module outside
    the allowlist may call the cross-encoder, so it can never become a generative path.
    Complements test_p5_server.py::test_reranking_is_query_time_only (index/+kb/ only).
    """
    base = Path(__file__).parents[2] / "rag_search"
    violations: list[str] = []
    for py in base.rglob("*.py"):
        if "rerank_passages" not in py.read_text(errors="replace"):
            continue
        rel = py.relative_to(base).as_posix()
        if rel not in _RERANK_ALLOWLIST:
            violations.append(str(py.relative_to(base.parent)))
    assert not violations, (
        "rerank_passages found outside the GPU-lane allowlist "
        f"{sorted(_RERANK_ALLOWLIST)}:\n" + "\n".join(violations)
    )
    for rel in _RERANK_ALLOWLIST:
        f = base / rel
        assert f.exists(), f"Allowlisted file missing: {rel}"
        assert "rerank_passages" in f.read_text(), (
            f"Allowlisted file has no rerank_passages: {rel}"
        )


# ---------------------------------------------------------------------------
# GPU-primary-EP source-guards (collection-time, no GPU required)
# ---------------------------------------------------------------------------


def test_embedder_never_requests_cpu_ep():
    """Source-guard: embedder.py and gpu.py must never list CPUExecutionProvider in a providers=[...] arg."""
    import re
    for name, path in [("embedder.py", _SRC / "embed" / "embedder.py"), ("gpu.py", _SRC / "core" / "gpu.py")]:
        src = path.read_text()
        matches = re.findall(r'providers\s*=\s*\[.*?CPUExecutionProvider.*?\]', src, re.DOTALL)
        assert not matches, (
            f"{name} must not request CPUExecutionProvider; found: " + str(matches)
        )


def test_embedder_does_not_use_is_gpu_available():
    """Source-guard: Embedder/Reranker must use assert_gpu_available (fatal), not is_gpu_available."""
    src = (_SRC / "embed" / "embedder.py").read_text()
    assert "is_gpu_available" not in src, (
        "embed/embedder.py must not call is_gpu_available() — "
        "use assert_gpu_available() (fatal) for runtime enforcement"
    )
    assert "assert_gpu_available" in src, (
        "embed/embedder.py must call assert_gpu_available() in __init__"
    )
