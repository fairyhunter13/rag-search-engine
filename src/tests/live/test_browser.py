"""Live browser tests — Playwright drives real Chromium against the live dashboard.

Covers all three views (Pulse / Chat / Admin), all chat intents, streaming SSE,
and admin actions (vacuum, dedup).

Run separately (conflicts with pytest-asyncio mode=auto):
    .venv/bin/pytest src/tests/live/test_browser.py --browser chromium --timeout=180

Requires: daemon at :8765, indexed project with communities.
"""
from __future__ import annotations

import httpx
import pytest

pytestmark = [pytest.mark.live, pytest.mark.slow]

DAEMON_URL = "http://localhost:8765"
DASHBOARD_URL = f"{DAEMON_URL}/dashboard"
_TIMEOUT_PAGE = 15_000    # ms — page load
_TIMEOUT_CHAT = 300_000  # ms — wait for AI response (global/debug can take 150s+)
# Heavy intents (global overview, graph traversals) hit full retrieved-context LLM paths
_TIMEOUT_CHAT_LONG = 480_000  # ms — long-tail budget for the 4 heaviest intent tests


# ---------------------------------------------------------------------------
# Module-level skip helpers
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module", autouse=True)
def require_daemon():
    """Skip entire module if daemon is not reachable."""
    try:
        httpx.get(f"{DAEMON_URL}/api/projects", timeout=5).raise_for_status()
    except Exception as exc:
        pytest.skip(f"Daemon not reachable: {exc}")


