"""R6 inference-lane guards: DeepSeek-only KB; haiku+DeepSeek chat; no local generative LLM."""
import inspect
from pathlib import Path

import pytest

pytestmark = pytest.mark.live

_SRC = Path(__file__).parents[2] / "opencode_search"


# ---------------------------------------------------------------------------
# R6a — KB enrichment → DeepSeek-only (no local generative LLM)
# ---------------------------------------------------------------------------


def test_kb_chat_is_deepseek_only():
    """R6a static: _kb_chat in graph/enrich.py delegates to deepseek_chat — no local ollama fallback."""
    from opencode_search.graph import enrich

    src = inspect.getsource(enrich._kb_chat)
    assert "deepseek_chat" in src, "_kb_chat must delegate to deepseek_chat"
    # No silent swallow — deepseek_chat raises on missing key/unreachable; _kb_chat must not hide that
    assert 'return ""' not in src, "_kb_chat must not silently swallow failures (no 'return \"\"')"


def test_enrich_callers_use_kb_chat():
    """R6a static: enrich_community / enrich_community_l2 / enrich_symbols all route through _kb_chat."""
    from opencode_search.graph import enrich

    for fn_name in ("enrich_community", "enrich_community_l2", "enrich_symbols"):
        src = inspect.getsource(getattr(enrich, fn_name))
        assert "_kb_chat(" in src, f"{fn_name} must call _kb_chat() — not bare deepseek_chat or deleted chat()"
        # Detect bare chat() calls (the old qwen3 invocation) — _kb_chat / deepseek_chat are fine
        residue = src.replace("_kb_chat(", "").replace("deepseek_chat(", "")
        assert "chat(" not in residue, (
            f"{fn_name} still has bare chat() call (local generative LLM decommissioned)"
        )


def test_enrich_project_crashes_without_key():
    """R6a static: sweeps._enrich_project precondition raises RuntimeError when DEEPSEEK_API_KEY absent."""
    from opencode_search.daemon import sweeps

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
# R6b — Dashboard chat: haiku primary + DeepSeek fallback
# ---------------------------------------------------------------------------


def test_stream_answer_has_deepseek_fallback():
    """R6b static: routes_chat.py _stream_answer has a real DeepSeek fallback branch."""
    from opencode_search.server import routes_chat

    src = inspect.getsource(routes_chat)
    assert "deepseek_chat" in src, "routes_chat.py must reference deepseek_chat for the fallback lane"
    assert "QUERY_LLM_FALLBACK_MODEL" in src, (
        "routes_chat.py must reference QUERY_LLM_FALLBACK_MODEL (set on fallback fire)"
    )
    assert "deepseek_key()" in src, "routes_chat.py must guard DeepSeek fallback with deepseek_key()"
    assert "codex" not in src.lower(), "routes_chat.py must not reference codex (removed)"


def test_chat_fallback_model_is_deepseek():
    """R6b static: QUERY_LLM_FALLBACK_MODEL defaults to deepseek-chat (not haiku or codex)."""
    from opencode_search.core.config import QUERY_LLM_FALLBACK_MODEL

    assert "deepseek" in QUERY_LLM_FALLBACK_MODEL.lower(), (
        f"QUERY_LLM_FALLBACK_MODEL must be a DeepSeek model; got {QUERY_LLM_FALLBACK_MODEL!r}"
    )


def test_chat_primary_model_is_haiku():
    """R6b static: QUERY_LLM_MODEL defaults to claude-haiku-4-5 (dashboard chat primary lane)."""
    from opencode_search.core.config import QUERY_LLM_MODEL

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
    """R6c static: core/gpu.py no longer contains assert_ollama_gpu; assert_cuda_available survives."""
    text = (_SRC / "core" / "gpu.py").read_text()
    assert "assert_ollama_gpu" not in text, "core/gpu.py still defines assert_ollama_gpu (decommissioned)"
    assert "assert_cuda_available" in text, (
        "core/gpu.py must still define assert_cuda_available (GPU guard for embeddings+reranking)"
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
