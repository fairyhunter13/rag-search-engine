"""R6 inference-lane guards: haiku-only chat; no local generative LLM; GPU runs embed+rerank only.

With tier 3 deleted the lane map is two lanes, not four: the local GPU hosts the embedder and the
reranker, and `claude -p` on the dashboard chat route is the only generative model the system
reaches at all. The DeepSeek half of these guards is gone with the subsystem it gated.
"""
import inspect
from pathlib import Path

import pytest

pytestmark = pytest.mark.live

_SRC = Path(__file__).parents[2] / "rag_search"


# R6a is gone with tier 3: it asserted that KB enrichment *reached* DeepSeek and that
# _enrich_project refused to run keyless. There is no KB and no DeepSeek to assert about —
# a keyless box is now the normal configuration, and DK2 (no LLM client anywhere) is the
# guard that replaces it.


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


# The graph/llm.py guard left with the module it read. Its negative half — no _OLLAMA_URL, no
# assert_ollama_gpu, no bare `def chat(` — is already carried tree-wide by B1 below, which screens
# every file in the package rather than one named module. Its positive half asserted the *opposite*
# of what R0 established: `def deepseek_chat` and `def deepseek_key` must exist. There is no
# DeepSeek client left, so keeping it would have made this file a guard against the deletion.


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


# R6c's `test_setup_llm_services_is_stub` retired with its subject on 2026-07-31. It asserted that
# `scripts/setup_llm_services.py` still existed as a tombstone, which made the pair circular: the
# file's only reason to exist was the test, and the test's only subject was the file. A sweep of
# every tracked .py for four reachability channels (import, entry point, mention in any tracked
# non-.py file, pytest collection) found the script reached by exactly one — this test.
#
# What it was actually protecting is that no ollama provisioner comes back, and that is asserted
# where it can still fail: B1 below greps the whole tree for the forbidden tokens, so a *new* file
# under any name is caught, which a test naming one path never could.


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
# kb/resolve_rerank.py was the third entry — BPRE's Tier-1.75 bridge, the one place kb/ was
# allowed to reach the cross-encoder. It left with tier 3, and the entry has to leave with it:
# the loop below asserts every allowlisted file *exists*, so a stale entry fails outright rather
# than quietly exempting whatever later takes that path.
_RERANK_ALLOWLIST = frozenset({
    "query/search.py",
    "query/ask.py",
})


def test_rerank_passages_only_in_gpu_lane():
    """B2 positive lane: rerank_passages appears ONLY in the known GPU-lane files.

    Proves 'local GPU = embedding + reranking ONLY' structurally: no module outside
    the allowlist may call the cross-encoder, so it can never become a generative path.
    Complements test_server.py::test_reranking_is_query_time_only (index/+kb/ only).
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


def test_only_claude_profiles_opens_a_url():
    """DK2: exactly one module opens a URL, and what it opens is a usage read."""
    base = Path(__file__).parents[2] / "rag_search"
    openers = {
        py.relative_to(base).as_posix()
        for py in base.rglob("*.py")
        if "https://" in py.read_text(errors="replace")
    }
    assert openers <= _DK2_URL_ALLOWLIST, (
        f"module(s) outside {sorted(_DK2_URL_ALLOWLIST)} open a URL: {sorted(openers - _DK2_URL_ALLOWLIST)}"
    )
    profiles = (base / "core" / "claude_profiles.py").read_text()
    assert "oauth/usage" in profiles, "claude_profiles.py must read the usage endpoint"
    assert "completion" not in profiles.lower(), (
        "claude_profiles.py is allowlisted for the *usage* endpoint only — no completion call"
    )


def test_deepseek_api_key_has_no_reader():
    """DK2: the tier-3 secret is unreadable — a keyless box is the normal configuration."""
    base = Path(__file__).parents[2] / "rag_search"
    readers = [
        f"{py.relative_to(base.parent)}:{n}"
        for py in base.rglob("*.py")
        for n, line in enumerate(py.read_text(errors="replace").splitlines(), 1)
        if "DEEPSEEK" in line and not line.strip().startswith("#")
    ]
    assert not readers, f"DEEPSEEK_API_KEY still has a reader: {readers}"


def test_embedder_never_requests_cpu_ep():
    """Source-guard: embedder.py and gpu.py must never list CPUExecutionProvider in a providers=[...] arg."""
    import re
    for name, path in [("embedder.py", _SRC / "embed" / "embedder.py"), ("gpu.py", _SRC / "core" / "gpu.py")]:
        src = path.read_text()
        matches = re.findall(r'providers\s*=\s*\[.*?CPUExecutionProvider.*?\]', src, re.DOTALL)
        assert not matches, (
            f"{name} must not request CPUExecutionProvider; found: " + str(matches)
        )


# ---------------------------------------------------------------------------
# DK2 — no generative LLM client anywhere in the package (the R0 closing gate)
# ---------------------------------------------------------------------------

# Host/path fragments that mean "a completion is being requested over HTTP". Written against
# *paths* as well as hosts, because the one legitimate network call in the package goes to
# api.anthropic.com — a usage read, not a completion (see _DK2_URL_ALLOWLIST).
_LLM_ENDPOINT_TOKENS = (
    "api.deepseek.com",
    "deepseek.com",
    "api.openai.com",
    "generativelanguage.googleapis.com",
    "/v1/chat/completions",
    "/v1/messages",
    "/v1/complete",
    "/api/generate",
)

# The single module allowed to open any URL at all. R0e kept it deliberately: dashboard chat
# picks its Claude account through it, which is what stopped the account-drain regression.
_DK2_URL_ALLOWLIST = frozenset({"core/claude_profiles.py"})


def test_no_module_opens_a_generative_llm_endpoint():
    """DK2: no module in src/rag_search/ requests a completion over HTTP."""
    base = Path(__file__).parents[2] / "rag_search"
    violations: list[str] = []
    for py in base.rglob("*.py"):
        text = py.read_text(errors="replace")
        for lineno, line in enumerate(text.splitlines(), 1):
            if line.strip().startswith("#"):
                continue
            for token in _LLM_ENDPOINT_TOKENS:
                if token in line:
                    violations.append(
                        f"{py.relative_to(base.parent)}:{lineno}: {token!r}: {line.strip()[:80]}"
                    )
    assert not violations, (
        "Generative LLM endpoint found in src/rag_search (tier 3 deleted 2026-07-28; "
        "`claude -p` on dashboard chat is the whole generative surface):\n"
        + "\n".join(violations[:20])
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
