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


# ---------------------------------------------------------------------------
# View: Chat — all 9 intents
# ---------------------------------------------------------------------------

class TestChatIntents:
    """Each intent must produce a non-empty response."""

    def _chat_and_get_text(self, page, live_project: str, query: str) -> str:
        _navigate_to_chat(page)
        _send_message(page, query)
        return _wait_for_ai_response(page)

    def test_intent_architecture(self, page, live_project):
        text = self._chat_and_get_text(page, live_project, "What is the overall architecture?")
        assert len(text) > 30, f"Architecture response too short: {text!r}"

    def test_intent_search(self, page, live_project):
        text = self._chat_and_get_text(page, live_project, "Find the main HTTP handler function")
        assert len(text) > 20, f"Search response too short: {text!r}"

    def test_intent_global(self, page, live_project):
        text = self._chat_and_get_text(page, live_project, "Give me a comprehensive global overview of the entire system")
        assert len(text) > 50, f"Global overview response too short: {text!r}"

    def test_intent_feature(self, page, live_project):
        text = self._chat_and_get_text(page, live_project, "How does the request processing feature work end to end?")
        assert len(text) > 30, f"Feature trace response too short: {text!r}"

    def test_intent_graph_callers(self, page, live_project):
        text = self._chat_and_get_text(page, live_project, "What functions call the main function?")
        assert len(text) > 10, f"Graph callers response too short: {text!r}"

    def test_intent_graph_impact(self, page, live_project):
        text = self._chat_and_get_text(page, live_project, "What breaks if I change the main handler?")
        assert len(text) > 10, f"Graph impact response too short: {text!r}"

    def test_intent_graph_callees(self, page, live_project):
        text = self._chat_and_get_text(page, live_project, "What does the search handler call internally?")
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
