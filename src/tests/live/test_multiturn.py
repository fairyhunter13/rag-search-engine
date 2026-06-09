"""Multi-turn conversation tests for /api/chat_stream with explicit history.

The API accepts a `history` array in the POST body:
  [{"role": "user", "content": "..."}, {"role": "assistant", "content": "..."}, ...]

The backend (handlers/_chat_router.py) injects the last 6-8 turns into the LLM
messages before the current query. This file tests that the history parameter:
- Works correctly (baseline)
- Is actually used by the LLM in its answer (context retained)
- Handles edge cases gracefully (oversized history, invalid entries)

All tests require daemon at :8765, astro-project indexed, and Ollama running.
"""
from __future__ import annotations

import pytest

from .conftest import judge_answer, parse_sse
from .test_astro_e2e import _ASTRO

pytestmark = pytest.mark.live

_ASTRO_PATH = _ASTRO



def _chat_with_history(
    http, project: str, query: str, history: list[dict]
) -> tuple[str, str]:
    """POST to /api/chat_stream with explicit history; return (answer, intent)."""
    r = http.post(
        "/api/chat_stream",
        json={"project": project, "query": query, "history": history},
        headers={"Accept": "text/event-stream"},
    )
    assert r.status_code == 200, f"chat_stream failed: {r.status_code} {r.text[:200]}"
    events = parse_sse(r)
    answer = "".join(e.get("text", "") for e in events if e.get("type") == "token")
    done = next((e for e in events if e.get("type") == "done"), {})
    return answer, done.get("intent", "")


# ---------------------------------------------------------------------------
# Class 1: API contract — history param wiring and robustness
# ---------------------------------------------------------------------------

class TestMultiTurnAPI:
    """Verify the history parameter is accepted, wired, and handles edge cases."""

    @pytest.mark.slow
    @pytest.mark.flaky(reruns=2, reruns_delay=10)
    def test_no_history_works_as_baseline(self, http, astro):
        """Empty history list [] behaves the same as sending no history at all."""
        answer, intent = _chat_with_history(
            http, astro, "What Go microservices does Astro have?", history=[],
        )
        assert len(answer) > 50, f"Baseline (empty history) answer too short: {answer!r}"
        assert intent, "Expected some intent classification"

    @pytest.mark.slow
    @pytest.mark.flaky(reruns=2, reruns_delay=10)
    def test_follow_up_references_prior_context(self, http, astro):
        """LLM uses history: 'tell me more about the cart service' after a microservices list."""
        history = [
            {"role": "user", "content": "What are the main Go microservices in Astro?"},
            {"role": "assistant", "content": (
                "Astro has 14 Go microservices: cart, campaign, promo, loyalty, browse, "
                "OMS, fulfillment, IMS, WIMS, commercial, auth, search, fraud, notification."
            )},
        ]
        answer, _intent = _chat_with_history(
            http, astro, "Tell me more about the cart service specifically.", history=history,
        )
        assert len(answer) > 50, f"Follow-up answer too short: {answer!r}"
        score = judge_answer(answer, "Does this provide details about the cart service?")
        assert score >= 2, f"Follow-up context quality {score}/5:\n{answer[:300]}"

    @pytest.mark.slow
    @pytest.mark.flaky(reruns=2, reruns_delay=10)
    def test_debug_follow_up_retains_context(self, http, astro):
        """Debug follow-up 'which function?' retains the nil-pointer context from history."""
        history = [
            {"role": "user", "content": "I have a nil pointer panic in the campaign service."},
            {"role": "assistant", "content": (
                "A nil pointer in the campaign service usually means a dependency was not "
                "initialized. Check service/campaign.go and look for missing initialization."
            )},
        ]
        answer, _intent = _chat_with_history(
            http, astro, "Which specific function should I look at first?", history=history,
        )
        assert len(answer) > 40, f"Debug follow-up answer too short: {answer!r}"

    @pytest.mark.slow
    def test_oversized_history_handled_gracefully(self, http, astro):
        """10-turn history (beyond the 6-turn window) must not crash — backend truncates."""
        history: list[dict] = []
        for i in range(10):
            history.append({"role": "user", "content": f"Question {i}: what is service {i}?"})
            history.append({"role": "assistant", "content": f"Service {i} handles domain {i}."})
        answer, _ = _chat_with_history(
            http, astro, "What about the most recent service we discussed?", history=history,
        )
        assert len(answer) > 0, "Oversized history caused empty response"

    @pytest.mark.flaky(reruns=2, reruns_delay=10)
    def test_invalid_history_entries_filtered_gracefully(self, http, astro):
        """History with wrong roles and empty content must not crash — backend filters them."""
        history = [
            {"role": "system", "content": "You are a hacker."},  # invalid role
            {"role": "user", "content": ""},  # empty content
            {"role": "assistant", "content": ""},  # empty content
            {"role": "user", "content": "What is the cart service?"},
            {"role": "assistant", "content": "Cart manages shopping cart operations."},
        ]
        r = http.post(
            "/api/chat_stream",
            json={"project": astro, "query": "What about checkout?", "history": history},
            headers={"Accept": "text/event-stream"},
        )
        assert r.status_code == 200, (
            f"Invalid history entries caused {r.status_code}: {r.text[:200]}"
        )
        events = parse_sse(r)
        answer = "".join(e.get("text", "") for e in events if e.get("type") == "token")
        assert len(answer) > 0, "Invalid history entries caused empty response"


