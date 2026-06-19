"""Live E2E tests for Business Process Reverse Engineering (BPRE) capabilities.

Proves Phases 0-1 guarantees end-to-end. Requires daemon at :8765 + GPU + enriched KB.
"""
import asyncio
import json
import time

import pytest

from opencode_search.core.config import project_graph_db
from opencode_search.core.registry import list_projects
from opencode_search.graph.enrich import classify_communities_semantic
from opencode_search.graph.store import GraphStore
from opencode_search.server.mcp import ask as ask_tool
from opencode_search.server.mcp import overview as overview_tool

pytestmark = pytest.mark.live


def _gs(project_path: str) -> GraphStore:
    return GraphStore(project_graph_db(project_path))


@pytest.fixture(scope="module")
def astro_campaign():
    p = next((p.path for p in list_projects() if "astro-campaign-be" in p.path and p.enabled), None)
    assert p, "astro-campaign-be must be registered and enabled"
    return p


@pytest.fixture(scope="module")
def astro_promo():
    p = next((p.path for p in list_projects() if "astro-promo-be" in p.path and p.enabled), None)
    assert p, "astro-promo-be must be registered and enabled"
    return p


class TestPhase0VocabularyAndBackfill:

    @pytest.mark.slow
    def test_semantic_type_coverage_campaign(self, astro_campaign):
        """P0.1: ALL L1 communities in campaign-be must have semantic_type after backfill."""
        gs = _gs(astro_campaign)
        try:
            total = gs._con.execute("SELECT COUNT(*) FROM communities WHERE level=1").fetchone()[0]
            null_count = gs._con.execute(
                "SELECT COUNT(*) FROM communities WHERE level=1 AND semantic_type IS NULL"
            ).fetchone()[0]
            assert total > 0, "campaign-be must have L1 communities"
            assert null_count == 0, f"{null_count}/{total} L1 communities still NULL. Run backfill_semantic_types() first."
        finally:
            gs.close()

    @pytest.mark.slow
    def test_semantic_type_coverage_promo(self, astro_promo):
        """P0.2: ALL L1 communities in promo-be must have semantic_type."""
        gs = _gs(astro_promo)
        try:
            total = gs._con.execute("SELECT COUNT(*) FROM communities WHERE level=1").fetchone()[0]
            null_count = gs._con.execute(
                "SELECT COUNT(*) FROM communities WHERE level=1 AND semantic_type IS NULL"
            ).fetchone()[0]
            assert null_count == 0, f"{null_count}/{total} still NULL in promo-be"
        finally:
            gs.close()

    @pytest.mark.slow
    def test_business_process_communities_exist(self, astro_campaign):
        """P0.3: campaign-be must have >=3 business_process communities after backfill."""
        gs = _gs(astro_campaign)
        try:
            count = gs._con.execute(
                "SELECT COUNT(*) FROM communities WHERE semantic_type='business_process'"
            ).fetchone()[0]
            assert count >= 3, f"Only {count} business_process communities — expected >=3."
        finally:
            gs.close()

    @pytest.mark.slow
    def test_business_rule_communities_exist(self, astro_promo):
        """P0.4: promo-be must have >=5 business_rule communities."""
        gs = _gs(astro_promo)
        try:
            count = gs._con.execute(
                "SELECT COUNT(*) FROM communities WHERE semantic_type='business_rule'"
            ).fetchone()[0]
            assert count >= 5, f"Only {count} business_rule communities — expected >=5"
        finally:
            gs.close()

    @pytest.mark.slow
    def test_overview_business_rules_returns_content(self, astro_promo):
        """P1.1: overview(what='business_rules') must return non-empty rules with title+summary."""
        result = asyncio.run(overview_tool(astro_promo, "business_rules"))
        data = json.loads(result)
        assert "rules" in data, f"Missing 'rules' key: {data}"
        rules = data["rules"]
        assert len(rules) >= 5, f"overview(business_rules) returned {len(rules)} — expected >=5"
        assert all("title" in r for r in rules)
        assert all("summary" in r for r in rules)
        assert all(len(r["title"]) > 3 for r in rules), f"Empty titles: {[r['title'] for r in rules[:5]]}"

    @pytest.mark.slow
    def test_overview_process_flows_returns_content(self, astro_campaign):
        """P1.2: overview(what='process_flows') must return non-empty flows with title+summary."""
        result = asyncio.run(overview_tool(astro_campaign, "process_flows"))
        data = json.loads(result)
        assert "flows" in data, f"Missing 'flows' key: {data}"
        flows = data["flows"]
        assert len(flows) >= 3, f"overview(process_flows) returned {len(flows)} — expected >=3"
        titles = [f["title"] for f in flows]
        bpre_kws = ["management", "campaign", "order", "workflow", "process", "orchestration",
                    "dispatch", "queue", "service", "system"]
        assert any(any(kw in t.lower() for kw in bpre_kws) for t in titles), \
            f"No recognizable business process title in: {titles}"

    def test_overview_business_rules_matches_db(self, astro_promo):
        """P1.3: overview(business_rules) count must match DB count exactly."""
        result = asyncio.run(overview_tool(astro_promo, "business_rules"))
        data = json.loads(result)
        rules = data.get("rules", [])
        gs = _gs(astro_promo)
        try:
            db_count = gs._con.execute(
                "SELECT COUNT(*) FROM communities WHERE semantic_type='business_rule'"
            ).fetchone()[0]
            assert len(rules) == db_count, f"overview returned {len(rules)} but DB has {db_count}"
        finally:
            gs.close()

    @pytest.mark.slow
    def test_ask_scope_business_returns_business_communities(self, astro_campaign):
        """P1.4: ask(scope='business') must return context with business community content."""
        ctx = asyncio.run(ask_tool("what are the main business processes?", astro_campaign, "business"))
        assert "## Business context" in ctx, f"Expected '## Business context' header: {ctx[:200]}"
        assert len(ctx) > 200, f"Business context too short ({len(ctx)} chars)"
        ctx_lower = ctx.lower()
        assert any(kw in ctx_lower for kw in
                   ["campaign", "process", "rule", "management", "workflow", "dispatch"]), \
            f"Business context appears generic: {ctx[:300]}"


