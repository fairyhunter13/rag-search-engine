"""Comprehensive real-world scenario tests for astro-project.

Verifies the search engine can handle the full range of software engineering
questions a developer on the Astro team would actually ask, using the chat
feature (codex gpt-5.4-mini / haiku 4.5 fallback) as the primary interface.

All tests require:
  - daemon running at :8765
  - astro-project indexed with communities > 0
  - GPU embeddings functional
  - Ollama running with qwen3-query:8b
  - codex CLI available (for chat tier)
"""
from __future__ import annotations

import pytest

from .conftest import judge_answer
from .test_astro_e2e import _chat

pytestmark = pytest.mark.live



# ---------------------------------------------------------------------------
# Class 1: the user's 3 explicit example questions
# ---------------------------------------------------------------------------

class TestAstroUserQuestions:
    """Validates the 3 example questions the user asked about."""

    pytestmark = pytest.mark.slow

    @pytest.mark.flaky(reruns=2, reruns_delay=10)
    def test_business_features_and_processes(self, http, astro):
        """'What are the features and business processes contained in astro project?'"""
        answer, intent, *_ = _chat(
            http, astro,
            "What are the features and business processes contained in the Astro project?",
        )
        assert len(answer) > 100, f"Answer too short: {answer!r}"
        assert intent in ("global", "business", "architecture", "feature"), (
            f"Expected broad overview intent; got {intent!r}"
        )
        score = judge_answer(
            answer,
            "Does this list concrete business domains, features, and processes "
            "(e.g., cart, promo, order, fulfillment)?",
        )
        assert score >= 2, f"Business features quality {score}/5:\n{answer[:400]}"

    @pytest.mark.flaky(reruns=2, reruns_delay=10)
    def test_function_call_tracing_in_business_features(self, http, astro):
        """'How is the function call traced and related in those business processes and features?'"""
        answer, intent, *_ = _chat(
            http, astro,
            "How is the function call traced and related in those business processes and features? "
            "Show me how they are connected.",
        )
        assert len(answer) > 80, f"Answer too short: {answer!r}"
        assert intent in ("feature", "graph_callers", "graph_callees", "global", "graph_impact"), (
            f"Expected trace/graph intent; got {intent!r}"
        )
        score = judge_answer(
            answer,
            "Does this describe function call relationships, service dependencies, "
            "or call chains across business features?",
        )
        assert score >= 2, f"Function tracing quality {score}/5:\n{answer[:400]}"

    @pytest.mark.flaky(reruns=2, reruns_delay=10)
    def test_debug_trace_bug_investigation(self, http, astro):
        """'How can we debug and trace if a bug happens and find which line of code to fix?'"""
        answer, intent, *_ = _chat(
            http, astro,
            "How can we debug and trace if a bug happens and investigate and find out "
            "which line of code we need to fix?",
        )
        assert len(answer) > 80, f"Answer too short: {answer!r}"
        assert intent in ("debug", "debug_trace", "feature", "global"), (
            f"Expected debug intent; got {intent!r}"
        )
        score = judge_answer(
            answer,
            "Does this describe how to debug or trace code in this codebase, "
            "with actionable steps or tool recommendations?",
        )
        assert score >= 2, f"Debug guidance quality {score}/5:\n{answer[:400]}"


# ---------------------------------------------------------------------------
# Class 2: architecture questions
# ---------------------------------------------------------------------------

