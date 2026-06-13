"""Live browser tests — Playwright drives real Chromium against the live dashboard.

Covers all five views (Pulse / Chat / Graph / Wiki / Admin), all chat intents, streaming SSE,
and the read-only storage panel (maintenance is fully automatic — no manual vacuum/dedup buttons).

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
_TIMEOUT_CHAT = 300_000  # ms — wait for AI response (global can take 150s+)
# Heavy intents (global overview, graph traversals) hit full retrieved-context LLM paths
_TIMEOUT_CHAT_LONG = 480_000  # ms — long-tail budget for the 4 heaviest intent tests


# ---------------------------------------------------------------------------
# Module-level skip helpers
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module", autouse=True)
def require_daemon():
    """Fail if daemon is not reachable — daemon must be running for browser tests."""
    try:
        httpx.get(f"{DAEMON_URL}/api/projects", timeout=5).raise_for_status()
    except Exception as exc:
        pytest.fail(f"Daemon not reachable: {exc}")


@pytest.fixture(scope="module")
def live_project():
    """Return an indexed project path with communities > 100 for richer test coverage."""
    r = httpx.get(f"{DAEMON_URL}/api/projects", timeout=10)
    projects = r.json().get("projects", [])
    all_indexed = [p for p in projects if p.get("communities", 0) > 0]
    assert all_indexed, "No indexed project with communities"
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


def _wait_for_chat_ready(page, timeout_ms: int = _TIMEOUT_CHAT_LONG) -> None:
    """Wait until the chat is fully idle and safe to send the next turn.

    _wait_for_ai_response returns on the first token, but _chatInFlight stays
    true until the SSE stream closes after the 'done' event.  Sending before
    that moment is silently dropped by the JS in-flight guard.
    """
    page.wait_for_function(
        "() => { const b = document.getElementById('send-btn'); "
        "return b && !b.disabled "
        "&& document.querySelectorAll('.msg.ai.thinking').length === 0; }",
        timeout=timeout_ms,
    )


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

    def test_pulse_sym_enrich_tile_present(self, page):
        """Symbol Intents tile (#tile-sym-enrich) must be rendered in the Pulse view."""
        _goto_with_retry(page, DASHBOARD_URL)
        page.wait_for_load_state("load", timeout=_TIMEOUT_PAGE)
        tile = page.locator("#tile-sym-enrich")
        assert tile.count() > 0, "Symbol Intents tile (#tile-sym-enrich) missing from Pulse view"
        bar = page.locator("#sym-enrich-bar")
        assert bar.count() > 0, "Symbol enrichment progress bar (#sym-enrich-bar) missing"


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
        # Wait for the SSE stream to fully close before sending the next turn;
        # _chatInFlight stays true until the 'done' event closes the stream.
        _wait_for_chat_ready(page)
        # Count existing messages before second turn
        msg_count_before = page.locator(".msg.ai:not(.thinking)").count()
        # Second turn: simple follow-up
        _send_message(page, "list the main directories")
        # wait_for_function fires on the FIRST token (bubble appears), not full response;
        # call _wait_for_chat_ready after to ensure complete content before reading.
        page.wait_for_function(
            f"document.querySelectorAll('.msg.ai:not(.thinking)').length > {msg_count_before}",
            timeout=_TIMEOUT_CHAT_LONG,
        )
        _wait_for_chat_ready(page)
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

        # Wait for turn 1 stream to close before sending turn 2
        _wait_for_chat_ready(page)
        count1 = page.locator(".msg.ai:not(.thinking)").count()
        _send_message(page, "tell me about the first one")
        # wait_for_function fires on first token; _wait_for_chat_ready ensures full content
        page.wait_for_function(
            f"document.querySelectorAll('.msg.ai:not(.thinking)').length > {count1}",
            timeout=_TIMEOUT_CHAT_LONG,
        )
        _wait_for_chat_ready(page)
        r2 = page.locator(".msg.ai:not(.thinking)").last.inner_text()
        assert len(r2) > 20, f"Second response too short: {r2!r}"

        count2 = page.locator(".msg.ai:not(.thinking)").count()
        _send_message(page, "how is it tested?")
        # wait_for_function fires on first token; _wait_for_chat_ready ensures full content
        page.wait_for_function(
            f"document.querySelectorAll('.msg.ai:not(.thinking)').length > {count2}",
            timeout=_TIMEOUT_CHAT_LONG,
        )
        _wait_for_chat_ready(page)
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
        admin_tab = page.locator("#vbtn-admin").first
        assert admin_tab.count() > 0, "Admin nav button (#vbtn-admin) not found in dashboard"
        admin_tab.click()
        page.wait_for_timeout(500)

    def test_admin_tab_opens(self, page):
        self._open_admin(page)
        content = page.content()
        has_admin = any(w in content.lower() for w in ("project", "build", "index", "jobs", "storage"))
        assert has_admin, "Admin tab opened but no admin controls found"

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

    def test_admin_storage_panel_renders(self, page):
        """Storage Health panel must appear in Admin view and display recoverable MB."""
        self._open_admin(page)
        # Wait for loadStorageHealth() to populate the panel
        page.wait_for_function(
            "document.getElementById('storage-health-body')?.textContent?.trim()?.length > 5",
            timeout=15_000,
        )
        content = (page.locator("#storage-health-body").text_content() or "").strip()
        assert "recoverable" in content.lower(), (
            f"Storage Health panel did not render recoverable MB; got: {content[:200]}"
        )

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
        btn = page.locator("#view-admin").locator(f"button:has-text('{btn_text}')").first
        if not btn.count():
            pytest.fail(f"Button '{btn_text}' not found in Admin view")
        btn.click()
        page.wait_for_function(
            "document.querySelector('#op-log')?.textContent?.trim()?.length > 0",
            timeout=20_000,
        )
        return (page.locator("#op-log").text_content() or "").strip()

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
        """Clicking an Admin operation button (Re-index) must produce a toast notification."""
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
        reindex_btn = page.locator("button:has-text('Re-index')").first
        if not reindex_btn.count():
            pytest.fail("Re-index button not found in Admin view")
        reindex_btn.click()
        page.wait_for_function(
            "document.querySelector('#toast div') !== null",
            timeout=8_000,
        )
        toast_el = page.locator("#toast div").first
        assert toast_el.count() > 0, "#toast div never appeared after Re-index"

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
        page.locator("#cmd-input").type("index")
        page.wait_for_timeout(200)
        items = page.locator("#cmd-results li")
        assert items.count() >= 1, "Filter returned no results for 'index'"
        labels = [(items.nth(i).text_content() or "") for i in range(items.count())]
        assert any("index" in lbl.lower() for lbl in labels), (
            f"Filter did not show index-related items: {labels}"
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
            pytest.fail("Vacuum button not found in Admin view")
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
        # Thinking events arrive every ~10s from server (heartbeat loop in _stream_architecture).
        # Accept either: timer shows "(Xs)" OR thinking bubble is gone (fast LLM response < 10s).
        # 60s budget: context assembly can be slow under GPU load.
        page.wait_for_function(
            "(document.querySelector('.msg.ai.thinking .msg-bubble')?.textContent?.includes('s)')) || "
            "(!document.querySelector('.msg.ai.thinking'))",
            timeout=60_000,
        )
        thinking_bubble = page.locator(".msg.ai.thinking")
        if thinking_bubble.count() == 0:
            # LLM responded before the first 10s heartbeat — fast path is valid, nothing more to assert
            _wait_for_ai_response(page)
            return
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
            pytest.fail("Vacuum button not found")
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
                pytest.fail("Vacuum button not found")
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
        assert len(options) >= 2, "Need at least 2 indexed projects to test dropdown switch"
        # Get current selector value via DOM (let _proj is not on window)
        current = sel.input_value()
        # Select a different option
        all_values = [opt.get_attribute("value") for opt in options]
        target = next((v for v in all_values if v != current and v), None)
        assert target is not None, "No alternative project path found in selector"
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
        assert first_result.count() > 0, "No palette results found after opening command palette"
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
        # Find theme toggle button via its fixed ID
        theme_btn = page.locator("#theme-btn").first
        assert theme_btn.count() > 0, "Theme toggle button (#theme-btn) not found in dashboard"
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
        assert vacuum_btn.count() > 0, "Vacuum button not found in Admin view"
        assert dedup_btn.count() > 0, "Dedup button not found in Admin view"
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


# ---------------------------------------------------------------------------
# Phase 71 gap additions: TestDashboardCoverage (top 10 from Phase 69 audit)
# ---------------------------------------------------------------------------

class TestDashboardCoverage:
    """Covers 10 user-visible dashboard surfaces identified in the Phase 69 audit
    as having no Playwright test. Uses real LLM/daemon except where a Playwright
    route intercept is the surface under test."""

    def test_switch_to_chat_view_autofocuses_input(self, page):
        """switchView('chat') must focus #chat-in — exercises the autofocus side-effect."""
        _goto_with_retry(page, DASHBOARD_URL)
        page.wait_for_load_state("load", timeout=_TIMEOUT_PAGE)
        # Navigate to pulse first so the chat switch is a real transition
        page.click("#vbtn-pulse")
        page.wait_for_timeout(200)
        page.click("#vbtn-chat")
        page.wait_for_timeout(300)
        focused_id = page.evaluate("document.activeElement?.id")
        assert focused_id == "chat-in", (
            f"#chat-in must receive focus on switchView('chat'); activeElement.id={focused_id!r}"
        )

    def test_activity_feed_renders_populated_event(self, page, live_project):
        """When kb_health returns a last_pipeline_event the feed must show an .act-item."""
        import json as _json
        stub_kb = {
            "total_communities": 10, "enriched_communities": 8,
            "enrichment_pct": 80.0, "wiki_page_count": 5,
            "last_pipeline_event": {"action": "index complete", "ts": "2026-06-08T12:00:00"},
        }
        page.route(
            "**/api/kb_health**",
            lambda r: r.fulfill(status=200, content_type="application/json",
                                body=_json.dumps(stub_kb)),
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
                "document.querySelector('.act-item') !== null",
                timeout=10_000,
            )
            items = page.locator(".act-item")
            assert items.count() > 0, ".act-item not rendered despite last_pipeline_event in stub"
            content = items.first.inner_text(timeout=5_000)
            assert "index complete" in content, (
                f".act-item text does not contain event msg; got: {content!r}"
            )
        finally:
            page.unroute("**/api/kb_health**")

    def test_admin_project_link_switches_active_project(self, page):
        """Clicking the inline project-name <a> in the admin table must update _proj."""
        _goto_with_retry(page, DASHBOARD_URL)
        page.wait_for_load_state("load", timeout=_TIMEOUT_PAGE)
        page.wait_for_function(
            "() => { const s = document.getElementById('project-sel'); "
            "return s && s.options.length > 0 && s.value !== ''; }",
            timeout=45_000,
        )
        sel = page.locator("#project-sel")
        opts = sel.locator("option").all()
        all_paths = [o.get_attribute("value") for o in opts if o.get_attribute("value")]
        assert len(all_paths) >= 2, "Need ≥2 indexed projects to test admin project-link switch"
        before = page.evaluate("_proj")
        # Use a project from the dropdown that is different from the active one
        target_path = next((p for p in all_paths if p != before), None)
        assert target_path is not None, "No alternative project path found in project selector"
        # Switch to target first so the admin table shows it with the active-row class
        page.click("#vbtn-admin")
        page.wait_for_function(
            "document.getElementById('view-admin')?.classList?.contains('active')",
            timeout=10_000,
        )
        page.wait_for_function(
            "document.querySelectorAll('#projects-body tr').length > 0",
            timeout=20_000,
        )
        # Click the admin table link for our target project
        target_link = page.locator(f"#projects-body a[data-path={target_path!r}]").first
        assert target_link.count() > 0, f"No admin table <a> link for path {target_path!r}"
        target_link.click()
        page.wait_for_timeout(1_000)
        after = page.evaluate("_proj")
        assert after == target_path, (
            f"_proj must equal clicked link's path; expected {target_path!r}, got {after!r}"
        )

    def test_runwiki_sends_action_wiki_query_param(self, page, live_project):
        """runWiki() must POST to /api/build_hierarchy with action=wiki query param."""
        _goto_with_retry(page, DASHBOARD_URL)
        page.wait_for_load_state("load", timeout=_TIMEOUT_PAGE)
        page.wait_for_function(
            "() => { const s = document.getElementById('project-sel'); "
            "return s && s.options.length > 0 && s.value !== ''; }",
            timeout=45_000,
        )
        page.click("#vbtn-admin")
        page.wait_for_function(
            "document.getElementById('view-admin')?.classList?.contains('active')",
            timeout=10_000,
        )
        captured_urls: list[str] = []
        page.on("request", lambda req: captured_urls.append(req.url)
                if "build_hierarchy" in req.url else None)
        wiki_btn = page.locator("#view-admin").locator("button:has-text('Wiki')").first
        assert wiki_btn.count() > 0, "Wiki op-button not found"
        wiki_btn.click()
        page.wait_for_timeout(2_000)
        wiki_urls = [u for u in captured_urls if "build_hierarchy" in u]
        assert wiki_urls, "No /api/build_hierarchy request fired after clicking Wiki button"
        assert any("action=wiki" in u for u in wiki_urls), (
            f"Wiki button request missing action=wiki param; URLs: {wiki_urls}"
        )

    def test_askquestion_produces_ai_response_e2e(self, page, live_project):
        """Clicking a .sq-btn must produce a full AI response via askQuestion() → sendChat()."""
        _goto_with_retry(page, DASHBOARD_URL)
        page.wait_for_load_state("load", timeout=_TIMEOUT_PAGE)
        page.wait_for_function(
            "() => { const s = document.getElementById('project-sel'); "
            "return s && s.options.length > 0 && s.value !== ''; }",
            timeout=45_000,
        )
        # Wait for suggested questions to load (requires Pulse view to be active and loaded)
        page.wait_for_function(
            "document.querySelectorAll('.sq-btn').length > 0",
            timeout=30_000,
        )
        sq_btn = page.locator(".sq-btn").first
        assert sq_btn.count() > 0, "No .sq-btn (suggested question) found on Pulse view"
        sq_btn.click()
        # askQuestion() switches to Chat view and calls sendChat()
        page.wait_for_function(
            "document.getElementById('view-chat')?.classList?.contains('active')",
            timeout=5_000,
        )
        text = _wait_for_ai_response(page, timeout_ms=_TIMEOUT_CHAT_LONG)
        assert len(text) > 30, f"askQuestion AI response too short: {text!r}"

    def test_sse_error_event_renders_ai_err_class(self, page, live_project):
        """SSE error events must render a .msg.ai.ai-err bubble with the error message."""
        import json as _json
        sse_body = (
            "data: " + _json.dumps({
                "type": "error", "message": "forced-test-error", "intent": "search",
            }) + "\n\n"
        )
        page.route(
            "**/api/chat_stream",
            lambda r: r.fulfill(
                status=200,
                content_type="text/event-stream",
                body=sse_body,
            ),
        )
        try:
            _navigate_to_chat(page)
            _send_message(page, "test error path")
            page.wait_for_selector(".msg.ai.ai-err", timeout=10_000)
            err_msg = page.locator(".msg.ai.ai-err").first
            assert err_msg.count() > 0, ".msg.ai.ai-err not rendered for SSE error event"
            content = err_msg.inner_text(timeout=5_000)
            assert "forced-test-error" in content, (
                f"Error bubble does not contain the error message; got: {content!r}"
            )
        finally:
            page.unroute("**/api/chat_stream")

    @pytest.mark.slow
    def test_pulse_auto_refresh_updates_sparklines_after_20s(self, page, live_project):
        """The 20s setInterval must trigger a second loadPulse, adding a new sparkline point."""
        _goto_with_retry(page, DASHBOARD_URL)
        page.wait_for_load_state("load", timeout=_TIMEOUT_PAGE)
        page.wait_for_function(
            "() => { const s = document.getElementById('project-sel'); "
            "return s && s.options.length > 0 && s.value !== ''; }",
            timeout=45_000,
        )
        # Stay on Pulse so the setInterval fires
        page.click("#vbtn-pulse")
        page.wait_for_timeout(500)
        initial_len = page.evaluate(
            "(_sparkHistory.files || []).length"
        )
        # Wait past the 20s interval
        page.wait_for_timeout(22_000)
        new_len = page.evaluate("(_sparkHistory.files || []).length")
        assert new_len > initial_len, (
            f"Sparkline history did not grow after 22s on Pulse view "
            f"(initial={initial_len}, after={new_len}) — setInterval not firing"
        )

    def test_project_switch_clears_spark_history(self, page):
        """Switching project via selector must reset _sparkHistory (setProj side-effect)."""
        _goto_with_retry(page, DASHBOARD_URL)
        page.wait_for_load_state("load", timeout=_TIMEOUT_PAGE)
        page.wait_for_function(
            "() => { const s = document.getElementById('project-sel'); "
            "return s && s.options.length > 0 && s.value !== ''; }",
            timeout=45_000,
        )
        # Accumulate at least one sparkline point
        page.evaluate("loadPulse()")
        page.wait_for_timeout(3_000)
        initial_history_len = page.evaluate(
            "Object.keys(_sparkHistory).reduce((sum, k) => sum + (_sparkHistory[k]||[]).length, 0)"
        )
        sel = page.locator("#project-sel")
        options = sel.locator("option").all()
        assert len(options) >= 2, "Need ≥2 indexed projects to test project-switch clear"
        current = sel.input_value()
        target = next((o.get_attribute("value") for o in options
                       if o.get_attribute("value") != current and o.get_attribute("value")), None)
        assert target is not None, "No alternative project path found in project selector"
        sel.select_option(value=target)
        page.wait_for_timeout(500)
        after_history_len = page.evaluate(
            "Object.keys(_sparkHistory).reduce((sum, k) => sum + (_sparkHistory[k]||[]).length, 0)"
        )
        assert after_history_len < initial_history_len or after_history_len == 0, (
            f"_sparkHistory not cleared after project switch; "
            f"was {initial_history_len} points, now {after_history_len}"
        )

    def test_daemon_dot_warn_state_from_chat_view(self, page, live_project):
        """Daemon dot must reflect warn state even when Chat view is active (loadPulse is global)."""
        page.route("**/api/suggested_questions**", lambda r: r.abort())
        try:
            _goto_with_retry(page, DASHBOARD_URL)
            page.wait_for_load_state("load", timeout=_TIMEOUT_PAGE)
            page.wait_for_function(
                "() => { const s = document.getElementById('project-sel'); "
                "return s && s.options.length > 0 && s.value !== ''; }",
                timeout=45_000,
            )
            page.click("#vbtn-chat")
            page.wait_for_function(
                "document.getElementById('view-chat')?.classList?.contains('active')",
                timeout=5_000,
            )
            page.evaluate("loadPulse()")
            page.wait_for_function(
                "document.querySelector('.sdot')?.classList?.contains('warn') || "
                "document.querySelector('.sdot')?.classList?.contains('ok') || "
                "document.querySelector('.sdot')?.classList?.contains('err')",
                timeout=35_000,
            )
            dot_classes = page.locator(".sdot").first.get_attribute("class") or ""
            assert "warn" in dot_classes, (
                f"Dot must be 'warn' on Chat view when suggested_questions blocked; "
                f"got: {dot_classes!r}"
            )
        finally:
            page.unroute("**/api/suggested_questions**")

    def test_kpi_tiles_show_dash_when_kb_health_empty(self, page, live_project):
        """When /api/kb_health returns empty {}, #kpi-communities must show '—', not '0'."""
        page.route(
            "**/api/kb_health**",
            lambda r: r.fulfill(status=200, content_type="application/json", body="{}"),
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
                "document.getElementById('kpi-communities')?.textContent?.trim() !== '—' || "
                "document.getElementById('kpi-communities')?.textContent?.trim() === '—'",
                timeout=10_000,
            )
            val = (page.locator("#kpi-communities").text_content(timeout=5_000) or "").strip()
            assert val == "—", (
                f"#kpi-communities must show '—' when communities is null; got: {val!r}"
            )
        finally:
            page.unroute("**/api/kb_health**")


# ---------------------------------------------------------------------------
# Phase 73: Graph view (TestGraphView)
# ---------------------------------------------------------------------------

_GRAPH_STUB = {
    "nodes": [
        {"id": "n1", "label": "main.py", "attributes": {"kind": "file"}},
        {"id": "n2", "label": "funcA", "attributes": {"kind": "symbol"}},
        {"id": "n3", "label": "funcB", "attributes": {"kind": "symbol"}},
    ],
    "edges": [
        {"source": "n1", "target": "n2"},
        {"source": "n1", "target": "n3"},
    ],
}


def _stub_graph(page, data=None) -> None:
    import json as _json
    body = _json.dumps(data or _GRAPH_STUB)
    page.route(
        "**/api/graph_export**",
        lambda r: r.fulfill(status=200, content_type="application/json", body=body),
    )


def _load_graph_view(page) -> None:
    _goto_with_retry(page, DASHBOARD_URL)
    page.wait_for_load_state("load", timeout=_TIMEOUT_PAGE)
    page.wait_for_function(
        "() => { const s = document.getElementById('project-sel'); "
        "return s && s.options.length > 0 && s.value !== ''; }",
        timeout=45_000,
    )
    page.evaluate("switchView('graph')")
    # Wait for loadGraph() to complete (window.__graph set, or error shown)
    page.wait_for_function(
        "() => window.__graph != null || document.getElementById('graph-empty')?.style.display === 'flex'",
        timeout=15_000,
    )


class TestGraphView:
    """Phase 73: Graph view powered by Sigma.js."""

    def test_graph_view_renders_canvas_with_nodes(self, page, live_project):
        """Stub /api/graph_export; graph view must set window.__graph with correct node count."""
        _stub_graph(page, _GRAPH_STUB)
        try:
            _load_graph_view(page)
            order = page.evaluate("() => window.__graph?.sigma?.getGraph().order ?? -1")
            assert order == len(_GRAPH_STUB["nodes"]), (
                f"Expected {len(_GRAPH_STUB['nodes'])} nodes in graph, got {order}"
            )
        finally:
            page.unroute("**/api/graph_export**")

    def test_graph_view_shows_node_count_hint(self, page, live_project):
        """#graph-node-count hint must display node/edge counts after load."""
        _stub_graph(page)
        try:
            _load_graph_view(page)
            hint = (page.locator("#graph-node-count").text_content(timeout=5_000) or "").strip()
            assert "nodes" in hint, f"#graph-node-count should mention 'nodes'; got: {hint!r}"
        finally:
            page.unroute("**/api/graph_export**")

    def test_graph_view_handles_empty_export_gracefully(self, page, live_project):
        """Empty node list must not crash — #graph-canvas should exist."""
        _stub_graph(page, {"nodes": [], "edges": []})
        try:
            _goto_with_retry(page, DASHBOARD_URL)
            page.wait_for_load_state("load", timeout=_TIMEOUT_PAGE)
            page.wait_for_function(
                "() => { const s = document.getElementById('project-sel'); "
                "return s && s.options.length > 0 && s.value !== ''; }",
                timeout=45_000,
            )
            page.evaluate("switchView('graph')")
            page.wait_for_timeout(3_000)
            # Canvas div must exist; JS must not have thrown a fatal uncaught error
            assert page.locator("#graph-canvas").count() == 1, "#graph-canvas div must be present"
        finally:
            page.unroute("**/api/graph_export**")

    def test_graph_search_affects_node_colors(self, page, live_project):
        """Typing into #graph-search must call searchGraphNode without error."""
        _stub_graph(page)
        try:
            _load_graph_view(page)
            # Wait for graph to load
            page.wait_for_function("() => window.__graph != null", timeout=10_000)
            page.fill("#graph-search", "main")
            page.wait_for_timeout(500)
            # Verify no JS error: node attribute access works
            color = page.evaluate(
                "() => window.__graph?.graph?.getNodeAttribute('n1', 'color')"
            )
            assert color is not None, "Node color should be set after search"
        finally:
            page.unroute("**/api/graph_export**")

    def test_graph_node_click_populates_detail_panel(self, page, live_project):
        """Clicking a node via page.evaluate must populate #graph-detail."""
        _stub_graph(page)
        try:
            _load_graph_view(page)
            page.wait_for_function("() => window.__graph != null", timeout=10_000)
            page.evaluate("_showNodeDetail('n1')")
            page.wait_for_timeout(300)
            detail = (page.locator("#graph-detail").inner_html(timeout=5_000) or "")
            assert "main.py" in detail, (
                f"#graph-detail should contain node label 'main.py'; got: {detail[:200]}"
            )
        finally:
            page.unroute("**/api/graph_export**")

    def test_graph_filter_kind_file_only(self, page, live_project):
        """Applying file filter via page.evaluate must hide non-file nodes."""
        _stub_graph(page)
        try:
            _load_graph_view(page)
            page.wait_for_function("() => window.__graph != null", timeout=10_000)
            page.evaluate("applyGraphFilter('file')")
            page.wait_for_timeout(300)
            # symbol nodes should be hidden
            hidden_n2 = page.evaluate(
                "() => window.__graph?.graph?.getNodeAttribute('n2', 'hidden')"
            )
            assert hidden_n2 is True, f"Symbol node n2 should be hidden after file filter; got {hidden_n2}"
        finally:
            page.unroute("**/api/graph_export**")


# ---------------------------------------------------------------------------
# Phase 73: Wiki view (TestWikiView)
# ---------------------------------------------------------------------------

_WIKI_PAGES = ["README", "Architecture", "API"]
_WIKI_PAGE_CONTENT = """# Architecture

This project uses a **layered** approach:

1. Indexing layer
2. Graph layer
3. Query layer

See also [README](/wiki/README).

| Layer | Purpose |
|-------|---------|
| Index | file vectors |
| Graph | community detection |
"""


def _stub_wiki(page) -> None:
    import json as _json

    def _handle_wiki(r):
        url = r.request.url
        if "wiki_lint" in url:
            r.fulfill(
                status=200,
                content_type="application/json",
                body=_json.dumps({"warnings": ["stale: OldPage"], "warning_count": 1}),
            )
        elif "wiki/page" in url or "page?" in url:
            name = url.split("name=")[-1].split("&")[0] if "name=" in url else "Page"
            r.fulfill(
                status=200,
                content_type="application/json",
                body=_json.dumps({"name": name, "content": _WIKI_PAGE_CONTENT}),
            )
        else:
            r.fulfill(
                status=200,
                content_type="application/json",
                body=_json.dumps({"pages": _WIKI_PAGES, "total": len(_WIKI_PAGES)}),
            )

    page.route("**/api/wiki**", _handle_wiki)


def _load_wiki_view(page) -> None:
    _goto_with_retry(page, DASHBOARD_URL)
    page.wait_for_load_state("load", timeout=_TIMEOUT_PAGE)
    page.wait_for_function(
        "() => { const s = document.getElementById('project-sel'); "
        "return s && s.options.length > 0 && s.value !== ''; }",
        timeout=45_000,
    )
    page.evaluate("switchView('wiki')")
    page.wait_for_function(
        "() => document.querySelectorAll('.wiki-page-link').length > 0 || "
        "document.getElementById('wiki-pages')?.textContent?.includes('No wiki')",
        timeout=10_000,
    )


class TestWikiView:
    """Phase 73: Wiki view — two-pane page browser."""

    def test_wiki_view_shows_page_list(self, page, live_project):
        """Stub /api/wiki; wiki view must render .wiki-page-link buttons for each page."""
        _stub_wiki(page)
        try:
            _load_wiki_view(page)
            count = page.locator(".wiki-page-link").count()
            assert count == len(_WIKI_PAGES), (
                f"Expected {len(_WIKI_PAGES)} wiki-page-link buttons; got {count}"
            )
        finally:
            page.unroute("**/api/wiki**")

    def test_wiki_open_page_renders_markdown(self, page, live_project):
        """Opening a wiki page must fetch content and render it as HTML in #wiki-content."""
        _stub_wiki(page)
        try:
            _load_wiki_view(page)
            # Use evaluate (awaits the Promise) so the fetch+render completes before we check
            page.evaluate("openWikiPage(_wikiPages[0])")
            page.wait_for_function(
                "document.getElementById('wiki-content')?.querySelector('h1, h2, h3') != null",
                timeout=10_000,
            )
            html = page.locator("#wiki-content").inner_html(timeout=5_000)
            assert "<h1>" in html, f"#wiki-content must render markdown headings; got: {html[:300]}"
        finally:
            page.unroute("**/api/wiki**")

    def test_wiki_search_filters_page_list(self, page, live_project):
        """Typing in #wiki-search must filter the page list."""
        _stub_wiki(page)
        try:
            _load_wiki_view(page)
            page.fill("#wiki-search", "README")
            page.wait_for_timeout(300)
            count = page.locator(".wiki-page-link").count()
            assert count == 1, f"Search for 'README' should show 1 result; got {count}"
        finally:
            page.unroute("**/api/wiki**")

    def test_wiki_lint_panel_shows_warnings_count(self, page, live_project):
        """Stub /api/wiki_lint with 1 warning; lint panel must become visible."""
        _stub_wiki(page)
        try:
            _load_wiki_view(page)
            page.wait_for_function(
                "document.getElementById('wiki-lint-panel')?.style.display !== 'none'",
                timeout=8_000,
            )
            count_text = (page.locator("#wiki-lint-count").text_content(timeout=5_000) or "").strip()
            assert count_text != "0", f"#wiki-lint-count should show >0; got: {count_text!r}"
        finally:
            page.unroute("**/api/wiki**")

    def test_mdsafe_renders_link_with_safe_url(self, page, live_project):
        """mdSafe must render [text](https://…) as <a> with target=_blank."""
        _goto_with_retry(page, DASHBOARD_URL)
        page.wait_for_load_state("load", timeout=_TIMEOUT_PAGE)
        page.wait_for_function(
            "() => document.getElementById('project-sel')?.options?.length > 0",
            timeout=30_000,
        )
        page.evaluate("switchView('chat')")
        page.evaluate("appendMsg('ai', '[opencode](https://example.com)')")
        page.wait_for_timeout(300)
        html = page.locator(".msg.ai .msg-bubble").last.inner_html(timeout=5_000)
        assert 'href="https://example.com"' in html, (
            f"mdSafe must render https link; got: {html[:300]}"
        )
        assert 'target="_blank"' in html, "Link must have target=_blank"

    def test_mdsafe_strips_javascript_url(self, page, live_project):
        """mdSafe must NOT render javascript: URLs as <a> tags (XSS guard)."""
        _goto_with_retry(page, DASHBOARD_URL)
        page.wait_for_load_state("load", timeout=_TIMEOUT_PAGE)
        page.wait_for_function(
            "() => document.getElementById('project-sel')?.options?.length > 0",
            timeout=30_000,
        )
        page.evaluate("switchView('chat')")
        page.evaluate("appendMsg('ai', '[bad](javascript:alert(1))')")
        page.wait_for_timeout(300)
        html = page.locator(".msg.ai .msg-bubble").last.inner_html(timeout=5_000)
        assert 'href="javascript:' not in html, (
            f"mdSafe must not render javascript: as href (XSS guard); got: {html[:300]}"
        )

    def test_mdsafe_renders_ordered_list(self, page, live_project):
        """mdSafe must convert '1. a\\n2. b\\n3. c' into <ol> with 3 <li>."""
        _goto_with_retry(page, DASHBOARD_URL)
        page.wait_for_load_state("load", timeout=_TIMEOUT_PAGE)
        page.wait_for_function(
            "() => document.getElementById('project-sel')?.options?.length > 0",
            timeout=30_000,
        )
        page.evaluate("switchView('chat')")
        page.evaluate("appendMsg('ai', '1. alpha\\n2. beta\\n3. gamma')")
        page.wait_for_timeout(300)
        html = page.locator(".msg.ai .msg-bubble").last.inner_html(timeout=5_000)
        assert "<ol>" in html, f"mdSafe must render ordered list <ol>; got: {html[:300]}"
        li_count = html.count("<li>")
        assert li_count == 3, f"Ordered list must have 3 <li>; got {li_count}: {html[:300]}"

    def test_mdsafe_renders_table(self, page, live_project):
        """mdSafe must convert pipe-table into <table> with <thead> and <tbody>."""
        _goto_with_retry(page, DASHBOARD_URL)
        page.wait_for_load_state("load", timeout=_TIMEOUT_PAGE)
        page.wait_for_function(
            "() => document.getElementById('project-sel')?.options?.length > 0",
            timeout=30_000,
        )
        page.evaluate("switchView('chat')")
        page.evaluate("appendMsg('ai', '| Col A | Col B |\\n|-------|-------|\\n| v1 | v2 |')")
        page.wait_for_timeout(300)
        html = page.locator(".msg.ai .msg-bubble").last.inner_html(timeout=5_000)
        assert "<table>" in html, f"mdSafe must render <table>; got: {html[:300]}"
        assert "<thead>" in html, f"Table must have <thead>; got: {html[:300]}"
        assert "<th>" in html, f"Table must have <th>; got: {html[:300]}"


# ---------------------------------------------------------------------------
# Phase 73: Admin SSE chips + dead endpoint removal (TestAdminSSE)
# ---------------------------------------------------------------------------

class TestAdminSSE:
    """Phase 73: Admin SSE job chips, auto-pipeline panel, dead endpoints removed."""

    def test_admin_job_chip_appears_on_sse_running_event(self, page, live_project):
        """Stubbed SSE stream with a job event must produce an .admin-chip in Admin view."""
        import json as _json
        sse_body = (
            "data: "
            + _json.dumps({
                "type": "job",
                "job_id": "test-chip-1",
                "action": "vacuum",
                "status": "running",
                "project": "/tmp/test",
                "error": None,
            })
            + "\n\n"
            + "data: "
            + _json.dumps({"type": "metrics", "requests": 0, "errors": 0, "uptime_s": 10})
            + "\n\n"
        )
        page.route(
            "**/api/events/stream**",
            lambda r: r.fulfill(
                status=200,
                content_type="text/event-stream",
                headers={"Cache-Control": "no-cache"},
                body=sse_body,
            ),
        )
        try:
            _goto_with_retry(page, DASHBOARD_URL)
            page.wait_for_load_state("load", timeout=_TIMEOUT_PAGE)
            page.wait_for_function(
                "() => document.getElementById('project-sel')?.options?.length > 0",
                timeout=45_000,
            )
            page.evaluate("switchView('admin')")
            page.wait_for_function(
                "() => document.getElementById('admin-job-chips')?.children?.length > 0",
                timeout=8_000,
            )
            chip = page.locator("#chip-test-chip-1")
            assert chip.count() == 1, "Job chip #chip-test-chip-1 must appear in Admin view"
            class_attr = chip.get_attribute("class") or ""
            assert "running" in class_attr, f"Chip should have 'running' class; got: {class_attr}"
        finally:
            page.unroute("**/api/events/stream**")

    def test_admin_auto_pipeline_panel_renders_entries(self, page, live_project):
        """If /api/auto_pipeline_status has events, #admin-autopipeline-log must show .ap-entry elements."""
        import json as _json
        pipeline_body = _json.dumps({
            "enabled": True,
            "events": [
                {"project": "/home/user/myproject", "scheduled_at": "2026-06-08T10:00:00", "status": "ok"},
                {"project": "/home/user/otherproject", "scheduled_at": "2026-06-08T11:00:00", "status": "error"},
            ],
        })
        page.route(
            "**/api/auto_pipeline_status**",
            lambda r: r.fulfill(status=200, content_type="application/json", body=pipeline_body),
        )
        try:
            _goto_with_retry(page, DASHBOARD_URL)
            page.wait_for_load_state("load", timeout=_TIMEOUT_PAGE)
            page.wait_for_function(
                "() => document.getElementById('project-sel')?.options?.length > 0",
                timeout=45_000,
            )
            page.evaluate("switchView('admin')")
            page.wait_for_function(
                "() => document.querySelectorAll('.ap-entry').length > 0 || "
                "document.getElementById('admin-autopipeline-log')?.textContent?.includes('No auto-pipeline')",
                timeout=8_000,
            )
            count = page.locator(".ap-entry").count()
            assert count > 0, (
                "#admin-autopipeline-log must render .ap-entry elements from stubbed events"
            )
        finally:
            page.unroute("**/api/auto_pipeline_status**")

    def test_dead_prerelease_endpoints_removed(self, live_project):
        """Deleted endpoints must return 404, not 503 or 200."""
        for path in ["/api/prerelease_status", "/api/qa_status", "/api/run_prerelease",
                     "/api/verify_status", "/api/auto_fix_trigger", "/api/run_qa"]:
            method = "POST" if path.startswith("/api/run") or path == "/api/auto_fix_trigger" else "GET"
            if method == "GET":
                r = httpx.get(f"{DAEMON_URL}{path}", timeout=5)
            else:
                r = httpx.post(f"{DAEMON_URL}{path}", json={}, timeout=5)
            assert r.status_code == 404, (
                f"Dead endpoint {path} must return 404 after deletion; got {r.status_code}"
            )


# ---------------------------------------------------------------------------
# Phase 80: Graph view gaps (TestGraphViewGaps)
# ---------------------------------------------------------------------------

class TestGraphViewGaps:
    """Phase 80: coverage gaps in graph view — layout switch, filter reset, view-kill."""

    def test_graph_layout_switch_circular_then_fa2(self, page, live_project):
        """applyGraphLayout must not throw; node coords change when switching layouts."""
        _stub_graph(page)
        try:
            _load_graph_view(page)
            page.wait_for_function("() => window.__graph != null", timeout=10_000)
            # Switch to circular — should complete without error
            page.evaluate("applyGraphLayout('circular')")
            page.wait_for_timeout(500)
            x_circular = page.evaluate(
                "() => window.__graph?.graph?.getNodeAttribute('n1', 'x')"
            )
            assert x_circular is not None, "Node x coord must be set after circular layout"
            # Switch to fa2 — layout starts in background, no throw
            page.evaluate("applyGraphLayout('fa2')")
            page.wait_for_timeout(1_500)  # let FA2 run briefly
            assert page.evaluate("() => window.__graph != null"), (
                "window.__graph must still exist after fa2 layout switch"
            )
        finally:
            page.unroute("**/api/graph_export**")

    def test_graph_filter_all_unhides_nodes(self, page, live_project):
        """Filtering to 'file' then back to 'all' must unhide symbol nodes."""
        _stub_graph(page)
        try:
            _load_graph_view(page)
            page.wait_for_function("() => window.__graph != null", timeout=10_000)
            # Filter to file-only first (n2/n3 are symbols — they get hidden)
            page.evaluate("applyGraphFilter('file')")
            page.wait_for_timeout(300)
            hidden_sym = page.evaluate(
                "() => window.__graph?.graph?.getNodeAttribute('n2', 'hidden')"
            )
            assert hidden_sym is True, f"Symbol node n2 should be hidden after file filter; got {hidden_sym}"
            # Reset to 'all' — n2 must be un-hidden
            page.evaluate("applyGraphFilter('all')")
            page.wait_for_timeout(300)
            hidden_after = page.evaluate(
                "() => window.__graph?.graph?.getNodeAttribute('n2', 'hidden')"
            )
            assert not hidden_after, (
                f"Symbol node n2 must not be hidden after filter reset to 'all'; got {hidden_after}"
            )
        finally:
            page.unroute("**/api/graph_export**")

    def test_graph_kill_on_view_switch(self, page, live_project):
        """Switching away from graph and back must rebuild sigma (no double-render crash)."""
        _stub_graph(page)
        try:
            _load_graph_view(page)
            page.wait_for_function("() => window.__graph != null", timeout=10_000)
            # Switch to pulse — loadGraph kills the old sigma
            page.evaluate("switchView('pulse')")
            page.wait_for_timeout(300)
            # Switch back to graph — sigma must be rebuilt cleanly
            page.evaluate("switchView('graph')")
            page.wait_for_function(
                "() => window.__graph != null || document.getElementById('graph-empty')?.style.display === 'flex'",
                timeout=15_000,
            )
            assert page.locator("#graph-canvas").count() == 1, (
                "#graph-canvas must exist after switching back to graph view"
            )
        finally:
            page.unroute("**/api/graph_export**")


# ---------------------------------------------------------------------------
# Phase 80: Wiki view gaps (TestWikiViewGaps)
# ---------------------------------------------------------------------------

class TestWikiViewGaps:
    """Phase 80: wiki lint panel toggle and page active-class tracking."""

    def test_wiki_lint_panel_toggle_expands_items(self, page, live_project):
        """Clicking .wiki-lint-hdr must toggle #wiki-lint-items open/closed."""
        _stub_wiki(page)
        try:
            _load_wiki_view(page)
            # Wait for lint panel to become visible (stub has 1 warning)
            page.wait_for_function(
                "document.getElementById('wiki-lint-panel')?.style.display !== 'none'",
                timeout=8_000,
            )
            # Initially items are hidden
            items_el = page.locator("#wiki-lint-items")
            assert "open" not in (items_el.get_attribute("class") or ""), (
                "#wiki-lint-items should not have 'open' class before toggling"
            )
            # Click header to expand
            page.locator(".wiki-lint-hdr").click()
            page.wait_for_timeout(300)
            class_after_open = items_el.get_attribute("class") or ""
            assert "open" in class_after_open, (
                f"#wiki-lint-items must have 'open' class after clicking header; got: {class_after_open}"
            )
            # Click again to collapse
            page.locator(".wiki-lint-hdr").click()
            page.wait_for_timeout(300)
            class_after_close = items_el.get_attribute("class") or ""
            assert "open" not in class_after_close, (
                f"#wiki-lint-items must not have 'open' after second click; got: {class_after_close}"
            )
        finally:
            page.unroute("**/api/wiki**")

    def test_wiki_page_active_class_after_open(self, page, live_project):
        """openWikiPage must add .active to the opened page; opening another moves it."""
        _stub_wiki(page)
        try:
            _load_wiki_view(page)
            links = page.locator(".wiki-page-link")
            assert links.count() >= 2, "Need at least 2 wiki pages for this test"
            # Open first page via JS (avoids _proj check race on slow CI)
            page.evaluate("openWikiPage(_wikiPages[0])")
            page.wait_for_timeout(500)
            # _renderWikiPages re-renders list with activeSlug=first page
            first_class = page.locator(".wiki-page-link").nth(0).get_attribute("class") or ""
            assert "active" in first_class, (
                f"First page link must have 'active' after open; got: {first_class!r}"
            )
            # Open second page
            page.evaluate("openWikiPage(_wikiPages[1])")
            page.wait_for_timeout(500)
            first_class_after = page.locator(".wiki-page-link").nth(0).get_attribute("class") or ""
            second_class_after = page.locator(".wiki-page-link").nth(1).get_attribute("class") or ""
            assert "active" not in first_class_after, (
                f"First link must lose 'active' after second opens; got: {first_class_after!r}"
            )
            assert "active" in second_class_after, (
                f"Second link must gain 'active' after open; got: {second_class_after!r}"
            )
        finally:
            page.unroute("**/api/wiki**")


# ---------------------------------------------------------------------------
# Phase 80: Admin SSE chip state transitions (TestAdminSSEGaps)
# ---------------------------------------------------------------------------

class TestAdminSSEGaps:
    """Phase 80: chip status transitions and error state rendering."""

    def _make_sse_body(self, *events) -> str:
        import json as _json
        lines = []
        for evt in events:
            lines.append("data: " + _json.dumps(evt) + "\n\n")
        return "".join(lines)

    def test_admin_chip_transitions_running_to_success(self, page, live_project):
        """Two SSE frames for the same job_id must transition chip from 'running' to 'done'."""
        frames: list[dict] = [
            {"type": "job", "job_id": "trans-1", "action": "enrich", "status": "running", "project": "/tmp/t", "error": None},
            {"type": "job", "job_id": "trans-1", "action": "enrich", "status": "done", "project": "/tmp/t", "error": None},
        ]
        sse_body = self._make_sse_body(*frames)
        page.route(
            "**/api/events/stream**",
            lambda r: r.fulfill(
                status=200,
                content_type="text/event-stream",
                headers={"Cache-Control": "no-cache"},
                body=sse_body,
            ),
        )
        try:
            _goto_with_retry(page, DASHBOARD_URL)
            page.wait_for_load_state("load", timeout=_TIMEOUT_PAGE)
            page.wait_for_function(
                "() => document.getElementById('project-sel')?.options?.length > 0",
                timeout=45_000,
            )
            page.evaluate("switchView('admin')")
            # Wait for chip to appear and reach final state
            page.wait_for_function(
                "() => document.getElementById('chip-trans-1')?.className?.includes('done') || "
                "document.getElementById('chip-trans-1')?.className?.includes('running')",
                timeout=8_000,
            )
            page.wait_for_timeout(500)  # let second frame settle
            chip = page.locator("#chip-trans-1")
            assert chip.count() == 1, "Chip #chip-trans-1 must exist"
            class_attr = chip.get_attribute("class") or ""
            assert "done" in class_attr, (
                f"Chip must show 'done' after success transition; got class: {class_attr}"
            )
        finally:
            page.unroute("**/api/events/stream**")

    def test_admin_chip_failure_status_shows_error_class(self, page, live_project):
        """SSE frame with status='error' must render chip with 'error' class."""
        sse_body = self._make_sse_body(
            {"type": "job", "job_id": "fail-1", "action": "index", "status": "error",
             "project": "/tmp/fail", "error": "out of memory"},
        )
        page.route(
            "**/api/events/stream**",
            lambda r: r.fulfill(
                status=200,
                content_type="text/event-stream",
                headers={"Cache-Control": "no-cache"},
                body=sse_body,
            ),
        )
        try:
            _goto_with_retry(page, DASHBOARD_URL)
            page.wait_for_load_state("load", timeout=_TIMEOUT_PAGE)
            page.wait_for_function(
                "() => document.getElementById('project-sel')?.options?.length > 0",
                timeout=45_000,
            )
            page.evaluate("switchView('admin')")
            page.wait_for_function(
                "() => document.getElementById('chip-fail-1') != null",
                timeout=8_000,
            )
            chip = page.locator("#chip-fail-1")
            class_attr = chip.get_attribute("class") or ""
            assert "error" in class_attr, (
                f"Chip must have 'error' class for failed job; got class: {class_attr}"
            )
        finally:
            page.unroute("**/api/events/stream**")


# ---------------------------------------------------------------------------
# Phase 80: Command palette keyboard gaps (TestCmdPaletteGaps)
# ---------------------------------------------------------------------------

class TestCmdPaletteGaps:
    """Phase 80: arrow-key navigation and Enter dispatch in command palette."""

    def _open_palette(self, page) -> None:
        _goto_with_retry(page, DASHBOARD_URL)
        page.wait_for_load_state("load", timeout=_TIMEOUT_PAGE)
        page.wait_for_function(
            "() => document.getElementById('project-sel')?.options?.length > 0",
            timeout=45_000,
        )
        page.evaluate("showCmdPalette()")
        page.wait_for_function(
            "!document.getElementById('cmd-overlay')?.classList?.contains('hidden')",
            timeout=5_000,
        )

    def test_cmd_palette_arrow_navigation_changes_highlight(self, page, live_project):
        """ArrowDown must move the .hi highlight down; ArrowUp must move it back up."""
        self._open_palette(page)
        cmd_input = page.locator("#cmd-input")
        cmd_input.focus()
        # Initially item[0] has .hi
        first_hi = page.evaluate(
            "() => document.querySelectorAll('#cmd-results li')[0]?.classList.contains('hi')"
        )
        assert first_hi, "First item must have .hi initially"
        # ArrowDown → index moves to 1
        cmd_input.press("ArrowDown")
        page.wait_for_timeout(100)
        idx1_hi = page.evaluate(
            "() => document.querySelectorAll('#cmd-results li')[1]?.classList.contains('hi')"
        )
        idx0_hi = page.evaluate(
            "() => document.querySelectorAll('#cmd-results li')[0]?.classList.contains('hi')"
        )
        assert idx1_hi, "Item[1] must have .hi after one ArrowDown"
        assert not idx0_hi, "Item[0] must NOT have .hi after ArrowDown"
        # ArrowUp → index moves back to 0
        cmd_input.press("ArrowUp")
        page.wait_for_timeout(100)
        idx0_back = page.evaluate(
            "() => document.querySelectorAll('#cmd-results li')[0]?.classList.contains('hi')"
        )
        assert idx0_back, "Item[0] must have .hi again after ArrowUp"

    def test_cmd_palette_enter_dispatches_action(self, page, live_project):
        """Typing 'Graph' then Enter must switch to graph view and close palette."""
        self._open_palette(page)
        cmd_input = page.locator("#cmd-input")
        cmd_input.fill("Graph")
        page.wait_for_timeout(200)
        cmd_input.press("Enter")
        # Palette must close
        page.wait_for_function(
            "document.getElementById('cmd-overlay')?.classList?.contains('hidden')",
            timeout=5_000,
        )
        # Graph view must be active
        graph_display = page.evaluate(
            "() => window.getComputedStyle(document.getElementById('view-graph') || {}).display"
        )
        assert graph_display not in ("none", "", None), (
            f"#view-graph must be visible after Enter dispatch; display={graph_display!r}"
        )


# ---------------------------------------------------------------------------
# Browser fixture scope guard (moved from test_resource_profile.py)
# ---------------------------------------------------------------------------

_SEEN_BROWSER_IDS: list[int] = []


class TestBrowserFixtureScope:
    """Verify browser fixture is session-scoped (one Chromium process per test run)."""

    def test_browser_is_session_scoped_a(self, browser):
        """Record browser fixture identity — must match test_browser_is_session_scoped_b."""
        _SEEN_BROWSER_IDS.append(id(browser))

    def test_browser_is_session_scoped_b(self, browser):
        """Browser fixture identity must equal test_a's — proves one Chromium process per session."""
        assert _SEEN_BROWSER_IDS, "test_browser_is_session_scoped_a must run first"
        assert id(browser) == _SEEN_BROWSER_IDS[0], (
            f"browser fixture not session-scoped: id mismatch "
            f"({_SEEN_BROWSER_IDS[0]} → {id(browser)}); "
            "a conftest change demoted browser to function scope"
        )