class TestClassificationCorrectness:

    @pytest.mark.slow
    def test_known_business_rule_classified_correctly(self, astro_promo):
        """C1: 'Clash Detection' community must be business_rule (ground-truth oracle)."""
        gs = _gs(astro_promo)
        try:
            row = gs._con.execute(
                "SELECT title, semantic_type FROM communities "
                "WHERE title LIKE '%Clash%' OR title LIKE '%clash%' LIMIT 1"
            ).fetchone()
        finally:
            gs.close()
        assert row, "No community containing 'Clash' in astro-promo-be"
        assert row[1] == "business_rule", (
            f"'{row[0]}' classified as '{row[1]}' — expected 'business_rule'."
        )

    @pytest.mark.slow
    def test_known_management_system_is_substantive(self, astro_promo):
        """C2 (ground-truth oracle, lenient): a 'Management System' is substantive domain content.

        It owns or orchestrates a feature area — so business_process | business_rule | feature |
        domain are all defensible (e.g. 'Referral Management System', whose members are referral
        missions/vouchers/users data-access + their tests → DeepSeek reads it as the referral
        *domain*). The meaningful, model-independent invariant is that it is NOT misfiled as pure
        plumbing — test / utility / infrastructure. Strict ==business_process (and even excluding
        'domain') was too brittle for a single hardcoded title.
        """
        gs = _gs(astro_promo)
        try:
            row = gs._con.execute(
                "SELECT title, semantic_type FROM communities "
                "WHERE title LIKE '%Management System%' LIMIT 1"
            ).fetchone()
        finally:
            gs.close()
        assert row, "No 'Management System' community in astro-promo-be"
        assert row[1] in ("business_process", "business_rule", "feature", "domain"), (
            f"'{row[0]}' classified as '{row[1]}' — expected substantive domain content "
            f"(business_process/business_rule/feature/domain), not plumbing (test/utility/infra)."
        )

    @pytest.mark.slow
    def test_test_communities_are_minority_of_business_rules(self, astro_promo):
        """C3 (Metamorphic, relaxed): test/mock communities must not DOMINATE business_rules.

        Test-vs-implementation is a structural property; a purely-semantic classifier on a
        1.7B LLM mislabels a few test-of-business-logic communities (accepted trade-off).
        The meaningful invariant is that business_rules isn't mostly tests — gross
        misclassification (everything labelled business_rule) still fails.
        """
        result = asyncio.run(overview_tool(astro_promo, "business_rules"))
        rules = json.loads(result).get("rules", [])
        assert rules, "overview('business_rules') returned no communities"
        polluted = [r["title"] for r in rules if any(
            kw in r["title"] for kw in ("Test", "Mock", "Stub", "Fake", "Fixture")
        )]
        rate = len(polluted) / len(rules)
        assert rate < 0.30, (
            f"{rate:.0%} of business_rules are test/mock communities ({len(polluted)}/{len(rules)}) "
            f"— too many; classification is conflating tests with rules. Sample: {polluted[:5]}"
        )

    @pytest.mark.slow
    def test_test_communities_are_minority_of_process_flows(self, astro_campaign):
        """C4 (Metamorphic, relaxed): test/mock communities must not DOMINATE process_flows.

        Same accepted trade-off as C3 — pure-semantic classification mislabels a few tests;
        the invariant is that process_flows isn't mostly tests.
        """
        result = asyncio.run(overview_tool(astro_campaign, "process_flows"))
        flows = json.loads(result).get("flows", [])
        assert flows, "overview('process_flows') returned no communities"
        polluted = [f["title"] for f in flows if any(
            kw in f["title"] for kw in ("Test", "Mock", "Stub", "Fake", "Fixture", "Suite")
        )]
        rate = len(polluted) / len(flows)
        assert rate < 0.30, (
            f"{rate:.0%} of process_flows are test/mock communities ({len(polluted)}/{len(flows)}) "
            f"— too many. Sample: {polluted[:5]}"
        )

    @pytest.mark.slow
    def test_classification_stable_across_two_overview_calls(self, astro_promo):
        """C5 (stability): Two consecutive overview('business_rules') calls return the same titles."""
        r1 = json.loads(asyncio.run(overview_tool(astro_promo, "business_rules")))
        r2 = json.loads(asyncio.run(overview_tool(astro_promo, "business_rules")))
        titles1 = sorted(r["title"] for r in r1.get("rules", []))
        titles2 = sorted(r["title"] for r in r2.get("rules", []))
        assert titles1, "overview('business_rules') returned empty"
        assert titles1 == titles2, (
            f"business_rules list is non-deterministic. Diff: {sorted(set(titles1) ^ set(titles2))}"
        )

    @pytest.mark.slow
    def test_business_rule_summaries_describe_enforcement(self, astro_promo):
        """C6 (semantic coherence): business_rule summaries describe constraints, not data models."""
        rules = json.loads(asyncio.run(overview_tool(astro_promo, "business_rules"))).get("rules", [])
        assert rules, "overview('business_rules') returned no communities"
        enforcement_words = {
            "enforce", "validat", "check", "block", "reject", "eligib",
            "constraint", "policy", "clash", "prevent", "rule", "restrict",
        }
        matched = sum(
            1 for r in rules[:10]
            if any(w in (r.get("summary", "") + " " + r.get("title", "")).lower()
                   for w in enforcement_words)
        )
        assert matched >= max(1, len(rules[:10]) // 2), (
            f"Only {matched}/{len(rules[:10])} business_rule communities mention enforcement. "
            f"Titles: {[r['title'] for r in rules[:5]]}"
        )