class TestAstroArchitecture:
    """High-level architecture understanding questions."""

    pytestmark = pytest.mark.slow

    @pytest.mark.flaky(reruns=2, reruns_delay=10)
    def test_overall_system_architecture(self, http, astro):
        answer, intent, *_ = _chat(
            http, astro,
            "What is the overall system architecture of the Astro platform? "
            "Describe the main services and how they relate to each other.",
        )
        assert intent in ("global", "architecture", "feature"), (
            f"Expected global/architecture; got {intent!r}"
        )
        assert len(answer) > 150, f"Architecture answer too short: {answer!r}"
        score = judge_answer(
            answer,
            "Does this describe a multi-service architecture with concrete service names "
            "(cart, promo, OMS, fulfillment, Kong)?",
        )
        assert score >= 3, f"Architecture quality {score}/5:\n{answer[:400]}"

    @pytest.mark.flaky(reruns=2, reruns_delay=10)
    def test_grpc_service_communication(self, http, astro):
        answer, _intent, *_ = _chat(
            http, astro,
            "How do the gRPC services communicate with each other in Astro? "
            "What are the main gRPC service contracts?",
        )
        assert len(answer) > 80, f"gRPC answer too short: {answer!r}"
        score = judge_answer(
            answer,
            "Does this explain gRPC service-to-service communication with "
            "protocol buffer contracts or service names?",
        )
        assert score >= 2, f"gRPC comms quality {score}/5:\n{answer[:400]}"

    @pytest.mark.flaky(reruns=2, reruns_delay=10)
    def test_api_gateway_architecture(self, http, astro):
        answer, _intent, *_ = _chat(
            http, astro,
            "How does the Kong API gateway route traffic to the backend services? "
            "How does API versioning work in the customer gateway?",
        )
        assert len(answer) > 80, f"API gateway answer too short: {answer!r}"
        score = judge_answer(
            answer,
            "Does this describe Kong routing, API versioning (v1-v12), or Spring gateway behavior?",
        )
        assert score >= 2, f"API gateway quality {score}/5:\n{answer[:400]}"

    @pytest.mark.flaky(reruns=2, reruns_delay=10)
    def test_database_architecture(self, http, astro):
        answer, _intent, *_ = _chat(
            http, astro,
            "What databases does Astro use and how is the database-per-service pattern implemented? "
            "Which services use PostgreSQL, MongoDB, Redis, and ClickHouse?",
        )
        assert len(answer) > 80, f"Database answer too short: {answer!r}"
        score = judge_answer(
            answer,
            "Does this mention PostgreSQL, MongoDB, Redis, or ClickHouse with service ownership?",
        )
        assert score >= 2, f"Database architecture quality {score}/5:\n{answer[:400]}"


# ---------------------------------------------------------------------------
# Class 3: business features deep-dive
# ---------------------------------------------------------------------------

class TestAstroBusinessFeatures:
    """Business domain knowledge questions covering key Astro features."""

    pytestmark = pytest.mark.slow

    @pytest.mark.flaky(reruns=2, reruns_delay=10)
    def test_campaign_discount_rules(self, http, astro):
        answer, intent, *_ = _chat(
            http, astro,
            "What campaign and promotion types does Astro support? "
            "How do the discount stacking rules work between SKP, BMSM, and Optic Price promotions?",
        )
        assert len(answer) > 80, f"Campaign answer too short: {answer!r}"
        assert intent in ("feature", "global", "search", "architecture", "business"), (
            f"Unexpected intent: {intent!r}"
        )
        score = judge_answer(
            answer,
            "Does this describe campaign types (SKP, BMSM, Optic Price, GWP, PWP) "
            "or discount stacking rules?",
        )
        assert score >= 2, f"Campaign discount quality {score}/5:\n{answer[:400]}"

    @pytest.mark.flaky(reruns=2, reruns_delay=10)
    def test_order_management_process(self, http, astro):
        answer, intent, *_ = _chat(
            http, astro,
            "What is the end-to-end order management process in Astro? "
            "How does an order move from cart checkout to fulfillment and delivery?",
        )
        assert intent in ("feature", "global"), f"Expected feature/global; got {intent!r}"
        assert len(answer) > 100, f"Order management answer too short: {answer!r}"
        score = judge_answer(
            answer,
            "Does this trace an order lifecycle from cart to fulfillment "
            "with specific steps or service names?",
        )
        assert score >= 2, f"Order management quality {score}/5:\n{answer[:400]}"

    @pytest.mark.flaky(reruns=2, reruns_delay=10)
    def test_loyalty_rewards_system(self, http, astro):
        answer, _intent, *_ = _chat(
            http, astro,
            "How does the loyalty and rewards program work? "
            "How are loyalty points calculated and what tiers exist?",
        )
        assert len(answer) > 60, f"Loyalty answer too short: {answer!r}"
        score = judge_answer(
            answer,
            "Does this describe a loyalty/rewards system with points, tiers, or reward mechanisms?",
        )
        assert score >= 2, f"Loyalty system quality {score}/5:\n{answer[:400]}"

    @pytest.mark.flaky(reruns=2, reruns_delay=10)
    def test_inventory_stock_validation(self, http, astro):
        answer, _intent, *_ = _chat(
            http, astro,
            "How does inventory stock validation work to prevent overselling? "
            "What happens when multiple concurrent orders try to buy the same item?",
        )
        assert len(answer) > 60, f"Inventory answer too short: {answer!r}"
        score = judge_answer(
            answer,
            "Does this describe stock validation, inventory service, "
            "or concurrency control for orders?",
        )
        assert score >= 2, f"Inventory validation quality {score}/5:\n{answer[:400]}"

    @pytest.mark.flaky(reruns=2, reruns_delay=10)
    def test_event_driven_pubsub_flow(self, http, astro):
        answer, _intent, *_ = _chat(
            http, astro,
            "How does Google Cloud Pub/Sub enable asynchronous communication between services? "
            "What events are published when an order is placed?",
        )
        assert len(answer) > 60, f"Pub/Sub answer too short: {answer!r}"
        score = judge_answer(
            answer,
            "Does this describe Pub/Sub messaging or async event flow between services?",
        )
        assert score >= 2, f"Pub/Sub flow quality {score}/5:\n{answer[:400]}"