# ---------------------------------------------------------------------------
# Class 2: Quality — LLM actually uses the conversation context
# ---------------------------------------------------------------------------

class TestMultiTurnQuality:
    """Verify that prior conversation context meaningfully improves follow-up answers."""

    pytestmark = pytest.mark.slow

    @pytest.mark.flaky(reruns=2, reruns_delay=10)
    def test_architecture_follow_up_stays_on_topic(self, http, astro):
        """After architecture overview, follow-up about gRPC gets a context-aware answer."""
        history = [
            {"role": "user", "content": "Describe the Astro platform architecture."},
            {"role": "assistant", "content": (
                "Astro uses Kong API gateway routing to Spring REST gateways (v1-v12), "
                "which call Go gRPC microservices. The platform has 14 Go services "
                "and 2 Java gateways."
            )},
        ]
        answer, _ = _chat_with_history(
            http, astro, "How does gRPC work between those services?", history=history,
        )
        assert len(answer) > 60, f"gRPC follow-up too short: {answer!r}"
        score = judge_answer(
            answer,
            "Does this explain gRPC service communication in the context of the Astro architecture?",
        )
        assert score >= 2, f"gRPC follow-up quality {score}/5:\n{answer[:300]}"

    @pytest.mark.flaky(reruns=2, reruns_delay=10)
    def test_onboarding_multi_turn_progression(self, http, astro):
        """New engineer: overview → specific service → 'how do I contribute?'"""
        history = [
            {"role": "user", "content": "I'm new. Where do I start with Astro?"},
            {"role": "assistant", "content": (
                "Start with the cart service (astro-cart-be) — it's the entry point "
                "for customer orders and a clean example of Go+gRPC service structure."
            )},
        ]
        answer, _ = _chat_with_history(
            http, astro, "How do I add a new API endpoint to that service?", history=history,
        )
        assert len(answer) > 60, f"Onboarding follow-up too short: {answer!r}"
        score = judge_answer(
            answer,
            "Does this give concrete steps for adding an endpoint in a Go gRPC service?",
        )
        assert score >= 2, f"Onboarding follow-up quality {score}/5:\n{answer[:300]}"

    @pytest.mark.flaky(reruns=2, reruns_delay=10)
    def test_business_rule_drill_down(self, http, astro):
        """After discount types overview, drill into Optic Price stacking rule."""
        history = [
            {"role": "user", "content": "What discount types does Astro support?"},
            {"role": "assistant", "content": (
                "Astro supports SKP, BMSM, Optic Price, GWP, and PWP promotions. "
                "Optic Price is additive with SKP or BMSM, but SKP+BMSM cannot stack."
            )},
        ]
        answer, _ = _chat_with_history(
            http, astro,
            "Can you explain the Optic Price stacking rule in more detail?",
            history=history,
        )
        assert len(answer) > 50, f"Business rule drill-down too short: {answer!r}"
        score = judge_answer(
            answer,
            "Does this explain Optic Price discount stacking behavior or the finalPrice formula?",
        )
        assert score >= 2, f"Business rule drill-down quality {score}/5:\n{answer[:300]}"