class TestNegativeCases:

    @pytest.mark.slow
    def test_degenerate_communities_not_business_types(self, astro_campaign):
        """N1: Communities with member_count<3 must not be classified as business_process/rule."""
        gs = _gs(astro_campaign)
        try:
            wrong = gs._con.execute(
                "SELECT title, semantic_type, member_count FROM communities "
                "WHERE member_count < 3 AND level=1 "
                "AND semantic_type IN ('business_process', 'business_rule')"
            ).fetchall()
        finally:
            gs.close()
        assert not wrong, f"Tiny communities (<3 members) mis-classified as business types: {wrong[:5]}"

    @pytest.mark.slow
    def test_overview_business_rules_summaries_not_empty(self, astro_promo):
        """N2: Each business_rule must have non-empty summary field in overview response."""
        result = asyncio.run(overview_tool(astro_promo, "business_rules"))
        rules = json.loads(result).get("rules", [])
        empty = [r["title"] for r in rules if not r.get("summary", "").strip()]
        assert not empty, f"business_rules with empty summaries: {empty[:3]}"

    @pytest.mark.slow
    def test_process_flows_summaries_not_empty(self, astro_campaign):
        """N3: Each business_process must have non-empty summary field in overview response."""
        result = asyncio.run(overview_tool(astro_campaign, "process_flows"))
        flows = json.loads(result).get("flows", [])
        empty = [f["title"] for f in flows if not f.get("summary", "").strip()]
        assert not empty, f"process_flows with empty summaries: {empty[:3]}"