# ---------------------------------------------------------------------------
# Class 4: function call tracing (graph intents)
# ---------------------------------------------------------------------------

class TestAstroFunctionCallTracing:
    """Function call graph navigation — callers, impact, and feature traces."""

    def test_add_to_cart_callers(self, http, astro):
        """What calls the AddToCart gRPC method?"""
        _, intent, *_ = _chat(
            http, astro,
            "What calls the AddToCart gRPC method? Which services or components invoke it?",
        )
        assert intent in ("graph_callers", "search", "feature", "global"), (
            f"Expected graph_callers or related; got {intent!r}"
        )

    @pytest.mark.slow
    @pytest.mark.flaky(reruns=2, reruns_delay=10)
    def test_campaign_service_impact(self, http, astro):
        """What breaks if campaign service contract changes?"""
        _, intent, *_ = _chat(
            http, astro,
            "What services would break if I change the campaign gRPC service contract?",
        )
        assert intent in ("graph_impact", "feature", "global"), (
            f"Expected graph_impact; got {intent!r}"
        )

    @pytest.mark.slow
    @pytest.mark.flaky(reruns=2, reruns_delay=10)
    def test_checkout_feature_trace(self, http, astro):
        """End-to-end checkout flow trace."""
        answer, intent, *_ = _chat(
            http, astro,
            "How does the checkout and payment process work end-to-end? "
            "Trace the function calls from the customer API gateway to the order service.",
        )
        assert intent in ("feature", "global"), f"Expected feature; got {intent!r}"
        assert len(answer) > 80, f"Checkout trace too short: {answer!r}"
        score = judge_answer(
            answer,
            "Does this trace a checkout/payment flow with service calls or function names?",
        )
        assert score >= 2, f"Checkout trace quality {score}/5:\n{answer[:400]}"

    @pytest.mark.slow
    @pytest.mark.flaky(reruns=2, reruns_delay=10)
    def test_search_to_browse_trace(self, http, astro):
        """Product search and discovery call trace."""
        answer, intent, *_ = _chat(
            http, astro,
            "How does the product search and discovery feature work? "
            "Trace from user query to product results.",
        )
        assert intent in ("feature", "search", "global"), (
            f"Expected feature/search; got {intent!r}"
        )
        assert len(answer) > 60, f"Search trace too short: {answer!r}"
        score = judge_answer(
            answer,
            "Does this trace a search or browse flow, mentioning search service or browse service?",
        )
        assert score >= 2, f"Search-to-browse quality {score}/5:\n{answer[:400]}"

    @pytest.mark.slow
    @pytest.mark.flaky(reruns=2, reruns_delay=10)
    def test_fulfillment_picking_flow(self, http, astro):
        """What triggers fulfillment picking?"""
        _, intent, *_ = _chat(
            http, astro,
            "What triggers the fulfillment picking process and which services are involved?",
        )
        assert intent in ("graph_callers", "feature", "global"), (
            f"Unexpected intent: {intent!r}"
        )


