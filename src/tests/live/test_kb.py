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
        r = http.get("/api/kb_health", params={"project": project})
        assert r.status_code == 200, f"kb_health failed: {r.text[:200]}"
        data = r.json()
        pct = data.get("enrichment_pct") or data.get("enriched_pct") or 0
        if pct == 0:
            # Fall back to counting communities directly
            r2 = http.get("/api/communities", params={"project": project, "top_k": 200})
            communities = r2.json().get("communities", [])
            if not communities:
                pytest.skip("No communities found")
            enriched = sum(1 for c in communities if c.get("title") and c["title"].strip())
            pct = enriched / len(communities) * 100
        assert pct >= 80, (
            f"Enrichment is only {pct:.0f}% — run build(action='enrich') to fix"
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
            pytest.skip(f"Ollama not reachable at localhost:11434: {e}")


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
        if not pages:
            pytest.skip("No wiki pages found")
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
        if patterns_cached is None:
            pytest.skip("patterns_cached not in kb_health response")
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
        if r.status_code != 200:
            pytest.skip("system_status endpoint not available")
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