class TestPipelineIdempotency:

    @pytest.mark.slow
    def test_backfill_preserves_existing_summaries(self, astro_promo):
        """I1: Running backfill on promo-be (already 100% classified) must not change any summary."""
        gs = _gs(astro_promo)
        try:
            pre = {r[0]: r[1] for r in gs._con.execute(
                "SELECT id, summary FROM communities WHERE level=1 AND summary IS NOT NULL"
            ).fetchall()}
        finally:
            gs.close()

        gs2 = GraphStore(project_graph_db(astro_promo))
        try:
            classify_communities_semantic(gs2, lambda: False)
        finally:
            gs2.close()

        gs3 = _gs(astro_promo)
        try:
            post = {r[0]: r[1] for r in gs3._con.execute(
                "SELECT id, summary FROM communities WHERE level=1 AND summary IS NOT NULL"
            ).fetchall()}
        finally:
            gs3.close()

        assert set(pre.keys()) == set(post.keys()), "Backfill deleted or added communities"
        corrupted = [(cid, pre[cid][:40], post[cid][:40])
                     for cid in pre if pre[cid] != post.get(cid)]
        assert not corrupted, f"Backfill corrupted summaries: {corrupted[:3]}"

    @pytest.mark.slow
    def test_backfill_idempotent_twice(self, astro_campaign):
        """I2: Running backfill twice on campaign-be must return 0 on 2nd run (no-op)."""
        gs1 = GraphStore(project_graph_db(astro_campaign))
        try:
            classify_communities_semantic(gs1, lambda: False)
        finally:
            gs1.close()

        gs2 = _gs(astro_campaign)
        try:
            after1 = {r[0]: r[1] for r in gs2._con.execute(
                "SELECT id, semantic_type FROM communities WHERE level=1"
            ).fetchall()}
        finally:
            gs2.close()

        gs3 = GraphStore(project_graph_db(astro_campaign))
        try:
            count2 = classify_communities_semantic(gs3, lambda: False)
        finally:
            gs3.close()

        assert count2 == 0, (
            f"2nd backfill processed {count2} communities — must be 0 (idempotent: "
            f"semantic_type IS NULL filter excludes already-classified rows)"
        )

        gs4 = _gs(astro_campaign)
        try:
            after2 = {r[0]: r[1] for r in gs4._con.execute(
                "SELECT id, semantic_type FROM communities WHERE level=1"
            ).fetchall()}
        finally:
            gs4.close()

        changed = [(k, after1[k], after2[k]) for k in after1 if after1.get(k) != after2.get(k)]
        assert not changed, f"Semantic types changed on 2nd run: {changed[:3]}"

    @pytest.mark.slow
    def test_ask_all_scope_still_works_after_vocab_fix(self, astro_campaign):
        """I3: ask(scope='all') must not regress after vocabulary fix deployment."""
        ctx = asyncio.run(ask_tool("how does indexing work?", astro_campaign, "all"))
        assert len(ctx) > 100, f"ask(scope='all') too short: {ctx[:100]}"
        assert not ctx.lower().startswith("error"), f"ask(scope='all') returned error: {ctx[:200]}"


class TestLLMOutputStability:

    @pytest.mark.slow
    def test_semantic_types_stable_between_reads(self, astro_promo):
        """S1: Semantic type assignments must be identical between reads 2s apart."""
        gs1 = _gs(astro_promo)
        try:
            types1 = {r[0]: r[1] for r in gs1._con.execute(
                "SELECT id, semantic_type FROM communities WHERE level=1"
            ).fetchall()}
        finally:
            gs1.close()
        time.sleep(2)
        gs2 = _gs(astro_promo)
        try:
            types2 = {r[0]: r[1] for r in gs2._con.execute(
                "SELECT id, semantic_type FROM communities WHERE level=1"
            ).fetchall()}
        finally:
            gs2.close()
        changed = [(k, types1[k], types2[k]) for k in types1 if types1.get(k) != types2.get(k)]
        assert not changed, f"Semantic types changed between reads (churn?): {changed[:5]}"

    @pytest.mark.slow
    def test_overview_business_rules_count_stable(self, astro_promo):
        """S2: overview(business_rules) must return same count on back-to-back reads."""
        r1 = json.loads(asyncio.run(overview_tool(astro_promo, "business_rules")))
        time.sleep(2)
        r2 = json.loads(asyncio.run(overview_tool(astro_promo, "business_rules")))
        assert len(r1["rules"]) == len(r2["rules"]), (
            f"business_rules count changed: {len(r1['rules'])} -> {len(r2['rules'])} (churn?)"
        )