# ---------------------------------------------------------------------------
# Class 5: debugging scenarios
# ---------------------------------------------------------------------------

class TestAstroDebugging:
    """Bug investigation and debugging strategy questions."""

    pytestmark = pytest.mark.slow

    @pytest.mark.flaky(reruns=2, reruns_delay=10)
    def test_goroutine_panic_debug(self, http, astro):
        """Raw Go stack trace must route to debug_trace intent and return some response."""
        answer, intent, *_ = _chat(
            http, astro,
            "goroutine 47 [running]:\n"
            "runtime/debug.Stack()\n"
            "\t/usr/local/go/src/runtime/debug/stack.go:24 +0x65\n"
            "github.com/example-org/astro-campaign-be/service.(*CampaignService).GetCampaignByID(...)\n"
            "\t/home/runner/work/astro-campaign-be/service/campaign.go:187 +0x3b2\n"
            "panic: runtime error: invalid memory address or nil pointer dereference",
        )
        assert intent in ("debug_trace", "debug"), (
            f"Expected debug_trace for stack trace; got {intent!r}"
        )
        # Raw stack trace tests intent routing, not answer quality — the KB may lack
        # the exact file context. Require a non-empty response, not a score threshold.
        assert len(answer) > 0, "No response returned for panic stack trace"

    @pytest.mark.flaky(reruns=2, reruns_delay=10)
    def test_503_error_investigation(self, http, astro):
        """Investigate 503 from Kong on cart checkout."""
        answer, intent, *_ = _chat(
            http, astro,
            "I'm getting 503 Service Unavailable errors from Kong when calling the cart checkout "
            "endpoint. How do I debug this? Which components should I investigate first?",
        )
        assert intent in ("debug", "debug_trace", "global", "feature"), (
            f"Unexpected intent: {intent!r}"
        )
        assert len(answer) > 60, f"503 debug answer too short: {answer!r}"
        score = judge_answer(
            answer,
            "Does this suggest debugging steps for 503 errors in Kong or service endpoints?",
        )
        assert score >= 2, f"503 debug quality {score}/5:\n{answer[:400]}"

    @pytest.mark.flaky(reruns=2, reruns_delay=10)
    def test_nil_pointer_cart_investigation(self, http, astro):
        """Nil pointer dereference panic in cart service."""
        answer, intent, *_ = _chat(
            http, astro,
            "I got a nil pointer dereference panic in astro-cart-be/service/cart.go. "
            "What does this file do and how should I investigate which variable is nil?",
        )
        assert intent in ("debug_trace", "debug", "search", "feature"), (
            f"Unexpected intent: {intent!r}"
        )
        assert len(answer) > 60, f"Nil pointer debug answer too short: {answer!r}"

    @pytest.mark.flaky(reruns=2, reruns_delay=10)
    def test_order_not_created_debug(self, http, astro):
        """Order not created despite cart checkout succeeding."""
        answer, intent, *_ = _chat(
            http, astro,
            "An order is not being created despite the cart checkout succeeding. "
            "How do I trace which service in the order creation flow is failing "
            "and where to add debug logging?",
        )
        assert intent in ("debug", "feature", "global"), (
            f"Unexpected intent: {intent!r}"
        )
        assert len(answer) > 60, f"Order debug answer too short: {answer!r}"
        score = judge_answer(
            answer,
            "Does this describe how to trace order creation failure across services?",
        )
        assert score >= 2, f"Order debug quality {score}/5:\n{answer[:400]}"


# ---------------------------------------------------------------------------
# Class 6: security and observability
# ---------------------------------------------------------------------------