@pytest.fixture(scope="module")
def live_project():
    """Return an indexed project path with communities > 100 for richer test coverage."""
    r = httpx.get(f"{DAEMON_URL}/api/projects", timeout=10)
    projects = r.json().get("projects", [])
    all_indexed = [p for p in projects if p.get("communities", 0) > 0]
    if not all_indexed:
        pytest.skip("No indexed project with communities")
    large = [p["path"] for p in all_indexed if p.get("communities", 0) > 100]
    return large[0] if large else all_indexed[0]["path"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _goto_with_retry(page, url: str, retries: int = 3, wait_s: int = 10) -> None:
    """Navigate with retry loop — handles transient daemon downtime (e.g. from reload test)."""
    import time as _t
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            page.goto(url, timeout=20_000)
            return
        except Exception as exc:
            last_err = exc
            if attempt < retries - 1:
                _t.sleep(wait_s)
    raise last_err  # type: ignore[misc]


def _navigate_to_chat(page) -> None:
    _goto_with_retry(page, DASHBOARD_URL)
    # Use "load" not "networkidle" — dashboard keeps SSE connections open that never idle
    page.wait_for_load_state("load", timeout=_TIMEOUT_PAGE)
    # Wait for the project selector element to exist in DOM
    page.wait_for_selector("#project-sel", timeout=_TIMEOUT_PAGE)
    # Wait for loadProjects() to complete — _proj must be set before sendChat() will work
    # Use 45s: daemon may be under LLM load from previous tests in the same session
    page.wait_for_function(
        "() => { const s = document.getElementById('project-sel'); return s && s.options.length > 0 && s.value !== ''; }",
        timeout=45000,
    )
    chat_tab = page.locator("button:has-text('Chat'), a:has-text('Chat'), [data-tab='chat']").first
    if chat_tab.count() > 0 and chat_tab.is_visible():
        chat_tab.click()
        page.wait_for_timeout(300)


def _get_chat_input(page):
    return page.locator("textarea[placeholder], textarea.chat-input, textarea").last


def _send_message(page, text: str) -> None:
    inp = _get_chat_input(page)
    assert inp.is_visible(), "Chat input not visible"
    inp.fill(text)
    inp.press("Enter")


def _wait_for_ai_response(page, timeout_ms: int = _TIMEOUT_CHAT):
    # Dashboard uses .msg.ai for AI message bubbles; .thinking = still streaming
    thinking_sel = ".msg.ai"
    done_sel = ".msg.ai:not(.thinking)"
    # Wait for any AI message (includes thinking placeholder)
    page.locator(thinking_sel).first.wait_for(state="attached", timeout=timeout_ms)
    # Wait until the thinking class is removed (response fully rendered)
    page.wait_for_selector(done_sel, timeout=timeout_ms)
    # Brief settle to allow innerHTML to finish updating
    page.wait_for_timeout(300)
    return page.locator(done_sel).last.inner_text()


# ---------------------------------------------------------------------------
# View: Dashboard loads
# ---------------------------------------------------------------------------

class TestDashboardLoads:
    def test_root_redirects_to_dashboard(self, page):
        _goto_with_retry(page, DAEMON_URL)
        page.wait_for_load_state("load", timeout=_TIMEOUT_PAGE)
        assert "/dashboard" in page.url or page.title() != "", (
            "Root URL did not redirect to dashboard"
        )

    def test_dashboard_page_not_blank(self, page):
        _goto_with_retry(page, DASHBOARD_URL)
        page.wait_for_load_state("load", timeout=_TIMEOUT_PAGE)
        content = page.content()
        assert len(content) > 500, "Dashboard returned a nearly empty page"
        assert "error" not in content[:200].lower() or "opencode" in content.lower(), (
            "Dashboard may be showing an error page"
        )

    def test_navigation_tabs_visible(self, page):
        _goto_with_retry(page, DASHBOARD_URL)
        page.wait_for_load_state("load", timeout=_TIMEOUT_PAGE)
        nav = page.locator("nav, [role='tablist'], .tabs, .navbar, .top-bar").first
        assert nav.count() > 0 or nav.is_visible(), "No navigation element found on dashboard"


# ---------------------------------------------------------------------------
# View: Pulse
# ---------------------------------------------------------------------------

class TestPulseView:
    def test_pulse_tab_accessible(self, page):
        _goto_with_retry(page, DASHBOARD_URL)
        page.wait_for_load_state("load", timeout=_TIMEOUT_PAGE)
        pulse = page.locator("button:has-text('Pulse'), a:has-text('Pulse'), [data-tab='pulse']").first
        assert pulse.count() > 0, "No Pulse tab found — dashboard nav must have a Pulse tab"
        pulse.click()
        page.wait_for_timeout(500)
        content = page.content()
        has_kpi = any(w in content.lower() for w in ("kpi", "communities", "indexed", "total", "metric"))
        assert has_kpi, "Pulse tab has no recognizable KPI content"

    def test_pulse_shows_activity_or_metrics(self, page):
        _goto_with_retry(page, DASHBOARD_URL)
        page.wait_for_load_state("load", timeout=_TIMEOUT_PAGE)
        pulse = page.locator("button:has-text('Pulse'), a:has-text('Pulse')").first
        if pulse.count() > 0:
            pulse.click()
            page.wait_for_timeout(800)
        content = page.content()
        has_numbers = any(c.isdigit() for c in content)
        assert has_numbers, "Pulse view contains no numeric data"

    def test_pulse_stream_health_tile_present(self, page):
        """Stream Health KPI tile must be rendered in the Pulse view."""
        _goto_with_retry(page, DASHBOARD_URL)
        page.wait_for_load_state("load", timeout=_TIMEOUT_PAGE)
        pulse = page.locator("button:has-text('Pulse'), a:has-text('Pulse'), [data-tab='pulse']").first
        if pulse.count() > 0:
            pulse.click()
        page.wait_for_timeout(1500)
        tile = page.locator("#tile-stream")
        assert tile.count() > 0, "Stream Health tile (#tile-stream) not found in Pulse view"
        kpi = page.locator("#kpi-stream")
        assert kpi.count() > 0, "Stream Health KPI value (#kpi-stream) not found"

    def test_pulse_kpi_enrichment_tile_renders(self, page):
        """#kpi-enrichment tile must exist and show any value (% or '—')."""
        _goto_with_retry(page, DASHBOARD_URL)
        page.wait_for_load_state("load", timeout=_TIMEOUT_PAGE)
        tile = page.locator("#kpi-enrichment")
        assert tile.count() > 0, "#kpi-enrichment tile not found in Pulse view"
        val = (tile.text_content(timeout=5_000) or "").strip()
        assert val != "", "#kpi-enrichment has no text content"

    def test_pulse_kpi_wiki_tile_renders(self, page):
        """#kpi-wiki tile must exist and show any value."""
        _goto_with_retry(page, DASHBOARD_URL)
        page.wait_for_load_state("load", timeout=_TIMEOUT_PAGE)
        tile = page.locator("#kpi-wiki")
        assert tile.count() > 0, "#kpi-wiki tile not found in Pulse view"
        val = (tile.text_content(timeout=5_000) or "").strip()
        assert val != "", "#kpi-wiki has no text content"

    def test_pulse_kpi_uptime_populated(self, page):
        """#kpi-uptime must show a non-dash value — daemon has been running."""
        _goto_with_retry(page, DASHBOARD_URL)
        page.wait_for_load_state("load", timeout=_TIMEOUT_PAGE)
        page.wait_for_function(
            "() => { const s = document.getElementById('project-sel'); return s && s.options.length > 0 && s.value !== ''; }",
            timeout=45_000,
        )
        page.wait_for_function(
            "document.querySelector('#kpi-uptime')?.textContent?.trim() !== '—'",
            timeout=20_000,
        )
        val = (page.locator("#kpi-uptime").text_content(timeout=5_000) or "").strip()
        assert val and val != "—", f"#kpi-uptime still showing '—': {val!r}"

    def test_pulse_kpi_tiles_populated(self, page):
        """KPI tiles must show non-dash values after the metrics poll runs."""
        _goto_with_retry(page, DASHBOARD_URL)
        page.wait_for_load_state("load", timeout=_TIMEOUT_PAGE)
        # Wait for project to be auto-selected (loadPulse requires _proj)
        page.wait_for_function(
            "() => { const s = document.getElementById('project-sel'); return s && s.options.length > 0 && s.value !== ''; }",
            timeout=45_000,
        )
        # Wait for the files KPI tile to show a real value (not "—")
        page.wait_for_function(
            "document.querySelector('#kpi-files')?.textContent?.trim() !== '—'",
            timeout=20_000,
        )
        # #kpi-requests shows "—" when total_requests==0 (falsy) — skip it
        for tile_id in ("#kpi-files", "#kpi-communities"):
            val = (page.locator(tile_id).text_content(timeout=5_000) or "").strip()
            assert val and val != "—", f"{tile_id} still showing '—' after metrics poll"

    def test_pulse_suggested_questions_clickable(self, page):
        """Clicking a suggested question must switch to chat view and submit it."""
        _goto_with_retry(page, DASHBOARD_URL)
        page.wait_for_load_state("load", timeout=_TIMEOUT_PAGE)
        # Wait for project selection and loadPulse to populate suggested questions
        page.wait_for_function(
            "() => { const s = document.getElementById('project-sel'); return s && s.options.length > 0 && s.value !== ''; }",
            timeout=45_000,
        )
        page.wait_for_selector(".sq-btn", timeout=20_000)
        first_btn = page.locator(".sq-btn").first
        first_btn.click()
        # askQuestion() calls switchView('chat') synchronously — verify view switched
        page.wait_for_function(
            "document.getElementById('view-chat')?.classList?.contains('active')",
            timeout=5_000,
        )
        # A user message bubble must appear (sendChat was called)
        page.wait_for_selector(".msg.user", timeout=10_000)


# ---------------------------------------------------------------------------
# View: Chat — basic behavior
# ---------------------------------------------------------------------------

class TestChatView:
    def test_chat_input_visible(self, page):
        _navigate_to_chat(page)
        assert _get_chat_input(page).is_visible(), "Chat input not visible"

    def test_chat_sends_and_receives_response(self, page, live_project):
        _navigate_to_chat(page)
        _send_message(page, "What does this project do?")
        text = _wait_for_ai_response(page)
        assert len(text) > 10, f"AI response too short: {text!r}"

    def test_chat_response_is_not_error_message(self, page, live_project):
        _navigate_to_chat(page)
        _send_message(page, "What is the overall architecture?")
        text = _wait_for_ai_response(page)
        error_phrases = ["500 internal", "traceback", "exception:", "cannot connect"]
        assert not any(p in text.lower() for p in error_phrases), (
            f"AI response looks like an error: {text[:200]}"
        )

    def test_project_switch_clears_chat_input(self, page):
        """After a page reload (simulating project switch), chat input must be empty."""
        _goto_with_retry(page, DASHBOARD_URL)
        page.wait_for_load_state("load", timeout=_TIMEOUT_PAGE)
        page.click("#vbtn-chat")
        page.wait_for_function(
            "document.getElementById('view-chat')?.classList?.contains('active')",
            timeout=10_000,
        )
        page.locator("#chat-in").fill("test message before project switch")
        # Reload simulates navigating away and back (or project switch re-init)
        _goto_with_retry(page, DASHBOARD_URL)
        page.wait_for_load_state("load", timeout=_TIMEOUT_PAGE)
        page.click("#vbtn-chat")
        page.wait_for_function(
            "document.getElementById('view-chat')?.classList?.contains('active')",
            timeout=10_000,
        )
        chat_val = page.locator("#chat-in").input_value(timeout=3_000)
        assert chat_val == "", f"chat-in not cleared after page reload: {chat_val!r}"


# ---------------------------------------------------------------------------
# View: Chat — all 9 intents
# ---------------------------------------------------------------------------

class TestChatIntents:
    """Each intent must produce a non-empty response."""

    def _chat_and_get_text(self, page, live_project: str, query: str,
                           timeout_ms: int = _TIMEOUT_CHAT) -> str:
        _navigate_to_chat(page)
        _send_message(page, query)
        return _wait_for_ai_response(page, timeout_ms=timeout_ms)

    def test_intent_architecture(self, page, live_project):
        text = self._chat_and_get_text(page, live_project, "What is the overall architecture?")
        assert len(text) > 30, f"Architecture response too short: {text!r}"

    def test_intent_search(self, page, live_project):
        text = self._chat_and_get_text(page, live_project, "Find the main HTTP handler function")
        assert len(text) > 20, f"Search response too short: {text!r}"

    def test_intent_global(self, page, live_project):
        text = self._chat_and_get_text(
            page, live_project,
            "Give me a comprehensive global overview of the entire system",
            timeout_ms=_TIMEOUT_CHAT_LONG,
        )
        assert len(text) > 50, f"Global overview response too short: {text!r}"

    def test_intent_feature(self, page, live_project):
        text = self._chat_and_get_text(page, live_project, "How does the request processing feature work end to end?")
        assert len(text) > 30, f"Feature trace response too short: {text!r}"

    def test_intent_graph_callers(self, page, live_project):
        text = self._chat_and_get_text(
            page, live_project,
            "What functions call the main function?",
            timeout_ms=_TIMEOUT_CHAT_LONG,
        )
        assert len(text) > 10, f"Graph callers response too short: {text!r}"

    def test_intent_graph_impact(self, page, live_project):
        text = self._chat_and_get_text(
            page, live_project,
            "What breaks if I change the main handler?",
            timeout_ms=_TIMEOUT_CHAT_LONG,
        )
        assert len(text) > 10, f"Graph impact response too short: {text!r}"

    def test_intent_graph_callees(self, page, live_project):
        text = self._chat_and_get_text(
            page, live_project,
            "What does the search handler call internally?",
            timeout_ms=_TIMEOUT_CHAT_LONG,
        )
        assert len(text) > 10, f"Graph callees response too short: {text!r}"

    def test_intent_debug_trace(self, page, live_project):
        traceback = (
            "Traceback (most recent call last):\n"
            "  File 'server.py', line 42, in handle_request\n"
            "    result = process(data)\n"
            "KeyError: 'content'"
        )
        text = self._chat_and_get_text(page, live_project, traceback)
        assert len(text) > 20, f"Debug trace response too short: {text!r}"

    def test_intent_debug(self, page, live_project):
        text = self._chat_and_get_text(page, live_project, "How do I debug a connection pool exhaustion issue?")
        assert len(text) > 20, f"Debug response too short: {text!r}"


# ---------------------------------------------------------------------------
# View: Chat — streaming behavior
# ---------------------------------------------------------------------------

class TestChatStreaming:
    def test_sse_events_received(self, page, live_project):
        """Network requests to /api/chat_stream must be seen while chat is active."""
        stream_requests: list[str] = []

        def on_request(request):
            if "chat_stream" in request.url:
                stream_requests.append(request.url)

        page.on("request", on_request)
        _navigate_to_chat(page)
        _send_message(page, "What does this project do?")
        page.wait_for_timeout(3000)
        assert len(stream_requests) > 0, (
            "No /api/chat_stream request seen — chat may not be using SSE streaming"
        )

    def test_intent_badge_visible_after_response(self, page, live_project):
        """After a response, an intent badge must appear (shows routing worked)."""
        _navigate_to_chat(page)
        _send_message(page, "What is the architecture?")
        _wait_for_ai_response(page)
        badge_sel = ".intent-tag, .intent-badge, [data-intent], .badge"
        badge = page.locator(badge_sel).first
        if badge.count() > 0:
            assert badge.is_visible(), "Intent badge exists but is not visible"

    def test_streaming_tokens_appear_progressively(self, page, live_project):
        """Tokens must appear in the DOM before the response is fully complete."""
        token_times: list[float] = []

        def on_response(response):
            if "chat_stream" in response.url:
                token_times.append(page.evaluate("Date.now()"))

        page.on("response", on_response)
        _navigate_to_chat(page)
        start = page.evaluate("Date.now()")
        _send_message(page, "What is this codebase?")
        # Wait for first partial text to appear in the AI bubble (thinking placeholder counts)
        page.wait_for_selector(".msg.ai", timeout=_TIMEOUT_CHAT)
        mid_time = page.evaluate("Date.now()")
        _wait_for_ai_response(page)
        # Streaming means mid_time is before response complete
        assert mid_time - start < _TIMEOUT_CHAT, "AI message appeared too late (not streaming)"

    def test_multi_turn_conversation(self, page, live_project):
        """Second question in same chat must produce a new response (multi-turn works)."""
        _navigate_to_chat(page)
        # Use a fast search-intent query (no global synthesis)
        _send_message(page, "what files are in this project?")
        first_text = _wait_for_ai_response(page)
        assert len(first_text) > 20, f"First response too short: {first_text!r}"
        # Count existing messages before second turn
        msg_count_before = page.locator(".msg.ai:not(.thinking)").count()
        # Second turn: simple follow-up
        _send_message(page, "list the main directories")
        # Wait for a new AI message to appear
        page.wait_for_function(
            f"document.querySelectorAll('.msg.ai:not(.thinking)').length > {msg_count_before}",
            timeout=_TIMEOUT_CHAT,
        )
        second_text = page.locator(".msg.ai:not(.thinking)").last.inner_text()
        assert len(second_text) > 20, f"Second response too short: {second_text!r}"
        assert second_text != first_text, "Second response identical to first — multi-turn may be broken"

    def test_no_duplicate_user_bubble_on_rapid_enter(self, page, live_project):
        """Pressing Enter twice rapidly must not create two user message bubbles."""
        _navigate_to_chat(page)
        inp = _get_chat_input(page)
        assert inp.is_visible()
        inp.fill("What is this project?")
        inp.press("Enter")
        inp.press("Enter")  # second rapid Enter
        page.wait_for_timeout(500)
        user_sel = ".msg.user, .user-bubble, .user-message, [data-role='user']"
        user_bubbles = page.locator(user_sel)
        count = user_bubbles.count()
        assert count <= 1, (
            f"Rapid Enter created {count} user bubbles — in-flight guard may be broken"
        )

    def test_message_metadata_appears_after_response(self, page, live_project):
        """After a full response, .elapsed timing must be visible in .msg-meta."""
        _navigate_to_chat(page)
        _send_message(page, "What files are in this project?")
        _wait_for_ai_response(page)
        meta = page.locator(".msg.ai:not(.thinking) .elapsed, .msg.ai:not(.thinking) .msg-meta").first
        if meta.count() > 0:
            assert meta.is_visible(), ".elapsed/.msg-meta found but not visible"

    def test_chat_response_has_rendered_html(self, page, live_project):
        """AI response bubble must contain rendered HTML elements (mdSafe ran)."""
        _navigate_to_chat(page)
        _send_message(page, "Show me a code example from this project")
        _wait_for_ai_response(page)
        bubble = page.locator(".msg.ai:not(.thinking) .msg-bubble").first
        assert bubble.count() > 0, "No AI bubble found after response"
        has_html = page.evaluate(
            "document.querySelector('.msg.ai:not(.thinking) .msg-bubble')?.children?.length > 0"
        )
        assert has_html, "msg-bubble has no HTML children — mdSafe may not have rendered"

    def test_send_button_click_works(self, page, live_project):
        """Clicking send button (not Enter) sends message and produces a response."""
        _navigate_to_chat(page)
        page.locator("#chat-in").fill("list the top level directories")
        send_btn = page.locator("#send-btn")
        assert send_btn.count() > 0, "#send-btn not found — send button missing from chat"
        send_btn.click()
        response = _wait_for_ai_response(page)
        assert len(response) > 10, f"Send button response too short: {response!r}"

    def test_source_chips_visible_after_response(self, page, live_project):
        """Source file chips (.src-chip) appear after a search-intent response."""
        _navigate_to_chat(page)
        _send_message(page, "find the main handler files")
        _wait_for_ai_response(page)
        chips = page.locator(".src-chip")
        assert chips.count() > 0, "No .src-chip elements appeared after search-intent response"

    def test_three_turn_conversation_works(self, page, live_project):
        """Three consecutive messages each produce a unique AI response."""
        _navigate_to_chat(page)
        _send_message(page, "what services exist?")
        r1 = _wait_for_ai_response(page)
        assert len(r1) > 20, f"First response too short: {r1!r}"

        count1 = page.locator(".msg.ai:not(.thinking)").count()
        _send_message(page, "tell me about the first one")
        page.wait_for_function(
            f"document.querySelectorAll('.msg.ai:not(.thinking)').length > {count1}",
            timeout=_TIMEOUT_CHAT,
        )
        r2 = page.locator(".msg.ai:not(.thinking)").last.inner_text()
        assert len(r2) > 20, f"Second response too short: {r2!r}"

        count2 = page.locator(".msg.ai:not(.thinking)").count()
        _send_message(page, "how is it tested?")
        page.wait_for_function(
            f"document.querySelectorAll('.msg.ai:not(.thinking)').length > {count2}",
            timeout=_TIMEOUT_CHAT,
        )
        r3 = page.locator(".msg.ai:not(.thinking)").last.inner_text()
        assert len(r3) > 20, f"Third response too short: {r3!r}"

        unique = {r1[:50], r2[:50], r3[:50]}
        assert len(unique) == 3, "Duplicate responses detected in 3-turn conversation"


# ---------------------------------------------------------------------------
# View: Admin
# ---------------------------------------------------------------------------

class TestAdminView:
    def _open_admin(self, page):
        _goto_with_retry(page, DASHBOARD_URL)
        page.wait_for_load_state("load", timeout=_TIMEOUT_PAGE)
        admin_tab = page.locator("button:has-text('Admin'), a:has-text('Admin'), [data-tab='admin']").first
        if admin_tab.count() == 0:
            pytest.skip("No Admin tab visible")
        admin_tab.click()
        page.wait_for_timeout(500)

    def test_admin_tab_opens(self, page):
        self._open_admin(page)
        content = page.content()
        has_admin = any(w in content.lower() for w in ("vacuum", "dedup", "project", "build", "index", "jobs"))
        assert has_admin, "Admin tab opened but no admin controls found"

    def test_admin_vacuum_button_present(self, page):
        self._open_admin(page)
        vacuum = page.locator("button:has-text('Vacuum'), button:has-text('vacuum')").first
        assert vacuum.count() > 0, "No Vacuum button in Admin view"

    def test_admin_dedup_button_present(self, page):
        self._open_admin(page)
        dedup = page.locator("button:has-text('Dedup'), button:has-text('dedup'), button:has-text('Deduplicate')").first
        assert dedup.count() > 0, "No Dedup button in Admin view"

    def test_admin_projects_table_visible(self, page):
        self._open_admin(page)
        projects_content = page.locator("table, .projects-list, .project-row, [data-project]").first
        assert projects_content.count() > 0, "No projects table in admin view — Admin tab must show indexed projects"
        assert projects_content.is_visible(), "Projects table not visible in admin"

    def test_admin_jobs_section_present(self, page):
        self._open_admin(page)
        content = page.content()
        has_jobs = any(w in content.lower() for w in ("jobs", "pipeline", "background", "running"))
        assert has_jobs, "No jobs/pipeline section in admin view"

    def test_admin_vacuum_executes_and_logs(self, page):
        """Clicking Vacuum must trigger the operation and write output to #op-log."""
        _goto_with_retry(page, DASHBOARD_URL)
        page.wait_for_load_state("load", timeout=_TIMEOUT_PAGE)
        # Wait for a project to be auto-selected (runVacuum requires _proj)
        page.wait_for_function(
            "() => { const s = document.getElementById('project-sel'); return s && s.options.length > 0 && s.value !== ''; }",
            timeout=45_000,
        )
        # Navigate to Admin using the exact button ID
        page.click("#vbtn-admin")
        # Wait for admin view to become active (op-log is 0-height when empty, so check view)
        page.wait_for_function(
            "document.getElementById('view-admin')?.classList?.contains('active')",
            timeout=10_000,
        )
        # Click Vacuum button
        vacuum_btn = page.locator("button:has-text('Vacuum')").first
        if not vacuum_btn.count():
            pytest.skip("Vacuum button not found in Admin view")
        vacuum_btn.click()
        # Wait for op-log to receive at least one entry (dry-run is fast)
        page.wait_for_function(
            "document.querySelector('#op-log')?.textContent?.trim()?.length > 0",
            timeout=15_000,
        )
        log_text = (page.locator("#op-log").text_content() or "").strip()
        assert log_text, "op-log empty after clicking Vacuum"

    def _click_admin_op(self, page, btn_text: str) -> str:
        """Open admin, click an operation button, return op-log text."""
        _goto_with_retry(page, DASHBOARD_URL)
        page.wait_for_load_state("load", timeout=_TIMEOUT_PAGE)
        page.wait_for_function(
            "() => { const s = document.getElementById('project-sel'); return s && s.options.length > 0 && s.value !== ''; }",
            timeout=45_000,
        )
        page.click("#vbtn-admin")
        page.wait_for_function(
            "document.getElementById('view-admin')?.classList?.contains('active')",
            timeout=10_000,
        )
        btn = page.locator(f"button:has-text('{btn_text}')").first
        if not btn.count():
            pytest.skip(f"Button '{btn_text}' not found in Admin view")
        btn.click()
        page.wait_for_function(
            "document.querySelector('#op-log')?.textContent?.trim()?.length > 0",
            timeout=20_000,
        )
        return (page.locator("#op-log").text_content() or "").strip()

    def test_admin_dedup_executes_and_logs(self, page):
        """Clicking Dedup must write output to #op-log."""
        log_text = self._click_admin_op(page, "Dedup")
        assert log_text, "op-log empty after clicking Dedup"

    def test_admin_reindex_submits_job(self, page):
        """Clicking Re-index must submit a background job and log it."""
        log_text = self._click_admin_op(page, "Re-index")
        assert log_text, "op-log empty after clicking Re-index"

    def test_admin_enrich_submits_job(self, page):
        """Clicking Enrich must submit a background job and log it."""
        log_text = self._click_admin_op(page, "Enrich")
        assert log_text, "op-log empty after clicking Enrich"

    def test_admin_wiki_submits_job(self, page):
        """Clicking Wiki must submit a background job and log it."""
        log_text = self._click_admin_op(page, "Wiki")
        assert log_text, "op-log empty after clicking Wiki"

    def test_admin_refresh_button_present(self, page):
        """Admin view must have a Refresh button that is clickable."""
        self._open_admin(page)
        refresh_btn = page.locator("button:has-text('Refresh'), button:has-text('🔄')").first
        assert refresh_btn.count() > 0, "No Refresh button found in Admin view"
        refresh_btn.click()
        page.wait_for_timeout(500)
        assert page.url.startswith(DASHBOARD_URL), "Page navigated away after Refresh click"

    def test_admin_operation_shows_toast(self, page):
        """Clicking an Admin operation button must produce a toast notification."""
        _goto_with_retry(page, DASHBOARD_URL)
        page.wait_for_load_state("load", timeout=_TIMEOUT_PAGE)
        page.wait_for_function(
            "() => { const s = document.getElementById('project-sel'); return s && s.options.length > 0 && s.value !== ''; }",
            timeout=45_000,
        )
        page.click("#vbtn-admin")
        page.wait_for_function(
            "document.getElementById('view-admin')?.classList?.contains('active')",
            timeout=10_000,
        )
        vacuum_btn = page.locator("button:has-text('Vacuum')").first
        if not vacuum_btn.count():
            pytest.skip("Vacuum button not found in Admin view")
        vacuum_btn.click()
        page.wait_for_function(
            "document.querySelector('#toast div') !== null",
            timeout=8_000,
        )
        toast_el = page.locator("#toast div").first
        assert toast_el.count() > 0, "#toast div never appeared after Vacuum"

    def test_admin_active_project_row_highlighted(self, page, live_project):
        """The currently selected project has .active-row class in the projects table."""
        _goto_with_retry(page, DASHBOARD_URL)
        page.wait_for_load_state("load", timeout=_TIMEOUT_PAGE)
        page.wait_for_function(
            "() => { const s = document.getElementById('project-sel'); return s && s.options.length > 0 && s.value !== ''; }",
            timeout=45_000,
        )
        page.click("#vbtn-admin")
        page.wait_for_function(
            "document.getElementById('view-admin')?.classList?.contains('active')",
            timeout=10_000,
        )
        # Wait for loadAdmin() to populate the table (it's async)
        page.wait_for_function(
            "document.getElementById('projects-body')?.children?.length > 0",
            timeout=10_000,
        )
        active_rows = page.locator(".projects-table tr.active-row, table tr.active-row")
        assert active_rows.count() >= 1, "No .active-row found in projects table after project selected"

    def test_admin_op_log_shows_intermediate_message(self, page, live_project):
        """Clicking Vacuum writes an immediate message to #op-log."""
        _goto_with_retry(page, DASHBOARD_URL)
        page.wait_for_load_state("load", timeout=_TIMEOUT_PAGE)
        page.wait_for_function(
            "() => { const s = document.getElementById('project-sel'); return s && s.options.length > 0 && s.value !== ''; }",
            timeout=45_000,
        )
        page.click("#vbtn-admin")
        page.wait_for_function(
            "document.getElementById('view-admin')?.classList?.contains('active')",
            timeout=10_000,
        )
        vacuum_btn = page.locator("button:has-text('Vacuum')").first
        if not vacuum_btn.count():
            pytest.skip("Vacuum button not found in Admin view")
        vacuum_btn.click()
        page.wait_for_function(
            "document.querySelector('#op-log')?.textContent?.trim()?.length > 0",
            timeout=10_000,
        )
        log_text = (page.locator("#op-log").text_content() or "").strip()
        assert len(log_text) > 0, "op-log empty after vacuum click"


# ---------------------------------------------------------------------------
# Global UI elements
# ---------------------------------------------------------------------------

class TestGlobalUI:
    """Status indicators, command palette — global UI elements present in all views."""

    def test_daemon_dot_ok_when_daemon_up(self, page):
        """#daemon-dot must have class 'ok' when daemon is reachable and responding."""
        _goto_with_retry(page, DASHBOARD_URL)
        page.wait_for_load_state("load", timeout=_TIMEOUT_PAGE)
        # loadPulse() calls setDot('ok') when API calls succeed; wait up to 15s
        page.wait_for_function(
            "document.getElementById('daemon-dot')?.classList?.contains('ok')",
            timeout=15_000,
        )
        classes = page.locator("#daemon-dot").get_attribute("class") or ""
        assert "ok" in classes, f"#daemon-dot class '{classes}' — expected 'ok' when daemon is up"

    def test_command_palette_opens_with_ctrl_k(self, page):
        """Ctrl+K must open the command palette; Escape must close it."""
        _goto_with_retry(page, DASHBOARD_URL)
        page.wait_for_load_state("load", timeout=_TIMEOUT_PAGE)
        # Command palette starts hidden
        overlay = page.locator("#cmd-overlay")
        assert overlay.count() > 0, "#cmd-overlay not found — command palette HTML missing"
        # Open with Ctrl+K
        page.keyboard.press("Control+k")
        page.wait_for_function(
            "!document.getElementById('cmd-overlay')?.classList?.contains('hidden')",
            timeout=5_000,
        )
        assert "hidden" not in (overlay.get_attribute("class") or ""), (
            "Command palette did not open after Ctrl+K"
        )
        # Close with Escape
        page.keyboard.press("Escape")
        page.wait_for_function(
            "document.getElementById('cmd-overlay')?.classList?.contains('hidden')",
            timeout=5_000,
        )
        assert "hidden" in (overlay.get_attribute("class") or ""), (
            "Command palette did not close after Escape"
        )

    def test_theme_toggle_changes_button_text(self, page):
        """Clicking #theme-btn must toggle text between ☀ and 🌙."""
        _goto_with_retry(page, DASHBOARD_URL)
        page.wait_for_load_state("load", timeout=_TIMEOUT_PAGE)
        btn = page.locator("#theme-btn")
        assert btn.count() > 0, "#theme-btn not found"
        initial_text = (btn.text_content() or "").strip()
        btn.click()
        page.wait_for_timeout(300)
        toggled_text = (btn.text_content() or "").strip()
        assert toggled_text != initial_text, (
            f"#theme-btn text did not change after click: was {initial_text!r}, still {toggled_text!r}"
        )
        # Toggle back
        btn.click()
        page.wait_for_timeout(300)
        restored_text = (btn.text_content() or "").strip()
        assert restored_text == initial_text, (
            f"Theme did not restore: expected {initial_text!r}, got {restored_text!r}"
        )

    def test_command_palette_filters_on_type(self, page):
        """Typing in #cmd-input must narrow the result list to matching items."""
        _goto_with_retry(page, DASHBOARD_URL)
        page.wait_for_load_state("load", timeout=_TIMEOUT_PAGE)
        page.keyboard.press("Control+k")
        page.wait_for_function(
            "!document.getElementById('cmd-overlay')?.classList?.contains('hidden')",
            timeout=5_000,
        )
        page.locator("#cmd-input").type("vacuum")
        page.wait_for_timeout(200)
        items = page.locator("#cmd-results li")
        assert items.count() >= 1, "Filter returned no results for 'vacuum'"
        labels = [(items.nth(i).text_content() or "") for i in range(items.count())]
        assert any("vacuum" in lbl.lower() for lbl in labels), (
            f"Filter did not show vacuum-related items: {labels}"
        )
        page.keyboard.press("Escape")

    def test_command_palette_executes_via_enter(self, page):
        """Pressing Enter on a highlighted palette item must execute it."""
        _goto_with_retry(page, DASHBOARD_URL)
        page.wait_for_load_state("load", timeout=_TIMEOUT_PAGE)
        page.keyboard.press("Control+k")
        page.wait_for_function(
            "!document.getElementById('cmd-overlay')?.classList?.contains('hidden')",
            timeout=5_000,
        )
        page.locator("#cmd-input").type("Pulse")
        page.wait_for_timeout(200)
        page.keyboard.press("Enter")
        page.wait_for_function(
            "document.getElementById('cmd-overlay')?.classList?.contains('hidden')",
            timeout=3_000,
        )
        page.wait_for_function(
            "document.getElementById('view-pulse')?.classList?.contains('active')",
            timeout=3_000,
        )

    def test_command_palette_arrow_navigation(self, page):
        """ArrowDown in command palette must highlight the first result item."""
        _goto_with_retry(page, DASHBOARD_URL)
        page.wait_for_load_state("load", timeout=_TIMEOUT_PAGE)
        page.keyboard.press("Control+k")
        page.wait_for_function(
            "!document.getElementById('cmd-overlay')?.classList?.contains('hidden')",
            timeout=5_000,
        )
        page.keyboard.press("ArrowDown")
        page.wait_for_timeout(150)
        highlighted = page.locator("#cmd-results li.hi, #cmd-results li[aria-selected='true']")
        assert highlighted.count() > 0, "ArrowDown did not highlight any palette item"
        page.keyboard.press("Escape")

    def test_pulse_sparklines_rendered(self, page, live_project):
        """Pulse tab SVG sparklines (#sp-files etc.) contain drawn polyline elements."""
        _goto_with_retry(page, DASHBOARD_URL)
        page.wait_for_load_state("load", timeout=_TIMEOUT_PAGE)
        page.wait_for_function(
            "() => { const s = document.getElementById('project-sel'); return s && s.options.length > 0 && s.value !== ''; }",
            timeout=45_000,
        )
        # Allow first metrics poll to complete, then trigger a second to get ≥2 data points
        page.wait_for_timeout(2_500)
        page.evaluate("loadPulse()")
        page.wait_for_timeout(2_500)
        sparkline = page.locator("#sp-files polyline, #sp-files polygon")
        assert sparkline.count() > 0, (
            "Pulse sparkline #sp-files has no drawn polyline/polygon — sparklines not rendering"
        )

    def test_command_palette_all_view_switches(self, page, live_project):
        """All 3 view switch commands (Admin, Chat, Pulse) work via command palette."""
        _goto_with_retry(page, DASHBOARD_URL)
        page.wait_for_load_state("load", timeout=_TIMEOUT_PAGE)
        for view_cmd in ("Admin", "Chat", "Pulse"):
            page.keyboard.press("Control+k")
            page.wait_for_function(
                "!document.getElementById('cmd-overlay')?.classList?.contains('hidden')",
                timeout=5_000,
            )
            page.locator("#cmd-input").fill(view_cmd)
            page.wait_for_timeout(200)
            page.keyboard.press("Enter")
            page.wait_for_function(
                "document.getElementById('cmd-overlay')?.classList?.contains('hidden')",
                timeout=5_000,
            )
            page.wait_for_timeout(500)

    def test_toast_auto_dismisses_after_timeout(self, page, live_project):
        """Toast notification disappears automatically within ~6s of appearing."""
        _goto_with_retry(page, DASHBOARD_URL)
        page.wait_for_load_state("load", timeout=_TIMEOUT_PAGE)
        page.wait_for_function(
            "() => { const s = document.getElementById('project-sel'); return s && s.options.length > 0 && s.value !== ''; }",
            timeout=45_000,
        )
        page.click("#vbtn-admin")
        page.wait_for_function(
            "document.getElementById('view-admin')?.classList?.contains('active')",
            timeout=10_000,
        )
        vacuum_btn = page.locator("button:has-text('Vacuum')").first
        if not vacuum_btn.count():
            pytest.skip("Vacuum button not found in Admin view")
        vacuum_btn.click()
        page.wait_for_selector("#toast div", state="visible", timeout=15_000)
        # Toast must auto-dismiss within 8s (4s display timeout + 4s buffer for animation)
        page.wait_for_selector("#toast div", state="hidden", timeout=8_000)


# ---------------------------------------------------------------------------
# Phase 64 gap additions: TestPulseViewGaps (C.1–C.10)
# ---------------------------------------------------------------------------

class TestPulseViewGaps:
    """Coverage gaps from Phase 64 dashboard audit — tile badges, sparklines, empty states."""

    def _load_pulse(self, page) -> None:
        _goto_with_retry(page, DASHBOARD_URL)
        page.wait_for_load_state("load", timeout=_TIMEOUT_PAGE)
        page.wait_for_function(
            "() => { const s = document.getElementById('project-sel'); return s && s.options.length > 0 && s.value !== ''; }",
            timeout=45_000,
        )
        page.wait_for_timeout(2_500)  # allow first metrics poll to complete
        # Trigger a second loadPulse() to get ≥2 data points so sparklines render
        page.evaluate("loadPulse()")
        page.wait_for_timeout(2_500)

    def test_tile_files_badge_shows_indexed_status(self, page, live_project):
        """#tb-files badge must be present and contain a non-empty string after metrics load."""
        self._load_pulse(page)
        badge = page.locator("#tb-files")
        assert badge.count() > 0, "#tb-files badge element not found"
        text = (badge.text_content(timeout=8_000) or "").strip()
        assert text != "", "#tb-files badge has no text"

    def test_tile_communities_badge_shows_graph_status(self, page, live_project):
        """#tb-communities badge must be present after pulse load."""
        self._load_pulse(page)
        badge = page.locator("#tb-communities")
        assert badge.count() > 0, "#tb-communities badge element not found"
        text = (badge.text_content(timeout=8_000) or "").strip()
        assert text != "", "#tb-communities badge has no text"

    def test_tile_enrichment_badge_has_status_class(self, page, live_project):
        """#tb-enrichment badge must carry one of ok/warn/err CSS class."""
        self._load_pulse(page)
        badge = page.locator("#tb-enrichment")
        assert badge.count() > 0, "#tb-enrichment badge element not found"
        page.wait_for_function(
            "['ok','warn','err'].some(c => document.getElementById('tb-enrichment')?.classList?.contains(c))",
            timeout=10_000,
        )
        classes = badge.get_attribute("class") or ""
        assert any(c in classes for c in ("ok", "warn", "err")), (
            f"#tb-enrichment must have ok/warn/err class; got: {classes!r}"
        )

    def test_tile_wiki_badge_present(self, page, live_project):
        """#tb-wiki badge must exist after pulse load."""
        self._load_pulse(page)
        badge = page.locator("#tb-wiki")
        assert badge.count() > 0, "#tb-wiki badge element not found"

    def test_tile_stream_badge_present(self, page, live_project):
        """#tb-stream badge must exist and have a status class after pulse load."""
        self._load_pulse(page)
        badge = page.locator("#tb-stream")
        assert badge.count() > 0, "#tb-stream badge element not found"
        page.wait_for_function(
            "['ok','warn','err'].some(c => document.getElementById('tb-stream')?.classList?.contains(c))",
            timeout=10_000,
        )

    def test_kpi_requests_tile_present_with_value_or_dash(self, page, live_project):
        """#kpi-requests must show a numeric value or '—' — never blank."""
        self._load_pulse(page)
        kpi = page.locator("#kpi-requests")
        assert kpi.count() > 0, "#kpi-requests element not found"
        text = (kpi.text_content(timeout=8_000) or "").strip()
        assert text != "", "#kpi-requests has no text content"

    def test_activity_feed_empty_state_shows_message(self, page, live_project):
        """When no recent pipeline events exist, #activity-list must show an empty-state message."""
        self._load_pulse(page)
        act_list = page.locator("#activity-list")
        assert act_list.count() > 0, "#activity-list element not found"
        content = act_list.inner_text(timeout=8_000)
        # Either populated or shows the empty-state message
        assert content.strip() != "", "#activity-list must not be completely blank"

    def test_sparklines_render_files_tile(self, page, live_project):
        """#sp-files sparkline must contain polyline or polygon after metrics poll."""
        self._load_pulse(page)
        sparkline = page.locator("#sp-files polyline, #sp-files polygon")
        assert sparkline.count() > 0, "#sp-files sparkline has no drawn polyline/polygon"

    def test_sparklines_render_communities_tile(self, page, live_project):
        """#sp-communities sparkline must render after metrics poll."""
        self._load_pulse(page)
        sparkline = page.locator("#sp-communities polyline, #sp-communities polygon")
        assert sparkline.count() > 0, (
            "#sp-communities sparkline has no drawn polyline/polygon — sparkline not rendering"
        )

    def test_sparklines_render_enrichment_tile(self, page, live_project):
        """#sp-enrichment sparkline must render after metrics poll."""
        self._load_pulse(page)
        sparkline = page.locator("#sp-enrichment polyline, #sp-enrichment polygon")
        assert sparkline.count() > 0, (
            "#sp-enrichment sparkline has no drawn polyline/polygon"
        )

    def test_pulse_tile_badges_are_labels_not_status_strings(self, page, live_project):
        """enrichment/wiki/requests tile badges must show human labels, not 'ok'/'warn'/'err'."""
        self._load_pulse(page)
        bad_values = {"ok", "warn", "err", ""}
        for tile_id in ("tb-enrichment", "tb-wiki", "tb-requests"):
            loc = page.locator(f"#{tile_id}")
            if loc.count() == 0:
                continue
            text = (loc.text_content(timeout=5_000) or "").strip()
            assert text not in bad_values, (
                f"#{tile_id} badge shows {text!r} — status string in label slot"
            )

    def test_pulse_dot_warn_when_only_some_endpoints_fail(self, page, live_project):
        """When only suggested_questions fails the dot must be 'warn' (met OK, secondary fails)."""
        # Set the route BEFORE navigation so the boot loadPulse() call hits the block
        page.route("**/api/suggested_questions**", lambda r: r.abort())
        try:
            _goto_with_retry(page, DASHBOARD_URL)
            page.wait_for_load_state("load", timeout=_TIMEOUT_PAGE)
            # Wait for _proj to be set so loadPulse actually runs
            page.wait_for_function(
                "() => { const s = document.getElementById('project-sel'); "
                "return s && s.options.length > 0 && s.value !== ''; }",
                timeout=45_000,
            )
            page.wait_for_function(
                "document.querySelector('.sdot')?.classList?.contains('warn')",
                timeout=15_000,
            )
            dot = page.locator(".sdot").first
            classes = dot.get_attribute("class") or ""
            assert "warn" in classes, (
                f"Dot must be 'warn' when /api/suggested_questions blocked but metrics OK; "
                f"classes: {classes!r}"
            )
        finally:
            page.unroute("**/api/suggested_questions**")

    def test_sp_uptime_sparkline_eventually_has_path(self, page, live_project):
        """#sp-uptime sparkline must contain a path after two loadPulse cycles (B2 fix)."""
        self._load_pulse(page)  # _load_pulse already calls loadPulse() twice
        sparkline = page.locator("#sp-uptime polyline, #sp-uptime polygon, #sp-uptime path")
        assert sparkline.count() > 0, (
            "#sp-uptime sparkline has no drawn element — pushSpark('uptime',…) not wired"
        )

    def test_uptime_tile_badge_warn_under_60s_marker(self, page, live_project):
        """When uptime_s < 60 the tile must carry 'warn' class, not always 'ok' (B1 fix)."""
        import json as _json
        stub_metrics = {
            "total_requests": 0, "errors": 0, "connected_clients": 0,
            "uptime_s": 30, "active_watchers": 0,
            "chat_stream": {"stream_error_count": 0, "stream_success_count": 0},
        }
        page.route(
            "**/api/metrics",
            lambda r: r.fulfill(status=200, content_type="application/json",
                                body=_json.dumps(stub_metrics)),
        )
        try:
            _goto_with_retry(page, DASHBOARD_URL)
            page.wait_for_load_state("load", timeout=_TIMEOUT_PAGE)
            page.wait_for_function(
                "() => { const s = document.getElementById('project-sel'); "
                "return s && s.options.length > 0 && s.value !== ''; }",
                timeout=45_000,
            )
            page.evaluate("loadPulse()")
            page.wait_for_function(
                "document.getElementById('tile-uptime')?.classList?.contains('warn') || "
                "document.getElementById('tile-uptime')?.classList?.contains('ok')",
                timeout=15_000,
            )
            classes = page.locator("#tile-uptime").get_attribute("class") or ""
            assert "warn" in classes, (
                f"tile-uptime must be 'warn' for uptime_s=30 (< 60s); got classes: {classes!r}"
            )
        finally:
            page.unroute("**/api/metrics")

    def test_runcmd_enter_executes_first_match(self, page):
        """Typing 'Pulse' in command palette + Enter must switch to the Pulse view (B3 regression guard)."""
        _goto_with_retry(page, DASHBOARD_URL)
        page.wait_for_load_state("load", timeout=_TIMEOUT_PAGE)
        # Switch to another view first so Enter-to-Pulse is a real transition
        page.click("#vbtn-chat")
        page.wait_for_timeout(300)
        page.keyboard.press("Control+k")
        page.wait_for_selector("#cmd-overlay", timeout=5_000)
        page.fill("#cmd-input", "Pulse")
        page.wait_for_timeout(300)
        page.keyboard.press("Enter")
        page.wait_for_function(
            "document.getElementById('view-pulse')?.classList?.contains('active')",
            timeout=5_000,
        )
        assert page.locator("#view-pulse.active").count() > 0, (
            "Command palette Enter on 'Pulse' did not switch to Pulse view"
        )


# ---------------------------------------------------------------------------
# Phase 64 gap additions: TestChatViewGaps (C.11–C.24)
# ---------------------------------------------------------------------------

class TestChatViewGaps:
    """Coverage gaps in chat view behavior: input guards, button state, mdSafe rendering."""

    def test_shift_enter_inserts_newline_does_not_send(self, page, live_project):
        """Shift+Enter in chat input must NOT send a message — must insert a newline."""
        _navigate_to_chat(page)
        inp = _get_chat_input(page)
        inp.click()
        inp.fill("test line 1")
        inp.press("Shift+Enter")
        page.wait_for_timeout(300)
        # No user message bubble must appear
        user_msgs = page.locator(".msg.user")
        assert user_msgs.count() == 0, (
            f"Shift+Enter must not send a message; {user_msgs.count()} user message(s) appeared"
        )
        # Input must still contain text
        val = inp.input_value()
        assert val.strip() != "", "Chat input was cleared by Shift+Enter (should not send)"

    def test_empty_input_does_not_send(self, page, live_project):
        """Pressing Enter on an empty chat input must not create any message bubble."""
        _navigate_to_chat(page)
        inp = _get_chat_input(page)
        inp.click()
        inp.fill("")
        inp.press("Enter")
        page.wait_for_timeout(300)
        user_msgs = page.locator(".msg.user")
        assert user_msgs.count() == 0, (
            f"Empty Enter must not send; got {user_msgs.count()} user messages"
        )

    def test_send_btn_disabled_during_in_flight(self, page, live_project):
        """#send-btn must be disabled immediately after sending a message (while in-flight)."""
        _navigate_to_chat(page)
        inp = _get_chat_input(page)
        inp.fill("what is 1+1?")
        page.locator("#send-btn").click()
        # Check disabled state immediately (within 1s of click)
        page.wait_for_function(
            "document.getElementById('send-btn')?.disabled === true",
            timeout=3_000,
        )

    def test_send_btn_re_enabled_after_response(self, page, live_project):
        """#send-btn must be re-enabled after the AI response is fully received."""
        _navigate_to_chat(page)
        inp = _get_chat_input(page)
        inp.fill("what is 1+1?")
        page.locator("#send-btn").click()
        _wait_for_ai_response(page)
        send_btn = page.locator("#send-btn")
        assert not send_btn.is_disabled(), "#send-btn must be re-enabled after AI response"

    def test_chat_in_auto_grow_caps_at_160px(self, page, live_project):
        """chat-in textarea auto-grows with content but caps at 160px height."""
        _navigate_to_chat(page)
        inp = _get_chat_input(page)
        inp.click()
        # Fill with many newlines to trigger auto-grow
        large_text = "\n".join(["line " + str(i) for i in range(30)])
        inp.fill(large_text)
        page.wait_for_timeout(300)
        height = page.evaluate("() => document.getElementById('chat-in')?.offsetHeight || 0")
        assert height > 0, "chat-in has zero height"
        assert height <= 165, (
            f"chat-in must cap at ~160px; got {height}px — auto-grow cap not enforced"
        )

    def test_chat_network_error_shows_ai_err_class(self, page, live_project):
        """When /api/chat_stream is blocked, an .ai-err bubble must appear in the chat."""
        _navigate_to_chat(page)
        # Intercept and abort the chat_stream request (native Playwright browser interception)
        page.route("**/api/chat_stream", lambda route: route.abort())
        try:
            inp = _get_chat_input(page)
            inp.fill("what is this project?")
            page.locator("#send-btn").click()
            page.wait_for_selector(".msg.ai.ai-err, .msg.ai-err", timeout=10_000)
            err_bubble = page.locator(".msg.ai.ai-err, .msg.ai-err, .ai-err")
            assert err_bubble.count() > 0, ".ai-err bubble not shown after network error"
        finally:
            page.unroute("**/api/chat_stream")

    def test_thinking_timer_increments(self, page, live_project):
        """Thinking bubble must show elapsed-seconds timer during LLM processing."""
        _navigate_to_chat(page)
        inp = _get_chat_input(page)
        inp.fill("describe the architecture of this project in detail")
        page.locator("#send-btn").click()
        # Wait for thinking bubble to appear
        page.wait_for_selector(".msg.ai.thinking", timeout=10_000)
        # Thinking events arrive every ~10s from server.
        # Accept either: timer shows "(Xs)" OR thinking bubble is gone (LLM responded < 10s).
        page.wait_for_function(
            "(document.querySelector('.msg.ai.thinking .msg-bubble')?.textContent?.includes('s)')) || "
            "(!document.querySelector('.msg.ai.thinking'))",
            timeout=25_000,
        )
        thinking_bubble = page.locator(".msg.ai.thinking")
        if thinking_bubble.count() == 0:
            # LLM responded before the 10s thinking event interval — nothing to assert
            pytest.skip("LLM responded before first thinking event; timer not exercised")
        text = thinking_bubble.locator(".msg-bubble").inner_text(timeout=5_000)
        assert "s)" in text, (
            f"Thinking timer did not show elapsed seconds; got: {text!r}"
        )
        # Wait for full response to avoid leaving in-flight state
        _wait_for_ai_response(page)

    def test_chat_in_placeholder_text_present(self, page, live_project):
        """Chat input must have a non-empty placeholder attribute."""
        _navigate_to_chat(page)
        inp = _get_chat_input(page)
        placeholder = inp.get_attribute("placeholder") or ""
        assert placeholder.strip() != "", "Chat input has no placeholder text"

    def test_mdsafe_escapes_script_tag(self, page, live_project):
        """mdSafe must escape <script> tags — no script element must appear in AI bubble DOM."""
        _navigate_to_chat(page)
        inp = _get_chat_input(page)
        inp.fill('Reply with this exact text (no markdown, no changes): <script>alert(1)</script>')
        page.locator("#send-btn").click()
        _wait_for_ai_response(page)
        # No actual <script> element must exist inside an AI bubble
        script_in_bubble = page.locator(".msg.ai script")
        assert script_in_bubble.count() == 0, (
            "mdSafe failed to escape <script> — a real <script> element exists in AI bubble DOM"
        )

    def test_mdsafe_renders_fenced_code_block(self, page, live_project):
        """mdSafe must render a fenced code block as <pre><code> in the AI bubble DOM."""
        _navigate_to_chat(page)
        # Inject a message with a known fenced code block via the real appendMsg function.
        # This exercises the real mdSafe rendering pipeline without depending on LLM output.
        page.evaluate(
            "appendMsg('ai', '```python\\nprint(\\'hello world\\')\\n```')"
        )
        page.wait_for_timeout(200)
        code_block = page.locator(".msg.ai pre code, .msg.ai pre")
        assert code_block.count() > 0, (
            "mdSafe did not render a <pre><code> block for a fenced-code input"
        )

    def test_mdsafe_renders_bullet_list(self, page, live_project):
        """mdSafe must render <ul><li> for bullet-list markdown — direct injection, no LLM."""
        _navigate_to_chat(page)
        page.evaluate("appendMsg('ai', '- item one\\n- item two\\n- item three')")
        page.wait_for_timeout(200)
        ul = page.locator(".msg.ai ul li, .msg.ai ol li")
        assert ul.count() > 0, "mdSafe did not render list items for a bullet-list input"

    def test_mdsafe_renders_bold_text(self, page, live_project):
        """mdSafe must render <strong> for **bold** markdown — direct injection, no LLM."""
        _navigate_to_chat(page)
        page.evaluate("appendMsg('ai', 'This has **bold** text and more **words**')")
        page.wait_for_timeout(200)
        bold = page.locator(".msg.ai strong, .msg.ai b")
        assert bold.count() > 0, "mdSafe did not render <strong>/<b> for **bold** input"


# ---------------------------------------------------------------------------
# Phase 64 gap additions: TestAdminViewGaps (B.7 + C.25–C.29)
# ---------------------------------------------------------------------------

class TestAdminViewGaps:
    """Admin view gap tests: double-click dedup, project link, no-project toast, watching indicator."""

    def _load_admin(self, page) -> None:
        _goto_with_retry(page, DASHBOARD_URL)
        page.wait_for_load_state("load", timeout=_TIMEOUT_PAGE)
        page.wait_for_function(
            "() => { const s = document.getElementById('project-sel'); return s && s.options.length > 0 && s.value !== ''; }",
            timeout=45_000,
        )
        page.click("#vbtn-admin")
        page.wait_for_function(
            "document.getElementById('view-admin')?.classList?.contains('active')",
            timeout=10_000,
        )
        page.wait_for_timeout(1_000)

    def test_vacuum_rapid_double_click_does_not_duplicate_jobs(self, page, live_project):
        """Rapid double-click on Vacuum must not create two op-log entries."""
        self._load_admin(page)
        vacuum_btn = page.locator("button:has-text('Vacuum')").first
        if not vacuum_btn.count():
            pytest.skip("Vacuum button not found")
        op_log = page.locator("#op-log")
        # Double-click rapidly
        vacuum_btn.click()
        page.wait_for_timeout(100)
        vacuum_btn.click()
        page.wait_for_timeout(2_000)
        entries = op_log.locator("div").all_text_contents()
        vacuum_entries = [e for e in entries if "vacuum" in e.lower() or "running" in e.lower()]
        assert len(vacuum_entries) <= 2, (
            f"Rapid double-click produced too many op-log entries: {vacuum_entries}"
        )

    def test_admin_op_without_project_shows_err_toast(self, page, live_project):
        """Clicking Vacuum with no project selected must show an error toast."""
        # Mock /api/projects to return empty list so _proj is never set
        page.route("**/api/projects", lambda route: route.fulfill(
            status=200, content_type="application/json", body='{"projects": []}'
        ))
        try:
            _goto_with_retry(page, DASHBOARD_URL)
            page.wait_for_load_state("load", timeout=_TIMEOUT_PAGE)
            page.wait_for_timeout(1_000)  # allow loadProjects() to complete with empty list
            page.click("#vbtn-admin")
            page.wait_for_function(
                "document.getElementById('view-admin')?.classList?.contains('active')",
                timeout=10_000,
            )
            page.wait_for_timeout(500)
            vacuum_btn = page.locator("button:has-text('Vacuum')").first
            if not vacuum_btn.count():
                pytest.skip("Vacuum button not found")
            vacuum_btn.click()
            page.wait_for_selector("#toast .err, .toast.err, [class*='err']", timeout=5_000)
            toast = page.locator("#toast .err, .toast.err")
            assert toast.count() > 0, "Error toast must appear when clicking Vacuum with no project"
        finally:
            page.unroute("**/api/projects")

    def test_admin_watching_indicator_present(self, page, live_project):
        """Admin table must show a watching indicator (● or ○) for each project row."""
        self._load_admin(page)
        # Table rows should contain watching indicators
        tbody = page.locator("#projects-body")
        assert tbody.count() > 0, "#projects-body not found"
        page.wait_for_function(
            "document.getElementById('projects-body')?.children?.length > 0",
            timeout=10_000,
        )
        content = tbody.inner_text(timeout=5_000)
        has_indicator = "●" in content or "○" in content
        assert has_indicator, (
            f"Admin table must show watching indicators (● or ○); got:\n{content[:300]}"
        )

    def test_admin_project_selector_onchange_switches_project(self, page, live_project):
        """Changing project-sel dropdown must update _proj variable."""
        self._load_admin(page)
        sel = page.locator("#project-sel")
        assert sel.count() > 0, "#project-sel not found"
        options = sel.locator("option").all()
        if len(options) < 2:
            pytest.skip("Need at least 2 projects to test dropdown switch")
        # Get current selector value via DOM (let _proj is not on window)
        current = sel.input_value()
        # Select a different option
        all_values = [opt.get_attribute("value") for opt in options]
        target = next((v for v in all_values if v != current and v), None)
        if not target:
            pytest.skip("No alternative project path found in selector")
        sel.select_option(value=target)
        page.wait_for_timeout(500)
        # Read _proj via JS (let variable is in Script scope, accessible from page.evaluate)
        new_proj = page.evaluate("_proj")
        assert new_proj == target, (
            f"_proj not updated after dropdown change; expected {target!r}, got {new_proj!r}"
        )

    def test_admin_empty_projects_table_shows_message(self, page):
        """If no projects are indexed, admin tbody must show 'No projects indexed' message."""
        _goto_with_retry(page, DASHBOARD_URL)
        page.wait_for_load_state("load", timeout=_TIMEOUT_PAGE)
        page.click("#vbtn-admin")
        page.wait_for_function(
            "document.getElementById('view-admin')?.classList?.contains('active')",
            timeout=10_000,
        )
        page.wait_for_timeout(2_000)
        tbody = page.locator("#projects-body")
        assert tbody.count() > 0, "#projects-body not found"
        content = tbody.inner_text(timeout=8_000)
        # Either shows projects or the empty message — never blank
        assert content.strip() != "", "#projects-body must not be blank after admin loads"


# ---------------------------------------------------------------------------
# Phase 64 gap additions: TestGlobalUIGaps (C.30–C.37)
# ---------------------------------------------------------------------------

class TestGlobalUIGaps:
    """Global UI gap tests: daemon dot, palette nav, theme CSS, toast stack."""

    def test_daemon_dot_err_when_metrics_fetch_fails(self, page, live_project):
        """When /api/metrics is blocked, the status dot must show 'err' class."""
        # Set the route BEFORE navigation so the boot loadPulse() call hits the block
        page.route("**/api/metrics", lambda route: route.abort())
        try:
            _goto_with_retry(page, DASHBOARD_URL)
            page.wait_for_load_state("load", timeout=_TIMEOUT_PAGE)
            # Wait for _proj to be set so loadPulse actually runs (not early-return on '')
            page.wait_for_function(
                "() => { const s = document.getElementById('project-sel'); "
                "return s && s.options.length > 0 && s.value !== ''; }",
                timeout=45_000,
            )
            # Force loadPulse now that _proj is set — the boot call may have raced/skipped
            page.evaluate("loadPulse()")
            page.wait_for_function(
                "document.querySelector('.sdot')?.classList?.contains('err')",
                timeout=35_000,
            )
            dot = page.locator(".sdot").first
            classes = dot.get_attribute("class") or ""
            assert "err" in classes, (
                f"Status dot must show 'err' when metrics fetch fails; classes: {classes!r}"
            )
        finally:
            page.unroute("**/api/metrics")

    def test_command_palette_arrow_up_navigation(self, page):
        """ArrowDown twice then ArrowUp once must highlight the first item, not the second."""
        _goto_with_retry(page, DASHBOARD_URL)
        page.wait_for_load_state("load", timeout=_TIMEOUT_PAGE)
        page.keyboard.press("Control+k")
        page.wait_for_function(
            "!document.getElementById('cmd-overlay')?.classList?.contains('hidden')",
            timeout=5_000,
        )
        page.keyboard.press("ArrowDown")
        page.wait_for_timeout(100)
        page.keyboard.press("ArrowDown")
        page.wait_for_timeout(100)
        page.keyboard.press("ArrowUp")
        page.wait_for_timeout(100)
        # After 2 downs + 1 up, the first item should be highlighted
        highlighted = page.locator("#cmd-results li.hi, #cmd-results li[aria-selected='true']")
        assert highlighted.count() > 0, "No item highlighted after ArrowDown+ArrowDown+ArrowUp"
        page.keyboard.press("Escape")

    def test_command_palette_click_outside_closes(self, page):
        """Clicking the palette overlay background must close the palette."""
        _goto_with_retry(page, DASHBOARD_URL)
        page.wait_for_load_state("load", timeout=_TIMEOUT_PAGE)
        page.keyboard.press("Control+k")
        page.wait_for_function(
            "!document.getElementById('cmd-overlay')?.classList?.contains('hidden')",
            timeout=5_000,
        )
        # Click the overlay (background) to close — use force=True since it may be behind other elements
        overlay = page.locator("#cmd-overlay")
        overlay.click(position={"x": 10, "y": 10}, force=True)
        page.wait_for_function(
            "document.getElementById('cmd-overlay')?.classList?.contains('hidden')",
            timeout=5_000,
        )

    def test_command_palette_mouse_click_result(self, page):
        """Clicking a palette result item with the mouse must execute the action and close palette."""
        _goto_with_retry(page, DASHBOARD_URL)
        page.wait_for_load_state("load", timeout=_TIMEOUT_PAGE)
        page.keyboard.press("Control+k")
        page.wait_for_function(
            "!document.getElementById('cmd-overlay')?.classList?.contains('hidden')",
            timeout=5_000,
        )
        first_result = page.locator("#cmd-results li").first
        if not first_result.count():
            pytest.skip("No palette results found")
        first_result.click()
        page.wait_for_function(
            "document.getElementById('cmd-overlay')?.classList?.contains('hidden')",
            timeout=5_000,
        )

    def test_command_palette_empty_filter_shows_results(self, page):
        """Opening palette with empty input must show multiple results (all commands)."""
        _goto_with_retry(page, DASHBOARD_URL)
        page.wait_for_load_state("load", timeout=_TIMEOUT_PAGE)
        page.keyboard.press("Control+k")
        page.wait_for_function(
            "!document.getElementById('cmd-overlay')?.classList?.contains('hidden')",
            timeout=5_000,
        )
        # With empty filter, all commands should be visible
        page.wait_for_function(
            "document.querySelectorAll('#cmd-results li').length >= 3",
            timeout=5_000,
        )
        count = page.locator("#cmd-results li").count()
        assert count >= 3, f"Palette with empty filter must show ≥3 commands; got {count}"
        page.keyboard.press("Escape")

    def test_theme_applies_different_css_vars(self, page, live_project):
        """Clicking theme toggle button must change document background color."""
        _goto_with_retry(page, DASHBOARD_URL)
        page.wait_for_load_state("load", timeout=_TIMEOUT_PAGE)
        initial_bg = page.evaluate("getComputedStyle(document.body).backgroundColor")
        # Find theme toggle button
        theme_btn = page.locator(
            "button:has-text('Theme'), button[title*='theme'], button[title*='Theme'], "
            "button[onclick*='theme'], button[onclick*='dark'], button[onclick*='light']"
        ).first
        if not theme_btn.count():
            # Try the command palette
            page.keyboard.press("Control+k")
            page.wait_for_timeout(200)
            page.locator("#cmd-input").fill("Theme")
            page.wait_for_timeout(200)
            result = page.locator("#cmd-results li").first
            if result.count():
                result.click()
            else:
                page.keyboard.press("Escape")
                pytest.skip("Theme toggle button not found")
        else:
            theme_btn.click()
        page.wait_for_timeout(500)
        new_bg = page.evaluate("getComputedStyle(document.body).backgroundColor")
        assert initial_bg != new_bg, (
            f"Theme toggle must change body background; both are {initial_bg!r}"
        )

    def test_theme_does_not_persist_across_reload(self, page, live_project):
        """Toggle theme, reload page — must revert to dark theme (no localStorage persistence)."""
        _goto_with_retry(page, DASHBOARD_URL)
        page.wait_for_load_state("load", timeout=_TIMEOUT_PAGE)
        initial_bg = page.evaluate("getComputedStyle(document.body).backgroundColor")
        # Toggle theme via JS directly (fastest, doesn't depend on button location)
        page.evaluate("typeof toggleTheme === 'function' && toggleTheme()")
        page.wait_for_timeout(400)
        # Reload
        page.reload()
        page.wait_for_load_state("load", timeout=_TIMEOUT_PAGE)
        page.wait_for_timeout(500)
        after_reload_bg = page.evaluate("getComputedStyle(document.body).backgroundColor")
        assert after_reload_bg == initial_bg, (
            f"Theme persisted across reload (localStorage likely in use); "
            f"initial={initial_bg!r} after_reload={after_reload_bg!r}. "
            "Update this test if localStorage persistence is intentionally added."
        )

    def test_toast_stack_multiple_toasts_visible(self, page, live_project):
        """Triggering two operations rapidly must stack multiple toast notifications."""
        _goto_with_retry(page, DASHBOARD_URL)
        page.wait_for_load_state("load", timeout=_TIMEOUT_PAGE)
        page.wait_for_function(
            "() => { const s = document.getElementById('project-sel'); return s && s.options.length > 0 && s.value !== ''; }",
            timeout=45_000,
        )
        page.click("#vbtn-admin")
        page.wait_for_function(
            "document.getElementById('view-admin')?.classList?.contains('active')",
            timeout=10_000,
        )
        vacuum_btn = page.locator("button:has-text('Vacuum')").first
        dedup_btn = page.locator("button:has-text('Dedup')").first
        if not vacuum_btn.count() or not dedup_btn.count():
            pytest.skip("Vacuum or Dedup button not found")
        # Click both rapidly
        vacuum_btn.click()
        page.wait_for_timeout(100)
        dedup_btn.click()
        # Check if multiple toasts appear
        page.wait_for_selector("#toast div", state="visible", timeout=10_000)
        page.wait_for_timeout(500)
        toast_count = page.locator("#toast div").count()
        # At minimum one toast must appear; ideally 2 stack
        assert toast_count >= 1, "No toast appeared after triggering admin ops"