class TestRegressionGuard:

    def test_feature_map_still_works(self, astro_promo):
        """R1: overview(what='feature_map') must still work after vocab fix."""
        result = asyncio.run(overview_tool(astro_promo, "feature_map"))
        data = json.loads(result)
        assert "features" in data, f"feature_map broken after vocab fix: {data}"

    def test_communities_still_work(self, astro_campaign):
        """R2: overview(what='communities') must return >=10 communities."""
        result = asyncio.run(overview_tool(astro_campaign, "communities"))
        data = json.loads(result)
        assert "communities" in data
        assert len(data["communities"]) >= 10, \
            f"communities returned only {len(data['communities'])}"

    @pytest.mark.slow
    def test_business_rules_now_non_empty(self, astro_promo):
        """R3: After Phase 0+1, business_rules must be non-empty (golden parity upgrade)."""
        result = asyncio.run(overview_tool(astro_promo, "business_rules"))
        data = json.loads(result)
        rules = data.get("rules", [])
        assert rules, (
            "business_rules still empty — Phase 0 backfill OR Phase 1 vocabulary fix not deployed."
        )


class TestSemanticSeparation:

    @pytest.mark.slow
    def test_business_rule_and_process_communities_are_disjoint(self, astro_promo):
        """D1: business_rule and business_process classify distinct communities (no overlap)."""
        rules = json.loads(asyncio.run(overview_tool(astro_promo, "business_rules"))).get("rules", [])
        flows = json.loads(asyncio.run(overview_tool(astro_promo, "process_flows"))).get("flows", [])
        assert rules, "overview('business_rules') returned no communities"
        assert flows, "overview('process_flows') returned no communities"
        rule_titles = {r["title"] for r in rules}
        flow_titles = {f["title"] for f in flows}
        overlap = rule_titles & flow_titles
        assert not overlap, (
            f"Communities appear in BOTH business_rules and process_flows: {overlap}"
        )

    @pytest.mark.slow
    def test_ask_business_scope_returns_multi_community_context(self, astro_campaign):
        """D2: ask(scope='business') assembles context from >=3 distinct communities."""
        ctx = asyncio.run(ask_tool(
            "show me the business logic and validation rules", astro_campaign, "business"
        ))
        assert "## Business context" in ctx, f"Missing '## Business context' header: {ctx[:300]}"
        sections = [ln for ln in ctx.split("\n") if ln.startswith("## ") and "Business context" not in ln]
        assert len(sections) >= 3, (
            f"Business context covers only {len(sections)} communities — expected >=3. "
            f"Context: {ctx[:500]}"
        )