class TestAstroSecurityAndObservability:
    """Authentication, distributed tracing, and feature flag questions."""

    pytestmark = pytest.mark.slow

    @pytest.mark.flaky(reruns=2, reruns_delay=10)
    def test_authentication_flow(self, http, astro):
        answer, _intent, *_ = _chat(
            http, astro,
            "How does authentication and authorization work in Astro? "
            "How are auth tokens propagated through gRPC inter-service calls?",
        )
        assert len(answer) > 60, f"Auth flow answer too short: {answer!r}"
        score = judge_answer(
            answer,
            "Does this describe auth token propagation, gRPC metadata, or auth service integration?",
        )
        assert score >= 2, f"Auth flow quality {score}/5:\n{answer[:400]}"

    @pytest.mark.flaky(reruns=2, reruns_delay=10)
    def test_datadog_tracing(self, http, astro):
        answer, _intent, *_ = _chat(
            http, astro,
            "How does distributed tracing with DataDog work across the gRPC services? "
            "How are trace IDs propagated between service calls?",
        )
        assert len(answer) > 60, f"DataDog answer too short: {answer!r}"
        score = judge_answer(
            answer,
            "Does this describe DataDog tracing, trace ID propagation, or observability instrumentation?",
        )
        assert score >= 2, f"DataDog tracing quality {score}/5:\n{answer[:400]}"

    @pytest.mark.flaky(reruns=2, reruns_delay=10)
    def test_feature_flags_implementation(self, http, astro):
        answer, _intent, *_ = _chat(
            http, astro,
            "How are feature flags implemented in Astro? "
            "Where are they checked and how do they control service behavior?",
        )
        assert len(answer) > 60, f"Feature flags answer too short: {answer!r}"
        score = judge_answer(
            answer,
            "Does this describe feature flag implementation, flag checks, or configuration?",
        )
        assert score >= 2, f"Feature flags quality {score}/5:\n{answer[:400]}"


# ---------------------------------------------------------------------------
# Class 7: developer onboarding and experience
# ---------------------------------------------------------------------------

class TestAstroOnboardingAndDeveloperExperience:
    """Questions a new or returning engineer would ask on day one."""

    pytestmark = pytest.mark.slow

    @pytest.mark.flaky(reruns=2, reruns_delay=10)
    def test_new_engineer_overview(self, http, astro):
        """New engineer asks where to start."""
        answer, intent, *_ = _chat(
            http, astro,
            "I'm a new software engineer on the Astro team. "
            "How should I understand the codebase architecture and where should I start?",
        )
        assert intent in ("global", "architecture", "feature"), (
            f"Expected overview intent; got {intent!r}"
        )
        assert len(answer) > 100, f"Onboarding answer too short: {answer!r}"
        score = judge_answer(
            answer,
            "Does this provide actionable onboarding guidance with service names, "
            "architecture overview, or entry points?",
        )
        assert score >= 2, f"Onboarding quality {score}/5:\n{answer[:400]}"

    @pytest.mark.flaky(reruns=2, reruns_delay=10)
    def test_go_service_structure(self, http, astro):
        """Understand typical Go microservice patterns in Astro."""
        answer, _intent, *_ = _chat(
            http, astro,
            "What is the typical structure of a Go microservice in Astro? "
            "How is dependency injection, gRPC setup, and graceful shutdown implemented?",
        )
        assert len(answer) > 60, f"Go service answer too short: {answer!r}"
        score = judge_answer(
            answer,
            "Does this describe Go service structure, initialization patterns, or gRPC server setup?",
        )
        assert score >= 2, f"Go service structure quality {score}/5:\n{answer[:400]}"

    @pytest.mark.flaky(reruns=2, reruns_delay=10)
    def test_add_new_grpc_endpoint(self, http, astro):
        """How to add a new gRPC endpoint to an existing service."""
        answer, _intent, *_ = _chat(
            http, astro,
            "How do I add a new gRPC endpoint to an existing service in Astro? "
            "What files do I need to create or modify in the proto and service layers?",
        )
        assert len(answer) > 60, f"gRPC endpoint guide too short: {answer!r}"
        score = judge_answer(
            answer,
            "Does this describe steps to add a gRPC endpoint including proto definitions, "
            "service implementation, or wiring?",
        )
        assert score >= 2, f"Add gRPC endpoint quality {score}/5:\n{answer[:400]}"

    @pytest.mark.flaky(reruns=2, reruns_delay=10)
    def test_local_development_setup(self, http, astro):
        """How to set up a local development environment."""
        answer, _intent, *_ = _chat(
            http, astro,
            "How do I set up a local development environment to run and test "
            "an Astro microservice?",
        )
        assert len(answer) > 60, f"Local dev setup answer too short: {answer!r}"
        score = judge_answer(
            answer,
            "Does this describe local development setup, build steps, or service configuration?",
        )
        assert score >= 2, f"Local dev setup quality {score}/5:\n{answer[:400]}"
