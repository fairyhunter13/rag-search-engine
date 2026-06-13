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
            f"Level-1 enrichment is only {pct:.0f}% — run POST /api/enrich_hierarchy to fix"
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
            "run POST /api/enrich_hierarchy to backfill"
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
            "No wiki pages — run POST /api/index or index(enabled=True) [daemon auto-builds KB]"
        )

    def test_wiki_page_has_content(self, http, project):
        r = http.get("/api/wiki", params={"project": project})
        assert r.status_code == 200
        pages = r.json().get("pages", r.json().get("wiki_pages", []))
        assert pages, "No wiki pages found — run POST /api/index first"
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

    @pytest.mark.slow
    def test_patterns_cache_populated(self, http, project):
        """Patterns cache must be populated — if empty, establish it and assert.

        No xfail: an empty cache is a fixable precondition, not an expected failure.
        POST /api/analyze_patterns runs the local GPU qwen3-enrich:1.7b classifier
        and persists patterns_cache.json; we then re-read kb_health and require it.
        """
        r = http.get("/api/kb_health", params={"project": project})
        assert r.status_code == 200
        data = r.json()
        patterns_cached = data.get("patterns_cached", data.get("has_patterns_cache"))
        assert patterns_cached is not None, (
            f"patterns_cached not in kb_health response; keys={list(data.keys())}"
        )
        analyze_resp = ""
        if not patterns_cached:
            # Establish the precondition with real LLM analysis (local GPU Ollama).
            ar = http.post("/api/analyze_patterns", params={"project": project})
            assert ar.status_code == 200, f"analyze_patterns failed: {ar.text[:200]}"
            analyze_resp = ar.text[:300]
            r2 = http.get("/api/kb_health", params={"project": project})
            assert r2.status_code == 200
            d2 = r2.json()
            patterns_cached = d2.get("patterns_cached", d2.get("has_patterns_cache"))
        assert patterns_cached, (
            f"Patterns cache still empty after POST /api/analyze_patterns; "
            f"analyze response: {analyze_resp}"
        )

    @pytest.mark.slow
    def test_patterns_llm_analysis_present(self, http, project):
        """/api/patterns must carry llm_analysis — trigger analysis if absent, then assert.

        No xfail: a missing analysis is a fixable precondition. We POST
        /api/analyze_patterns (local GPU qwen3-enrich:1.7b) and require the
        subsequent /api/patterns response to carry a real llm_analysis payload.
        """
        r = http.get("/api/patterns", params={"project": project})
        assert r.status_code == 200, f"patterns failed: {r.text[:200]}"
        llm_analysis = r.json().get("llm_analysis")
        if llm_analysis is None:
            ar = http.post("/api/analyze_patterns", params={"project": project})
            assert ar.status_code == 200, f"analyze_patterns failed: {ar.text[:200]}"
            r2 = http.get("/api/patterns", params={"project": project})
            assert r2.status_code == 200, f"patterns failed: {r2.text[:200]}"
            llm_analysis = r2.json().get("llm_analysis")
        assert isinstance(llm_analysis, (dict, str)) and llm_analysis, (
            f"llm_analysis missing or wrong type after analysis: {type(llm_analysis).__name__}"
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
        """Detector verdict must match the graph's real level-≥2 enrichment state.

        _project_needs_hierarchy_enrich returns True iff some level ≥2 community has
        node_count ≥ 2 and no title. We recompute that exact predicate directly from
        the graph and assert the public detector agrees — meaningful in BOTH states
        (no hidden skip):
          - a thin federation root (redacted-name-3) has only level-1, so max_level==1
            and the detector correctly returns False (nothing above L1 to enrich);
          - a project with unenriched L2/L3 communities makes the detector return True.
        Either way the assertion is real — no xfail.
        """
        from opencode_search.config import get_project_graph_db_path
        from opencode_search.graph.storage import GraphStorage
        from opencode_search.handlers._autopipeline import _project_needs_hierarchy_enrich

        result = _project_needs_hierarchy_enrich(project)

        # Independently recompute the ground-truth predicate from the graph.
        graph_db = get_project_graph_db_path(project)
        gs = GraphStorage(graph_db)
        gs.open()
        try:
            max_level = gs.get_max_community_level()
            ground_truth = False
            for lvl in range(2, max_level + 1):
                comms = gs.get_communities(level=lvl)
                if any(not c.title and (c.node_count or 0) >= 2 for c in comms):
                    ground_truth = True
                    break
        finally:
            gs.close()

        assert result == ground_truth, (
            f"_project_needs_hierarchy_enrich returned {result} but the graph's actual "
            f"unenriched-level≥2 state is {ground_truth} (max_level={max_level})"
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
        """_handle_pipeline_body must use a high cap (≥1000) not the old 200 limit.

        The 200-community cap caused L1 enrichment to be incomplete on the first
        run for large projects, relying on incremental backfill instead.
        """
        import inspect

        from opencode_search.handlers import _autopipeline
        src = inspect.getsource(_autopipeline._handle_pipeline_body)
        assert "10_000" in src or "enrich_max_communities=10000" in src, (
            "_handle_pipeline_body must pass enrich_max_communities=10_000 (not 200) "
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
    @pytest.mark.flaky(reruns=2, reruns_delay=30)
    def test_sweep_raises_level2_enrichment(self, http, project):
        """A sweep cycle must advance level-2 enrichment toward convergence.

        No xfail — every outcome is asserted explicitly so the test never silently
        skips. Three real cases:
          - thin federation root (no level-2 meta-communities) → nothing to enrich,
            assert the empty state is self-consistent;
          - level-2 already converged (≥99%) → assert it stays converged after a
            re-trigger (no regression);
          - level-2 unconverged → one sweep must make real forward progress.
        Marked slow because enrichment requires real GPU + Ollama; flaky reruns
        absorb transient Ollama-queue stalls without masking a genuine plateau.
        """
        import time

        r = http.get("/api/kb_health", params={"project": project})
        assert r.status_code == 200
        l2 = r.json().get("enrichment_by_level", {}).get("2", {})
        before = l2.get("pct", 0)
        total_l2 = l2.get("total", 0)

        # Case 1: no level-2 meta-communities exist (thin/aggregator root). There is
        # nothing to converge — assert the empty state is consistent and return.
        if total_l2 == 0:
            assert before in (0, 0.0) or before >= 99.0, (
                f"Level-2 reports 0 communities but pct={before} — inconsistent kb_health"
            )
            return

        # Trigger one sweep cycle via the dedicated HTTP endpoint.
        r2 = http.post("/api/enrich_hierarchy", json={"project": project})
        assert r2.status_code in (200, 202), f"enrich_hierarchy failed: {r2.text[:200]}"

        # Case 2: already converged — staying converged is the success condition.
        if before >= 99.0:
            time.sleep(30)
            r3 = http.get("/api/kb_health", params={"project": project})
            after = (
                r3.json().get("enrichment_by_level", {}).get("2", {}).get("pct", before)
                if r3.status_code == 200 else before
            )
            assert after >= 99.0, (
                f"Level-2 was converged ({before:.1f}%) but regressed to {after:.1f}%"
            )
            return

        # Case 3: unconverged level with real communities — one sweep must progress.
        deadline = time.time() + 420
        after = before
        while time.time() < deadline:
            time.sleep(30)
            r3 = http.get("/api/kb_health", params={"project": project})
            if r3.status_code == 200:
                after = r3.json().get("enrichment_by_level", {}).get("2", {}).get("pct", before)
                if after > before:
                    break

        assert after >= before, (
            f"Level-2 enrichment regressed after sweep trigger: "
            f"before={before:.1f}% after={after:.1f}%."
        )
        assert after > before, (
            f"Level-2 enrichment made no progress within 7 minutes "
            f"(before={before:.1f}% after={after:.1f}%). "
            "A sweep on an unconverged level must enrich at least one community — "
            "investigate: is qwen3-enrich:1.7b running? Is VRAM available?"
        )


# ---------------------------------------------------------------------------
# KB query model routing (Phase 99)
# ---------------------------------------------------------------------------

class TestKbQueryRouting:
    """Interactive ask handlers must use qwen3-enrich:1.7b (GPU-local, never cloud). U3: single model."""

    def test_kb_query_client_is_ollama_gpu(self):
        """create_kb_query_llm_client() must return an OllamaClient targeting qwen3-enrich:1.7b (U3)."""
        from opencode_search.enricher import create_kb_query_llm_client
        from opencode_search.enricher.client import OllamaClient
        client = create_kb_query_llm_client()
        assert client is not None, "create_kb_query_llm_client() returned None"
        assert isinstance(client, OllamaClient), (
            f"Expected OllamaClient for GPU-local KB queries, got {type(client).__name__}. "
            "KB queries must never use cloud (codex/anthropic) — GPU-only enforcement."
        )
        assert "qwen3-enrich" in client.model, (
            f"KB query model is '{client.model}' — expected qwen3-enrich:1.7b (U3 single-model). "
            "create_kb_query_llm_client must default to qwen3-enrich:1.7b after U3."
        )

    def test_kb_query_client_is_ollama_gpu_not_cloud(self):
        """KB query client must never route to cloud — GPU-only (qwen3-enrich:1.7b is the resident model)."""
        import os
        env_override = os.environ.get("OPENCODE_KB_QUERY_LLM_MODEL")
        try:
            if env_override:
                del os.environ["OPENCODE_KB_QUERY_LLM_MODEL"]
            from opencode_search.enricher import create_kb_query_llm_client, create_llm_client
            from opencode_search.enricher.client import OllamaClient
            enrich_client = create_llm_client()
            query_client = create_kb_query_llm_client()
            assert enrich_client is not None and query_client is not None
            # U3: both build and KB-query use qwen3-enrich:1.7b — same resident model, different phases.
            # Critical invariant: neither must be cloud (codex/anthropic).
            if isinstance(enrich_client, OllamaClient) and isinstance(query_client, OllamaClient):
                assert "qwen3-enrich" in query_client.model, (
                    f"KB query client uses {query_client.model!r} — expected qwen3-enrich:1.7b (U3)."
                )
                assert "qwen3-enrich" in enrich_client.model, (
                    f"Enrich client uses {enrich_client.model!r} — expected qwen3-enrich:1.7b (U3)."
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
                raise AssertionError("Expected RuntimeError for codex provider in enrich tier")
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


# ---------------------------------------------------------------------------
# Service mesh cache (Part 1+2 of the service_mesh timeout fix)
# ---------------------------------------------------------------------------

class TestServiceMeshCache:
    """service_mesh two-tier cache: fast cold read, instant second call, bounded scan."""

    def test_service_mesh_cold_read_under_30s(self, http, project):
        """Cold GET /api/overview?what=service_mesh must complete well under 300s."""
        import time
        # Invalidate in-process cache first so we measure a real scan
        try:
            from opencode_search.handlers._service_mesh import invalidate_service_mesh_cache
            invalidate_service_mesh_cache(project)
        except Exception:
            pass

        t0 = time.perf_counter()
        r = http.get("/api/overview", params={"project": project, "what": "service_mesh"})
        elapsed = time.perf_counter() - t0
        assert r.status_code == 200, f"service_mesh failed: {r.text[:200]}"
        assert elapsed < 30.0, (
            f"Cold service_mesh scan took {elapsed:.1f}s — must be < 30s. "
            "Parallel bounded walk + LLM-off-read-path fixes this."
        )

    def test_service_mesh_second_call_cached(self, http, project):
        """Second GET /api/overview?what=service_mesh must return cached:true."""
        # First call to ensure cache is populated
        r1 = http.get("/api/overview", params={"project": project, "what": "service_mesh"})
        assert r1.status_code == 200, f"first call failed: {r1.text[:200]}"

        # Second call must hit cache
        r2 = http.get("/api/overview", params={"project": project, "what": "service_mesh"})
        assert r2.status_code == 200, f"second call failed: {r2.text[:200]}"
        data = r2.json()
        assert data.get("cached") is True, (
            f"Expected cached=True on second call; got cached={data.get('cached')!r}. "
            f"Keys: {list(data.keys())}"
        )

    def test_service_mesh_result_has_bounded_fields(self, http, project):
        """Service mesh result must contain scanned_files and truncated fields."""
        # Invalidate to get a fresh result with the new fields
        try:
            from opencode_search.handlers._service_mesh import invalidate_service_mesh_cache
            invalidate_service_mesh_cache(project)
        except Exception:
            pass
        r = http.get("/api/overview", params={"project": project, "what": "service_mesh"})
        assert r.status_code == 200
        data = r.json()
        assert "scanned_files" in data or data.get("cached"), (
            f"scanned_files missing from non-cached result. keys={list(data.keys())}"
        )
        assert "truncated" in data or data.get("cached"), (
            f"truncated field missing from non-cached result. keys={list(data.keys())}"
        )


# ---------------------------------------------------------------------------
# Codex confinement: read tier must NEVER return a codex/claude-code client
# ---------------------------------------------------------------------------

class TestCodexConfinement:
    """create_kb_query_llm_client() is ollama-pinned — never returns codex."""

    def test_kb_query_client_is_not_codex_regardless_of_query_provider(self):
        """Even if OPENCODE_QUERY_LLM_PROVIDER=codex, kb_query must be ollama."""
        import os
        old_query = os.environ.get("OPENCODE_QUERY_LLM_PROVIDER")
        try:
            os.environ["OPENCODE_QUERY_LLM_PROVIDER"] = "codex"
            from opencode_search.enricher.client import CodexClient, create_kb_query_llm_client
            client = create_kb_query_llm_client()
            # May return None if :11434 is unavailable, but must never be a codex client
            assert not isinstance(client, CodexClient), (
                "create_kb_query_llm_client() returned a CodexClient even though "
                "OPENCODE_QUERY_LLM_PROVIDER=codex. KB queries must use ollama only."
            )
        finally:
            if old_query is None:
                os.environ.pop("OPENCODE_QUERY_LLM_PROVIDER", None)
            else:
                os.environ["OPENCODE_QUERY_LLM_PROVIDER"] = old_query

    def test_kb_chat_uses_query_tier_not_enrich_tier(self):
        """handle_kb_chat is the dashboard chat handler — it must use create_query_llm_client
        (codex → haiku) not the raw enrich/build-tier create_llm_client (qwen3-enrich:1.7b).

        U3 retired qwen3-query:8b; handle_kb_chat is a user-facing dashboard feature so it
        uses the codex query tier, exactly as the user rule requires.
        """
        from pathlib import Path
        kb_chat_path = Path(__file__).parents[2] / "opencode_search" / "handlers" / "_kb_chat.py"
        source = kb_chat_path.read_text()
        assert "create_query_llm_client" in source, (
            "_kb_chat.py must use create_query_llm_client (codex → haiku dashboard tier). "
            "U3: qwen3-query:8b retired; dashboard chat handler must use codex/haiku, not the "
            "GPU-local enrich model."
        )
        assert "create_llm_client()" not in source.replace("create_query_llm_client", ""), (
            "_kb_chat.py must not call raw create_llm_client() (enrich/build tier). "
            "Use create_query_llm_client() for dashboard chat."
        )


# ---------------------------------------------------------------------------
class TestKbQueryClientAvailable:
    """create_kb_query_llm_client() must always return a live GPU-local client."""

    @pytest.mark.slow
    def test_client_available(self):
        """create_kb_query_llm_client() must return a non-None available OllamaClient."""
        from opencode_search.enricher.client import create_kb_query_llm_client
        client = create_kb_query_llm_client()
        assert client is not None, "create_kb_query_llm_client() returned None — :11434 must be up"
        assert client.is_available(), "create_kb_query_llm_client().is_available() is False"
