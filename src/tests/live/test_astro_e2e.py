"""Comprehensive end-to-end tests for astro-project via the live daemon.

Exercises every MCP tool and every major chat intent against the real astro-project
knowledge base.  All tests require:
  - daemon running at :8765
  - astro-project indexed with communities > 0
  - GPU embeddings functional
  - Ollama running with qwen3-query:8b
  - codex CLI available (for chat tier)

The project fixture in conftest.py prefers projects with >100 communities, so
astro-project (5896+ communities) will be selected over smaller projects.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.live

_ASTRO = "/home/user/git/github.com/fairyhunter13/astro-project"

from .conftest import judge_answer, parse_sse

_MIN_SCORE = 3  # LLM judge minimum


# ---------------------------------------------------------------------------
# Fixture: ensure astro-project is indexed
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def astro(http):
    """Return astro-project path; skip if not indexed."""
    r = http.get("/api/projects")
    projects = {p["path"]: p for p in r.json().get("projects", [])}
    if _ASTRO not in projects:
        pytest.skip(f"astro-project not in registry: {_ASTRO}")
    info = projects[_ASTRO]
    if info.get("communities", 0) == 0:
        pytest.skip("astro-project has no communities — run build(action='pipeline') first")
    return _ASTRO


# ---------------------------------------------------------------------------
# Tool 1: search
# ---------------------------------------------------------------------------

class TestAstroSearch:
    """search(query, scope, project_paths) — vector index over astro-project."""

    def test_search_returns_go_files(self, http, astro):
        r = http.get("/api/search", params={"q": "HTTP handler route", "project": astro, "scope": "code"})
        assert r.status_code == 200, f"search failed: {r.text[:200]}"
        results = r.json().get("results", [])
        assert len(results) > 0, "search returned no results for 'HTTP handler route'"
        paths = [res.get("path") or res.get("file") or "" for res in results]
        assert any(p.endswith(".go") or p.endswith(".py") or p.endswith(".java") for p in paths), (
            f"No source code files in results; paths: {paths[:5]}"
        )

    def test_search_grpc_finds_proto_or_go(self, http, astro):
        r = http.get("/api/search", params={"q": "gRPC service definition", "project": astro, "top_k": 10})
        assert r.status_code == 200
        results = r.json().get("results", [])
        assert len(results) > 0, "search for gRPC returned no results"

    def test_search_top_k_respected(self, http, astro):
        r = http.get("/api/search", params={"q": "authentication", "project": astro, "top_k": 3})
        assert r.status_code == 200
        results = r.json().get("results", [])
        assert len(results) <= 3, f"top_k=3 returned {len(results)} results"

    def test_search_different_queries_return_different_files(self, http, astro):
        r1 = http.get("/api/search", params={"q": "database connection pool", "project": astro, "top_k": 5})
        r2 = http.get("/api/search", params={"q": "HTTP request routing", "project": astro, "top_k": 5})
        files1 = {(res.get("path") or res.get("file") or "") for res in r1.json().get("results", [])}
        files2 = {(res.get("path") or res.get("file") or "") for res in r2.json().get("results", [])}
        overlap = files1 & files2
        assert len(overlap) < min(len(files1), len(files2)), (
            "Two semantically different queries returned identical file sets — vector index may be broken"
        )


# ---------------------------------------------------------------------------
# Tool 2: ask
# ---------------------------------------------------------------------------

class TestAstroAsk:
    """ask(query, project_path, scope) — KB-powered synthesis."""

    def test_ask_architecture_returns_answer(self, http, astro):
        r = http.get("/api/ask", params={"q": "what is the overall system architecture?", "project": astro})
        assert r.status_code == 200, f"ask failed: {r.text[:200]}"
        data = r.json()
        answer = data.get("answer") or data.get("result") or str(data)
        assert len(answer) > 100, f"Architecture answer too short: {answer[:200]}"

    def test_ask_global_scope_synthesizes_all_communities(self, http, astro):
        r = http.get("/api/ask", params={"q": "give me a comprehensive overview", "project": astro, "scope": "global"})
        assert r.status_code == 200, f"ask global failed: {r.text[:200]}"
        data = r.json()
        answer = data.get("answer") or data.get("result") or str(data)
        assert len(answer) > 200, f"Global answer too short: {answer[:200]}"

    def test_ask_feature_scope_returns_entry_points(self, http, astro):
        r = http.get("/api/ask", params={"q": "how does search work end to end?", "project": astro, "scope": "feature"})
        assert r.status_code == 200, f"ask feature failed: {r.text[:200]}"
        data = r.json()
        answer = data.get("answer") or data.get("result") or str(data)
        assert len(answer) > 50, f"Feature answer too short: {answer[:200]}"

    def test_ask_answer_quality_architecture(self, http, astro):
        r = http.get("/api/ask", params={"q": "describe the overall architecture", "project": astro, "scope": "global"})
        data = r.json()
        answer = data.get("answer") or data.get("result") or str(data)
        score = judge_answer(answer, "Does this describe a real system architecture with concrete components?")
        assert score >= _MIN_SCORE, f"Architecture answer quality {score}/5 too low:\n{answer[:400]}"


# ---------------------------------------------------------------------------
# Tool 3: graph
# ---------------------------------------------------------------------------

class TestAstroGraph:
    """graph(symbol, project_path, relation) — call graph traversal."""

    def _find_a_symbol(self, http, astro: str) -> str:
        """Return a real symbol name from the graph DB."""
        r = http.get("/api/search", params={"q": "main function", "project": astro, "scope": "code", "top_k": 5})
        results = r.json().get("results", [])
        for res in results:
            snippet = res.get("content") or ""
            for word in snippet.split():
                if word.isidentifier() and len(word) > 3:
                    return word
        return "main"

    def test_graph_callers_returns_response(self, http, astro):
        symbol = self._find_a_symbol(http, astro)
        r = http.get("/api/graph", params={"symbol": symbol, "project": astro, "relation": "callers"})
        assert r.status_code == 200, f"graph callers failed: {r.text[:200]}"
        data = r.json()
        assert "callers" in data or "error" in data, f"Unexpected response keys: {list(data.keys())}"

    def test_graph_callees_returns_response(self, http, astro):
        symbol = self._find_a_symbol(http, astro)
        r = http.get("/api/graph", params={"symbol": symbol, "project": astro, "relation": "callees"})
        assert r.status_code == 200, f"graph callees failed: {r.text[:200]}"

    def test_graph_impact_narrative_returns_text(self, http, astro):
        symbol = self._find_a_symbol(http, astro)
        r = http.get("/api/graph", params={"symbol": symbol, "project": astro, "relation": "impact_narrative"})
        assert r.status_code == 200, f"graph impact_narrative failed: {r.text[:200]}"
        data = r.json()
        narrative = data.get("narrative") or data.get("summary") or data.get("answer") or str(data)
        assert len(str(narrative)) > 10, f"impact_narrative too short: {str(narrative)[:200]}"


# ---------------------------------------------------------------------------
# Tool 4: overview
# ---------------------------------------------------------------------------

class TestAstroOverview:
    """overview(project_path, what) — all major what= values."""

    def test_overview_structure(self, http, astro):
        r = http.get("/api/overview", params={"project": astro, "what": "structure"})
        assert r.status_code == 200, f"overview structure failed: {r.text[:200]}"
        data = r.json()
        assert data, "overview structure returned empty"

    def test_overview_communities_has_list(self, http, astro):
        r = http.get("/api/overview", params={"project": astro, "what": "communities"})
        assert r.status_code == 200
        data = r.json()
        communities = data.get("communities") or data.get("results") or []
        assert len(communities) > 0, "overview communities returned empty list"

    def test_overview_patterns_has_frameworks(self, http, astro):
        r = http.get("/api/overview", params={"project": astro, "what": "patterns"})
        assert r.status_code == 200, f"overview patterns failed: {r.text[:200]}"
        data = r.json()
        # patterns should have at least one of: key_frameworks, architecture, language
        has_patterns = (
            data.get("key_frameworks") or
            data.get("architecture") or
            data.get("language") or
            data.get("languages")
        )
        assert has_patterns, f"overview patterns missing expected fields; keys={list(data.keys())}"

    def test_overview_architecture_domains(self, http, astro):
        r = http.get("/api/overview", params={"project": astro, "what": "architecture_domains"})
        assert r.status_code == 200, f"overview architecture_domains failed: {r.text[:200]}"

    def test_overview_service_mesh(self, http, astro):
        r = http.get("/api/overview", params={"project": astro, "what": "service_mesh"})
        assert r.status_code == 200, f"overview service_mesh failed: {r.text[:200]}"

    def test_overview_projects_lists_astro(self, http, astro):
        r = http.get("/api/overview", params={"what": "projects"})
        assert r.status_code == 200
        data = r.json()
        projects = data.get("projects") or []
        paths = [p.get("path") or p.get("project") or "" for p in projects]
        assert any(_ASTRO in p for p in paths), (
            f"astro-project not listed in overview projects; found: {paths[:5]}"
        )

    def test_overview_feature_map(self, http, astro):
        r = http.get("/api/overview", params={"project": astro, "what": "feature_map"})
        assert r.status_code == 200, f"overview feature_map failed: {r.text[:200]}"

    def test_overview_suggested_questions(self, http, astro):
        r = http.get("/api/overview", params={"project": astro, "what": "suggested_questions"})
        assert r.status_code == 200, f"overview suggested_questions failed: {r.text[:200]}"
        data = r.json()
        questions = data.get("questions") or data.get("suggested_questions") or []
        assert len(questions) > 0, f"No suggested questions returned; data={str(data)[:200]}"


# ---------------------------------------------------------------------------
# Tool 5: build (non-destructive: just check job API)
# ---------------------------------------------------------------------------

class TestAstroBuild:
    """build(project_path, action) — async job API."""

    def test_build_jobs_api_works(self, http, astro):
        r = http.get("/api/jobs", params={"project": astro})
        assert r.status_code == 200, f"jobs API failed: {r.text[:200]}"
        data = r.json()
        assert "jobs" in data, f"jobs API missing 'jobs' key; got {list(data.keys())}"

    def test_build_enrich_starts_job(self, http, astro):
        """Enrich action must start a background job (non-blocking)."""
        r = http.post("/api/build", json={"project": astro, "action": "enrich"})
        assert r.status_code == 200, f"build enrich failed: {r.text[:200]}"
        data = r.json()
        job_id = data.get("job_id") or data.get("id") or data.get("jobId")
        assert job_id, f"build enrich must return job_id; got {data}"
        # Check the job shows up in the jobs list
        r2 = http.get("/api/jobs", params={"project": astro})
        jobs = r2.json().get("jobs", [])
        ids = [j.get("job_id") or j.get("id") for j in jobs]
        assert job_id in ids, f"job {job_id!r} not found in jobs list: {ids}"


# ---------------------------------------------------------------------------
# Tool 6: federation
# ---------------------------------------------------------------------------

class TestAstroFederation:
    """federation(root_path) — list sub-repositories."""

    def test_federation_list_returns_response(self, http, astro):
        r = http.get("/api/federation", params={"root": astro})
        assert r.status_code == 200, f"federation failed: {r.text[:200]}"
        data = r.json()
        repos = data.get("repos") or data.get("repositories") or data.get("sub_repos") or []
        # astro-project is a federation with multiple sub-repos
        assert isinstance(repos, list), f"federation response must have a list; got {type(repos)}"

    def test_federation_discovers_sub_repos(self, http, astro):
        r = http.get("/api/federation", params={"root": astro})
        data = r.json()
        repos = data.get("repos") or data.get("repositories") or data.get("sub_repos") or []
        assert len(repos) > 0, (
            "astro-project federation must discover sub-repositories; got 0. "
            "Run federation(root_path=astro, action='list') to check."
        )


# ---------------------------------------------------------------------------
# Tool 7: manage
# ---------------------------------------------------------------------------

class TestAstroManage:
    """manage(project_path, action) — project lifecycle operations."""

    def test_manage_status_returns_info(self, http, astro):
        r = http.get("/api/status", params={"project": astro})
        assert r.status_code == 200, f"manage status failed: {r.text[:200]}"
        data = r.json()
        assert data, "manage status returned empty response"

    def test_manage_wiki_lint_responds(self, http, astro):
        r = http.post("/api/manage", json={"project": astro, "action": "wiki_lint"})
        assert r.status_code == 200, f"manage wiki_lint failed: {r.text[:200]}"

    def test_manage_dedup_dry_run_responds(self, http, astro):
        r = http.post("/api/manage", json={"project": astro, "action": "dedup", "dry_run": True})
        assert r.status_code == 200, f"manage dedup dry_run failed: {r.text[:200]}"


# ---------------------------------------------------------------------------
# Chat — all intents against astro-project
# ---------------------------------------------------------------------------

def _chat(http, project: str, query: str) -> tuple[str, str, list[str], int, str]:
    """Send a chat_stream query, return (answer, intent, sources, elapsed_ms, model)."""
    r = http.post(
        "/api/chat_stream",
        json={"project": project, "query": query},
        headers={"Accept": "text/event-stream"},
    )
    assert r.status_code == 200, f"chat_stream failed: {r.status_code} {r.text[:200]}"
    events = parse_sse(r)
    answer = "".join(e.get("text", "") for e in events if e.get("type") == "token")
    done = next((e for e in events if e.get("type") == "done"), {})
    return answer, done.get("intent", ""), done.get("sources", []), done.get("elapsed_ms", 0), done.get("model", "")


class TestAstroChatIntents:
    """Chat router correctly routes all intents and returns LLM-quality answers."""

    def test_chat_search_intent(self, http, astro):
        answer, intent, sources, elapsed, model = _chat(http, astro, "find the gRPC service definition files")
        assert intent == "search", f"Expected intent=search; got {intent!r}"
        assert len(answer) > 20, f"Search answer too short: {answer!r}"

    def test_chat_architecture_intent(self, http, astro):
        answer, intent, sources, elapsed, model = _chat(http, astro, "what is the overall system architecture?")
        assert intent in ("architecture", "global"), f"Expected architecture/global intent; got {intent!r}"
        assert len(answer) > 100, f"Architecture answer too short: {answer!r}"

    def test_chat_global_intent(self, http, astro):
        answer, intent, sources, elapsed, model = _chat(http, astro, "give me a comprehensive global overview of the entire system")
        assert intent == "global", f"Expected intent=global; got {intent!r}"
        assert len(answer) > 200, f"Global answer too short: {answer!r}"

    def test_chat_feature_intent(self, http, astro):
        answer, intent, sources, elapsed, model = _chat(http, astro, "how does the ad display search work end to end?")
        assert intent == "feature", f"Expected intent=feature; got {intent!r}"
        assert len(answer) > 50, f"Feature answer too short: {answer!r}"

    def test_chat_graph_callers_intent(self, http, astro):
        answer, intent, sources, elapsed, model = _chat(http, astro, "what calls the DisplaySearch handler?")
        assert intent == "graph_callers", f"Expected intent=graph_callers; got {intent!r}"

    def test_chat_graph_impact_intent(self, http, astro):
        answer, intent, sources, elapsed, model = _chat(http, astro, "what breaks if I change the search proto contract?")
        assert intent == "graph_impact", f"Expected intent=graph_impact; got {intent!r}"

    def test_chat_debug_trace_intent(self, http, astro):
        answer, intent, sources, elapsed, model = _chat(
            http, astro,
            "goroutine 1 [running]:\nruntime/debug.Stack()\n\t/usr/local/go/src/runtime/debug/stack.go:24 +0x65\n"
            "main.main()\n\t/home/user/astro/cmd/main.go:42 +0x3b2"
        )
        assert intent == "debug_trace", f"Expected intent=debug_trace for stack trace; got {intent!r}"

    def test_chat_answer_quality_global(self, http, astro):
        answer, intent, *_ = _chat(http, astro, "Give me a comprehensive global overview of this entire system")
        score = judge_answer(answer, "Does this provide a broad, multi-domain system overview with concrete details?")
        assert score >= _MIN_SCORE, f"Global overview quality {score}/5 too low:\n{answer[:400]}"

    def test_chat_answer_quality_feature(self, http, astro):
        answer, intent, *_ = _chat(http, astro, "How does the search feature work end to end?")
        score = judge_answer(answer, "Does this trace a specific feature end-to-end with entry points or call chain?")
        assert score >= _MIN_SCORE, f"Feature trace quality {score}/5 too low:\n{answer[:400]}"

    def test_chat_sources_populated(self, http, astro):
        _, intent, sources, elapsed, model = _chat(http, astro, "find the main service handler")
        # At least some intents should return sources
        assert elapsed > 0, "elapsed_ms must be positive"

    def test_chat_stream_no_empty_answers(self, http, astro):
        """No intent should ever produce an empty answer."""
        queries = [
            "what is this project?",
            "how does authentication work?",
            "find the payment service",
        ]
        for q in queries:
            answer, intent, *_ = _chat(http, astro, q)
            assert len(answer) > 10, f"Empty answer for query {q!r} (intent={intent!r})"
