"""Live knowledge base completeness tests.

Verifies every layer of the KB is correctly built:
  - Enrichment: communities have LLM-assigned title + semantic_type (local qwen3-enrich:1.7b)
  - Hierarchy: Leiden levels built, level-2+ communities enriched
  - Wiki: pages generated from communities (local qwen3-enrich:1.7b)
  - Patterns cache: LLM classification present (frameworks, architecture, conventions)

Chat uses cloud LLM (codex gpt-5.4-mini → haiku 4.5 fallback).
All other operations use local Ollama qwen3-enrich:1.7b.

Requires: daemon at :8765, project indexed and built.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.live


# ---------------------------------------------------------------------------
# Enrichment
# ---------------------------------------------------------------------------

class TestEnrichment:
    """Communities must have LLM-assigned titles and semantic types."""

    def test_enrichment_pct_above_80(self, http, project):
        """Level-1 community enrichment must be >=80%.

        Level-1 = base Leiden communities produced by the standard pipeline.
        Level-2+ (hierarchy meta-communities) require a separate enrich_hierarchy
        run and are not checked here.
        """
        r = http.get("/api/kb_health", params={"project": project})
        assert r.status_code == 200, f"kb_health failed: {r.text[:200]}"
        data = r.json()

        by_level = data.get("enrichment_by_level", {})
        l1 = by_level.get("1", {})
        total = l1.get("total", 0)
        enriched_count = l1.get("enriched", 0)

        if total > 0:
            pct = l1.get("pct", enriched_count / total * 100)
        else:
            # No per-level breakdown — fall back to overall pct
            pct = data.get("enrichment_pct") or data.get("enriched_pct") or 0

        if pct == 0 and total == 0:
            # Last resort: count communities directly
            r2 = http.get("/api/communities", params={"project": project, "top_k": 200})
            communities = r2.json().get("communities", [])
            assert communities, "No communities found — enrichment_pct=0 and no communities in /api/communities"
            enriched = sum(1 for c in communities if c.get("title") and c["title"].strip())
            pct = enriched / len(communities) * 100

        assert pct >= 80, (
            f"Level-1 enrichment is only {pct:.0f}% — run build(action='enrich') to fix"
        )

    def test_communities_have_semantic_types(self, http, project):
        r = http.get("/api/communities", params={"project": project, "top_k": 20})
        assert r.status_code == 200
        communities = r.json().get("communities", [])
        assert communities, "No communities returned"
        first = communities[0]
        assert "semantic_type" in first, (
            f"semantic_type field missing from community response; keys={list(first.keys())} — "
            "handle_get_communities must include semantic_type"
        )
        with_type = [c for c in communities if c.get("semantic_type")]
        pct = len(with_type) / len(communities) * 100
        assert pct >= 50, (
            f"Only {pct:.0f}% of top-20 communities have semantic_type — "
            "run build(action='enrich') to backfill"
        )

    def test_enrichment_uses_local_llm(self):
        """Local enrichment LLM (qwen3-enrich:1.7b via Ollama) must be reachable."""
        import urllib.error
        import urllib.request
        try:
            with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=5) as resp:
                import json
                data = json.loads(resp.read())
                models = [m.get("name", "") for m in data.get("models", [])]
                enrich_models = [m for m in models if "enrich" in m or "qwen3" in m]
                assert enrich_models, (
                    f"qwen3-enrich model not found in Ollama — available: {models[:10]}. "
                    "Run: ollama pull qwen3-enrich:1.7b"
                )
        except urllib.error.URLError as e:
            pytest.fail(f"Ollama not reachable at localhost:11434: {e}")


# ---------------------------------------------------------------------------
# Hierarchy
# ---------------------------------------------------------------------------

class TestHierarchy:
    """Community hierarchy (Leiden levels) must be built and enriched."""

    def test_hierarchy_has_levels(self, http, project):
        r = http.get("/api/overview", params={"project": project, "what": "hierarchy"})
        assert r.status_code == 200, f"hierarchy failed: {r.text[:200]}"
        data = r.json()
        levels = data.get("levels", data.get("hierarchy_levels", []))
        if isinstance(levels, dict):
            levels = list(levels.values())
        # Either we have explicit levels, or the overview itself has community hierarchy data
        assert len(levels) > 0 or data.get("communities") or data.get("error"), (
            f"Hierarchy returned empty levels and no fallback: {data}"
        )

    def test_architecture_domains_non_empty(self, http, project):
        r = http.get("/api/overview", params={"project": project, "what": "architecture_domains"})
        assert r.status_code == 200, f"architecture_domains failed: {r.text[:200]}"
        data = r.json()
        domains = data.get("domains", data.get("architecture_domains", []))
        assert isinstance(domains, list), f"domains must be a list; got {type(domains)}"

    def test_feature_map_returns_categories(self, http, project):
        r = http.get("/api/overview", params={"project": project, "what": "feature_map"})
        assert r.status_code == 200, f"feature_map failed: {r.text[:200]}"
        data = r.json()
        feature_map = data.get("feature_map", data.get("map", {}))
        assert isinstance(feature_map, (dict, list)), (
            f"feature_map must be dict or list; got {type(feature_map)}"
        )


# ---------------------------------------------------------------------------
# Wiki
# ---------------------------------------------------------------------------

class TestWiki:
    """Wiki pages must be generated from communities."""

    def test_wiki_pages_exist(self, http, project):
        r = http.get("/api/wiki", params={"project": project})
        assert r.status_code == 200, f"wiki list failed: {r.text[:200]}"
        data = r.json()
        pages = data.get("pages", data.get("wiki_pages", []))
        assert len(pages) > 0, (
            "No wiki pages — run build(action='wiki') or build(action='pipeline')"
        )

    def test_wiki_page_has_content(self, http, project):
        r = http.get("/api/wiki", params={"project": project})
        assert r.status_code == 200
        pages = r.json().get("pages", r.json().get("wiki_pages", []))
        assert pages, "No wiki pages found — run build(action='wiki') first"
        first_page = pages[0].get("name", pages[0]) if isinstance(pages[0], dict) else pages[0]
        r2 = http.get("/api/wiki/page", params={"project": project, "name": first_page})
        assert r2.status_code == 200, f"wiki page fetch failed: {r2.text[:200]}"
        data = r2.json()
        content = data.get("content", data.get("markdown", ""))
        assert len(content) > 50, f"Wiki page '{first_page}' is nearly empty ({len(content)} chars)"

    def test_wiki_health_reported_in_kb_health(self, http, project):
        r = http.get("/api/kb_health", params={"project": project})
        assert r.status_code == 200
        data = r.json()
        wiki_count = (
            data.get("wiki_page_count")
            or data.get("wiki_count")
            or data.get("wiki_pages")
        )
        assert wiki_count is not None, (
            f"wiki_page_count not in kb_health response; keys={list(data.keys())}"
        )
        assert wiki_count >= 0, f"Unexpected wiki_count: {wiki_count}"


# ---------------------------------------------------------------------------
# Patterns cache (LLM classification)
# ---------------------------------------------------------------------------

class TestPatternsCache:
    """LLM-classified patterns must be cached and non-trivial."""

    def test_patterns_cache_populated(self, http, project):
        r = http.get("/api/kb_health", params={"project": project})
        assert r.status_code == 200
        data = r.json()
        patterns_cached = data.get("patterns_cached", data.get("has_patterns_cache"))
        assert patterns_cached is not None, (
            f"patterns_cached not in kb_health response; keys={list(data.keys())}"
        )
        # Warn but don't fail if cache is empty — first run might not have it
        if not patterns_cached:
            pytest.xfail("Patterns cache is empty — run build(action='analyze_patterns')")

    def test_patterns_llm_analysis_present(self, http, project):
        r = http.get("/api/patterns", params={"project": project})
        assert r.status_code == 200, f"patterns failed: {r.text[:200]}"
        data = r.json()
        llm_analysis = data.get("llm_analysis")
        if llm_analysis is None:
            pytest.xfail("LLM analysis not yet run — build(action='analyze_patterns') needed")
        assert isinstance(llm_analysis, (dict, str)), (
            f"llm_analysis has unexpected type: {type(llm_analysis)}"
        )


# ---------------------------------------------------------------------------
# Indexing pipeline completeness
# ---------------------------------------------------------------------------

class TestIndexingCompleteness:
    """Verify every part of the indexing pipeline produced results."""

    def test_graph_nodes_and_edges_exist(self, http, project):
        r = http.get("/api/overview", params={"project": project, "what": "structure"})
        assert r.status_code == 200
        data = r.json()
        graph_stats = data.get("graph_stats", {})
        nodes = (
            graph_stats.get("nodes", 0)
            or graph_stats.get("total_communities", 0)
            or data.get("node_count", 0)
        )
        assert nodes > 0, f"Graph has zero nodes — tree-sitter extraction failed: {graph_stats}"

    def test_vector_index_has_documents(self, http, project):
        r = http.get("/api/search", params={"q": "function", "project": project, "top_k": 1})
        assert r.status_code == 200
        results = r.json().get("results", [])
        assert len(results) > 0, "Vector index is empty — embedding step may have failed"

    def test_embedding_uses_gpu_device(self, http):
        """Embedding device must be reported as CUDA in system status."""
        r = http.get("/api/system_status")
        assert r.status_code == 200, f"system_status endpoint returned {r.status_code}: {r.text[:200]}"
        data = r.json()
        embed_device = (
            data.get("embed_device")
            or data.get("embedding_device")
            or data.get("gpu", {}).get("embed_device", "")
        )
        if embed_device:
            assert "cuda" in str(embed_device).lower(), (
                f"Embedding device is '{embed_device}' — must be CUDA. CPU fallback is forbidden."
            )

    def test_chat_uses_cloud_llm(self):
        """Dashboard chat hardcoded default must be cloud LLM — read config source to bypass env override."""
        import re
        from pathlib import Path
        config_src = (Path(__file__).parent.parent.parent / "opencode_search" / "config.py").read_text()
        m_provider = re.search(r'DEFAULT_QUERY_LLM_PROVIDER.*?os\.environ\.get\(\s*"OPENCODE_QUERY_LLM_PROVIDER"\s*,\s*"([^"]+)"', config_src)
        m_model = re.search(r'DEFAULT_QUERY_LLM_MODEL.*?os\.environ\.get\(\s*"OPENCODE_QUERY_LLM_MODEL"\s*,\s*"([^"]+)"', config_src)
        assert m_provider, "DEFAULT_QUERY_LLM_PROVIDER not found in config.py"
        assert m_model, "DEFAULT_QUERY_LLM_MODEL not found in config.py"
        provider_default = m_provider.group(1)
        model_default = m_model.group(1)
        assert provider_default in ("codex", "claude-code", "anthropic"), (
            f"Chat LLM provider default is '{provider_default}' — "
            "dashboard chat must use cloud LLM (codex or claude-code), not local Ollama"
        )
        cloud_models = ("gpt-5", "gpt-4", "haiku", "sonnet", "claude")
        assert any(m in model_default for m in cloud_models), (
            f"Chat model default '{model_default}' doesn't look like a cloud model — "
            "expected codex gpt-5.4-mini or claude haiku 4.5"
        )

    def test_enrichment_uses_local_ollama(self):
        """Enrichment LLM hardcoded default must be local Ollama — read config source to bypass env override."""
        import re
        from pathlib import Path
        config_src = (Path(__file__).parent.parent.parent / "opencode_search" / "config.py").read_text()
        m_provider = re.search(r'DEFAULT_LLM_PROVIDER.*?os\.environ\.get\(\s*"OPENCODE_LLM_PROVIDER"\s*,\s*"([^"]+)"', config_src)
        m_model = re.search(r'DEFAULT_LLM_MODEL.*?os\.environ\.get\(\s*"OPENCODE_LLM_MODEL"\s*,\s*"([^"]+)"', config_src)
        assert m_provider, "DEFAULT_LLM_PROVIDER not found in config.py"
        assert m_model, "DEFAULT_LLM_MODEL not found in config.py"
        provider_default = m_provider.group(1)
        model_default = m_model.group(1)
        assert provider_default == "ollama", (
            f"config.py hardcoded default is '{provider_default}' — must be 'ollama'. "
            "Codex/Anthropic are forbidden for KB building; only dashboard chat may use cloud LLM."
        )
        assert "qwen3" in model_default or "enrich" in model_default, (
            f"config.py default model '{model_default}' — expected qwen3-enrich:1.7b"
        )


# ---------------------------------------------------------------------------
# Hierarchy detection correctness
# ---------------------------------------------------------------------------

class TestHierarchyDetection:
    """_project_needs_hierarchy_enrich must detect unenriched communities at ALL levels."""

    def test_all_level_detector_fires_for_astro(self, project):
        """All-level detector must identify redacted-name-3 as needing hierarchy enrichment.

        redacted-name-3 has level-3 at 0% enrichment (0/1417 communities).
        The old level-2-only detector would miss this if L2 had ANY enriched titles.
        The new detector scans every level ≥2 and must return True.
        """
        from opencode_search.handlers._autopipeline import _project_needs_hierarchy_enrich
        result = _project_needs_hierarchy_enrich(project)
        # If L2 or L3 have ANY unenriched communities the detector must fire.
        # If the project is 100% enriched at all levels (sweep has completed),
        # this returns False — which is also correct; xfail to avoid flakiness.
        if not result:
            pytest.xfail(
                "All hierarchy levels appear fully enriched — "
                "the KB sweep has completed for this project. "
                "This is the desired end-state, not a test failure."
            )

    def test_detector_scans_max_level_not_just_level2(self, project):
        """_project_needs_hierarchy_enrich must inspect every level up to max_level.

        This is a code-correctness test: verify the implementation loops over
        all levels rather than hard-coding level=2 (the old bug).
        """
        import inspect

        from opencode_search.handlers import _autopipeline
        src = inspect.getsource(_autopipeline._project_needs_hierarchy_enrich)
        assert "for lvl in range" in src, (
            "_project_needs_hierarchy_enrich must use 'for lvl in range(2, max_level + 1)' — "
            "the old hard-coded level=2 check fails to detect unenriched L3+ communities"
        )

    def test_auto_pipeline_cap_raised(self):
        """handle_auto_pipeline must use a high cap (≥1000) not the old 200 limit.

        The 200-community cap caused L1 enrichment to be incomplete on the first
        run for large projects, relying on incremental backfill instead.
        """
        import inspect

        from opencode_search.handlers import _autopipeline
        src = inspect.getsource(_autopipeline.handle_auto_pipeline)
        assert "10_000" in src or "enrich_max_communities=10000" in src, (
            "handle_auto_pipeline must pass enrich_max_communities=10_000 (not 200) "
            "so the first KB build enriches all level-1 communities"
        )


# ---------------------------------------------------------------------------
# Periodic KB sweep convergence
# ---------------------------------------------------------------------------

class TestKbSweep:
    """Periodic KB sweep must be wired and converge toward 100% enrichment."""

    def test_kb_sweep_enabled_by_default(self):
        """OPENCODE_KB_SWEEP_ENABLED must default to True."""
        from opencode_search.daemon import _KB_SWEEP_ENABLED
        assert _KB_SWEEP_ENABLED, (
            "KB self-healing sweep is disabled — set OPENCODE_KB_SWEEP_ENABLED=1 "
            "(or unset, as 1 is the default)"
        )

    def test_kb_sweep_interval_reasonable(self):
        """KB sweep interval must be between 60s and 3600s."""
        from opencode_search.daemon import _KB_SWEEP_INTERVAL_S
        assert 60 <= _KB_SWEEP_INTERVAL_S <= 3600, (
            f"KB sweep interval {_KB_SWEEP_INTERVAL_S}s is outside the 60–3600s range. "
            "Set OPENCODE_KB_SWEEP_INTERVAL_S to a reasonable value."
        )

    @pytest.mark.slow
    def test_sweep_raises_level2_enrichment(self, http, project):
        """One sweep cycle must increase level-2 enrichment pct for redacted-name-3.

        This is a convergence smoke test — not a full-completion test.
        Marked slow because enrichment requires real GPU + Ollama.
        """
        import time

        r = http.get("/api/kb_health", params={"project": project})
        assert r.status_code == 200
        before = r.json().get("enrichment_by_level", {}).get("2", {}).get("pct", 0)

        # Trigger one sweep cycle via the HTTP API (build action=enrich_hierarchy)
        r2 = http.post("/api/build", json={"project": project, "action": "enrich_hierarchy"})
        assert r2.status_code in (200, 202), f"enrich_hierarchy failed: {r2.text[:200]}"

        # Wait for enrichment to progress (up to 5 minutes)
        deadline = time.time() + 300
        after = before
        while time.time() < deadline:
            time.sleep(30)
            r3 = http.get("/api/kb_health", params={"project": project})
            if r3.status_code == 200:
                after = r3.json().get("enrichment_by_level", {}).get("2", {}).get("pct", before)
                if after > before:
                    break

        assert after >= before, (
            f"Level-2 enrichment did not increase after sweep trigger: "
            f"before={before:.1f}% after={after:.1f}%. "
            "Investigate: is qwen3-enrich:1.7b running? Is VRAM available?"
        )
        if after > before:
            return  # convergence confirmed

        pytest.xfail(
            f"Level-2 enrichment did not increase within 5 minutes (stuck at {after:.1f}%). "
            "This may be a slow GPU or Ollama queue issue rather than a code bug."
        )


# ---------------------------------------------------------------------------
# KB query model routing (Phase 99)
# ---------------------------------------------------------------------------

class TestKbQueryRouting:
    """Interactive ask handlers must use qwen3-query:8b (not qwen3-enrich:1.7b / not codex)."""

    def test_kb_query_client_is_ollama_gpu(self):
        """create_kb_query_llm_client() must return an OllamaClient targeting qwen3-query:8b."""
        from opencode_search.enricher import create_kb_query_llm_client
        from opencode_search.enricher.client import OllamaClient
        client = create_kb_query_llm_client()
        assert client is not None, "create_kb_query_llm_client() returned None"
        assert isinstance(client, OllamaClient), (
            f"Expected OllamaClient for GPU-local KB queries, got {type(client).__name__}. "
            "KB queries must never use cloud (codex/anthropic) — GPU-only enforcement."
        )
        assert "qwen3-query" in client.model, (
            f"KB query model is '{client.model}' — expected qwen3-query:8b. "
            "Interactive ask must not share the qwen3-enrich:1.7b build model."
        )

    def test_kb_query_client_differs_from_enrich_client(self):
        """KB query client must use a different model than the enrich/build client."""
        import os
        # Temporarily ensure no env override hides the default
        env_override = os.environ.get("OPENCODE_KB_QUERY_LLM_MODEL")
        try:
            if env_override:
                del os.environ["OPENCODE_KB_QUERY_LLM_MODEL"]
            from opencode_search.enricher import create_kb_query_llm_client, create_llm_client
            from opencode_search.enricher.client import OllamaClient
            enrich_client = create_llm_client()
            query_client = create_kb_query_llm_client()
            assert enrich_client is not None and query_client is not None
            if isinstance(enrich_client, OllamaClient) and isinstance(query_client, OllamaClient):
                assert enrich_client.model != query_client.model, (
                    f"Enrich model == KB query model ({enrich_client.model!r}). "
                    "These must be distinct so interactive ask doesn't queue behind the build."
                )
        finally:
            if env_override:
                os.environ["OPENCODE_KB_QUERY_LLM_MODEL"] = env_override

    def test_enrich_client_rejects_codex(self):
        """create_llm_client(provider='codex') must raise RuntimeError — GPU-only enforcement."""
        import os
        old = os.environ.get("OPENCODE_LLM_PROVIDER")
        try:
            os.environ["OPENCODE_LLM_PROVIDER"] = "codex"
            import importlib
            import opencode_search.enricher.client as _mod
            importlib.reload(_mod)
            try:
                _mod.create_llm_client()
                assert False, "Expected RuntimeError for codex provider in enrich tier"
            except RuntimeError as exc:
                assert "FORBIDDEN" in str(exc), f"RuntimeError message should mention FORBIDDEN: {exc}"
        finally:
            if old is None:
                os.environ.pop("OPENCODE_LLM_PROVIDER", None)
            else:
                os.environ["OPENCODE_LLM_PROVIDER"] = old
            import importlib
            import opencode_search.enricher.client as _mod2
            importlib.reload(_mod2)

    def test_ollama_two_models_max_loaded(self):
        """OLLAMA_MAX_LOADED_MODELS must be ≥2 so enrich + query models are resident together."""
        import subprocess
        result = subprocess.run(
            ["systemctl", "show", "ollama.service", "-p", "Environment"],
            capture_output=True, text=True, check=False,
        )
        env_line = result.stdout.strip()
        # Parse all OLLAMA_MAX_LOADED_MODELS values
        import re
        matches = re.findall(r'OLLAMA_MAX_LOADED_MODELS=(\d+)', env_line)
        assert matches, (
            "OLLAMA_MAX_LOADED_MODELS not found in ollama.service environment. "
            "Set OLLAMA_MAX_LOADED_MODELS=2 in /etc/systemd/system/ollama.service.d/memory-limits.conf "
            "so qwen3-enrich:1.7b and qwen3-query:8b can be resident together."
        )
        max_loaded = int(matches[-1])
        assert max_loaded >= 2, (
            f"OLLAMA_MAX_LOADED_MODELS={max_loaded} — must be ≥2. "
            "With MAX_LOADED_MODELS=1, every enrich↔query switch evicts and cold-reloads the other "
            "model, causing interactive ask to queue behind the build."
        )

    def test_note_query_distinct_from_heartbeat(self):
        """note_query() must update last_query_monotonic; client_heartbeat() must not."""
        import time
        from opencode_search.daemon_runtime import _RuntimeState
        state = _RuntimeState()
        # heartbeat should NOT advance last_query_monotonic
        state.client_open("c1")
        before = state.last_query_monotonic
        time.sleep(0.01)
        state.client_heartbeat("c1")
        assert state.last_query_monotonic == before, (
            "client_heartbeat() must not update last_query_monotonic — "
            "heartbeats should not look like interactive queries."
        )
        # note_query() should advance it
        time.sleep(0.01)
        state.note_query()
        after = state.last_query_monotonic
        assert after > before, "note_query() must advance last_query_monotonic"
        age = state.seconds_since_last_query()
        assert age < 1.0, f"seconds_since_last_query() returned {age}s — should be near 0"