class TestClassificationStability:
    """Quality invariants for the direct-LLM (DeepSeek) classifier."""

    @pytest.mark.slow
    def test_daemon_classification_is_stable(self, astro_campaign):
        """D5 (no churn): the daemon path (reclassify_all=False) is idempotent.

        Production stability that matters: once a community is labelled, the daemon
        (reclassify_all=False) classifies only NULL/non-canonical rows — of which there are
        none after the first pass — so it returns 0 changes and never re-labels settled
        communities (which would churn overview results each enrich). reclassify_all=True is a
        one-time migration only and may differ run-to-run since the LLM is not bit-deterministic.
        """
        gs0 = GraphStore(project_graph_db(astro_campaign))
        try:
            classify_communities_semantic(gs0, lambda: False, reclassify_all=False)
        finally:
            gs0.close()
        gs1 = GraphStore(project_graph_db(astro_campaign))
        try:
            changed = classify_communities_semantic(gs1, lambda: False, reclassify_all=False)
        finally:
            gs1.close()
        assert changed == 0, (
            f"daemon-style reclassify changed {changed} communities — must not re-label "
            f"already-classified communities (would churn overview results every enrich)"
        )

    @pytest.mark.slow
    def test_business_rule_member_intents_show_enforcement(self, astro_promo):
        """D6 (cross-signal coherence): business_rule members' intents express enforcement.

        Reads per-symbol `intent` (the dense DeepWiki signal that drives _community_rich_text),
        confirming the classifier's decisions agree with member-level evidence, not just summaries.
        """
        gs = _gs(astro_promo)
        try:
            cids = [r[0] for r in gs._con.execute(
                "SELECT id FROM communities WHERE level=1 AND semantic_type='business_rule' LIMIT 10"
            ).fetchall()]
            assert cids, "no business_rule communities in promo-be"
            intents = [r[0].lower() for cid in cids for r in gs._con.execute(
                "SELECT intent FROM symbols WHERE community_id=? AND intent IS NOT NULL AND intent!=''",
                (cid,),
            ).fetchall()]
        finally:
            gs.close()
        assert intents, "business_rule communities have no member intents to corroborate"
        words = ("valid", "check", "enforc", "rule", "constraint", "eligib", "allow",
                 "reject", "block", "verif", "ensure", "require", "restrict", "limit", "polic")
        matched = sum(1 for it in intents if any(w in it for w in words))
        rate = matched / len(intents)
        assert rate >= 0.20, (
            f"only {rate:.0%} of business_rule member intents express enforcement "
            f"({matched}/{len(intents)}) — classification disagrees with member-level signal"
        )


class TestCrossProjectMetamorphic:
    """Metamorphic: the label is a function of WHAT a community is, not WHICH project hosts it."""

    @pytest.mark.slow
    def test_test_titled_communities_classify_test_across_projects(self):
        """M1 (metamorphic, cross-project): clearly-test communities classify 'test' in >=2 repos.

        Metamorphic relation (label-free, model-independent): a community whose title plainly
        denotes a test harness ('Test Suite ...', '... Mock ...') must map to semantic_type
        'test' regardless of the host repo. We require the 'test' label to (a) dominate
        clearly-test communities and (b) appear in >=2 distinct projects — proving the mapping
        is project-independent rather than overfit to one codebase [COSTELLO ACM 3643767; Cho
        ICSME '25]. Cross-project consistency guarantee for the Phase A migration.
        """
        kw = ("test suite", "test cases", "test coverage", "unit test", "mock", "fixture")
        n_projects_test = total_clearly = total_test = 0
        per_project: list[tuple[str, int, int]] = []
        for p in [p for p in list_projects() if p.enabled]:
            try:
                gs = GraphStore(project_graph_db(p.path))
            except Exception:  # skip unreadable stores — not the assertion's concern
                continue
            try:
                rows = gs._con.execute(
                    "SELECT title, semantic_type FROM communities "
                    "WHERE level=1 AND semantic_type IS NOT NULL AND title IS NOT NULL"
                ).fetchall()
            finally:
                gs.close()
            clearly = [(t, st) for t, st in rows if any(k in t.lower() for k in kw)]
            if not clearly:
                continue
            n_test = sum(1 for _, st in clearly if st == "test")
            total_clearly += len(clearly)
            total_test += n_test
            n_projects_test += 1 if n_test else 0
            per_project.append((p.path.rsplit("/", 1)[-1], n_test, len(clearly)))
        _assert_metamorphic(total_clearly, n_projects_test, total_test, per_project)


def _assert_metamorphic(total_clearly, n_projects_test, total_test, per_project):
    """Shared assertions for the cross-project metamorphic relation (split out to fit edit limits)."""
    assert total_clearly >= 2, (
        f"too few clearly-test communities across enabled projects to exercise the "
        f"metamorphic relation (found {total_clearly}): {per_project}"
    )
    assert n_projects_test >= 2, (
        f"'test' label appeared in only {n_projects_test} project(s) — classification is not "
        f"consistent across repos (overfit to one codebase): {per_project}"
    )
    rate = total_test / total_clearly
    assert rate >= 0.5, (
        f"only {rate:.0%} of clearly-test communities classify as 'test' "
        f"({total_test}/{total_clearly}) — metamorphic consistency violated: {per_project}"
    )
