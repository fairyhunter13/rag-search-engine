"""Playwright browser E2E tests — dashboard UI.

Proves that JavaScript executes, DOM updates on interactions,
layout renders without errors, and UX is simple and operable.

Strategy:
  - Inject the dashboard HTML directly with page.set_content()
  - Mock all fetch() calls via page.route() with realistic data
  - Use Playwright's SYNCHRONOUS API (pytest-playwright default)
  - Screenshots saved to /tmp/dashboard-playwright-screenshots/

Run:
    pytest tests/e2e/test_dashboard_playwright.py -v --browser chromium
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.playwright  # skip unless -m playwright is passed

# ── Mock data ──────────────────────────────────────────────────────────────────

_MOCK_PROJECT = "/tmp/playwright-test-project"

_MOCK_PROJECTS = {"projects": [
    {"path": _MOCK_PROJECT, "chunks": 42156, "watching": True},
]}
_MOCK_OVERVIEW = {
    "total_files": 42156, "file_count": 42156,
    "language_breakdown": {"Python": 800, "TypeScript": 400},
}
_MOCK_KB_HEALTH = {
    "total_communities": 247, "enriched_communities": 231,
    "enrichment_pct": 93.5, "wiki_page_count": 18,
    "auto_pipeline_enabled": True,
    "last_pipeline_event": {"action": "pipeline_complete", "ts": "2026-06-04T10:15:00"},
}
_MOCK_METRICS = {
    "uptime_s": 7240, "connected_clients": 2,
    "total_requests": 5420, "errors": 3, "active_watchers": 1,
}
_MOCK_SUGGESTED = {"questions": [
    "how does the indexing pipeline work?",
    "what calls the graph extractor?",
    "explain community detection",
]}
_MOCK_CHAT = {
    "answer": "The indexing pipeline starts with tree-sitter parsing across all source files. Each file is split into semantic chunks, embedded on GPU, and stored in LanceDB.",
    "intent": "feature",
    "sources": [
        f"{_MOCK_PROJECT}/src/handlers/_pipeline.py",
        f"{_MOCK_PROJECT}/src/graph/extractor.py",
    ],
    "elapsed_ms": 312,
    "model": "qwen3-query:8b",
}
_MOCK_EMPTY = {"alerts": [], "enabled": False, "events": [],
               "message": "ok", "result": "ok"}

_SCREENSHOT_DIR = Path("/tmp/dashboard-playwright-screenshots")


# ── Fixtures ───────────────────────────────────────────────────────────────────

def _j(data: dict) -> dict:
    return {
        "status": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(data),
    }


@pytest.fixture(scope="session")
def _dashboard_server():
    """Start a session-scoped HTTP server serving the dashboard + mock APIs."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from opencode_search._dashboard_html import _DASHBOARD_HTML

    html = _DASHBOARD_HTML.encode()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a): pass

        def _send_json(self, data):
            body = json.dumps(data).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            p = self.path.split("?")[0]
            if p in ("/", "/dashboard"):
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(html)))
                self.end_headers()
                self.wfile.write(html)
            elif p == "/api/projects":                 self._send_json(_MOCK_PROJECTS)
            elif p.startswith("/api/overview"):        self._send_json(_MOCK_OVERVIEW)
            elif p.startswith("/api/kb_health"):       self._send_json(_MOCK_KB_HEALTH)
            elif p.startswith("/api/metrics"):         self._send_json(_MOCK_METRICS)
            elif p.startswith("/api/suggested_questions"): self._send_json(_MOCK_SUGGESTED)
            elif p.startswith("/api/alerts") or p.startswith("/api/auto_pipeline"):          self._send_json(_MOCK_EMPTY)
            elif p.startswith("/api/system_status"):   self._send_json({"status": "ok"})
            else:                                      self._send_json(_MOCK_EMPTY)

        def _send_sse_stream(self, answer: str, meta: dict):
            """Simulate SSE streaming chat response (text/event-stream)."""
            events = []
            chunk = 40
            for i in range(0, max(len(answer), 1), chunk):
                events.append("data: " + json.dumps({"type": "token", "text": answer[i:i + chunk]}) + "\n\n")
            events.append("data: " + json.dumps({
                "type": "done",
                "intent": meta.get("intent", "feature"),
                "sources": meta.get("sources", []),
                "elapsed_ms": meta.get("elapsed_ms", 312),
                "model": meta.get("model", "qwen3-query:8b"),
            }) + "\n\n")
            body = "".join(events).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            p = self.path.split("?")[0]
            if p == "/api/chat":                       self._send_json(_MOCK_CHAT)
            elif p == "/api/chat_stream":
                self._send_sse_stream(
                    _MOCK_CHAT["answer"],
                    {"intent": _MOCK_CHAT["intent"], "sources": _MOCK_CHAT["sources"],
                     "elapsed_ms": _MOCK_CHAT["elapsed_ms"], "model": _MOCK_CHAT["model"]},
                )
            elif p.startswith("/api/vacuum"):          self._send_json({"message": "Vacuum complete. 123 MB freed."})
            elif p.startswith("/api/dedup"):           self._send_json({"message": "Dedup complete. 12 duplicates removed."})
            elif p.startswith("/api/build_hierarchy") or p.startswith("/api/enrich_hierarchy"): self._send_json({"message": "Job submitted"})
            else:                                      self._send_json(_MOCK_EMPTY)

    # allow_reuse_address prevents "address already in use" between runs
    HTTPServer.allow_reuse_address = True
    server = HTTPServer(("127.0.0.1", 19765), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield "http://127.0.0.1:19765"
    server.shutdown()


@pytest.fixture
def pw_page(page, _dashboard_server):
    """Navigate to the local dashboard server and wait for KPI tiles to load."""
    page.goto(_dashboard_server + "/", wait_until="domcontentloaded")
    page.wait_for_function(
        "document.getElementById('kpi-files').textContent !== '—'",
        timeout=8000,
    )
    yield page


# ── 1. Initial render ──────────────────────────────────────────────────────────

class TestInitialRender:
    def test_page_loaded_without_crash(self, pw_page):
        # If pw_page fixture completed, the JS boot() ran without fatal errors.
        # Verify the DOM is populated — proves boot() succeeded end-to-end.
        title = pw_page.title()
        body_text = pw_page.evaluate("document.body.innerText")
        assert "opencode" in body_text.lower() or title, \
            "Page must have opencode content after load"

    def test_top_navbar_visible(self, pw_page):
        topnav = pw_page.query_selector(".topnav")
        assert topnav is not None, ".topnav element not found"
        assert topnav.is_visible(), ".topnav must be visible"

    def test_three_view_buttons_visible(self, pw_page):
        for vid in ("vbtn-pulse", "vbtn-chat", "vbtn-admin"):
            btn = pw_page.query_selector(f"#{vid}")
            assert btn is not None, f"#{vid} not found"
            assert btn.is_visible(), f"#{vid} must be visible"

    def test_pulse_view_active_by_default(self, pw_page):
        pulse = pw_page.query_selector("#view-pulse")
        assert "active" in (pulse.get_attribute("class") or ""), \
            "Pulse view must be active on load"

    def test_no_old_sidebar(self, pw_page):
        assert pw_page.query_selector(".sidebar") is None, \
            "Old sidebar must not exist in 3-view design"

    def test_brand_name_visible(self, pw_page):
        brand = pw_page.query_selector(".brand")
        assert brand is not None
        assert "opencode" in (brand.text_content() or "").lower()

    def test_bento_grid_present(self, pw_page):
        bento = pw_page.query_selector(".bento")
        assert bento is not None, "Bento KPI grid must be present"
        assert bento.is_visible()


# ── 2. KPI tiles ──────────────────────────────────────────────────────────────

class TestKpiBentoTiles:
    def test_files_tile_shows_value(self, pw_page):
        text = pw_page.query_selector("#kpi-files").text_content()
        assert text not in ("—", "", "loading…"), f"Files tile must show data, got: {text!r}"

    def test_communities_tile_shows_value(self, pw_page):
        text = pw_page.query_selector("#kpi-communities").text_content()
        assert text not in ("—", ""), f"Communities tile empty: {text!r}"

    def test_enrichment_tile_shows_percentage(self, pw_page):
        text = pw_page.query_selector("#kpi-enrichment").text_content()
        assert text not in ("—", ""), f"Enrichment tile empty: {text!r}"
        assert "%" in text or text[0].isdigit(), f"Enrichment should show %, got: {text!r}"

    def test_wiki_tile_shows_value(self, pw_page):
        text = pw_page.query_selector("#kpi-wiki").text_content()
        assert text not in ("—", ""), f"Wiki tile empty: {text!r}"

    def test_uptime_tile_shows_time(self, pw_page):
        text = pw_page.query_selector("#kpi-uptime").text_content()
        assert text not in ("—", ""), f"Uptime tile empty: {text!r}"
        assert any(u in text for u in ("s", "m", "h", "d")), \
            f"Uptime must have time unit, got: {text!r}"

    def test_all_six_tiles_present(self, pw_page):
        for tid in ("tile-files", "tile-communities", "tile-enrichment",
                    "tile-wiki", "tile-requests", "tile-uptime"):
            tile = pw_page.query_selector(f"#{tid}")
            assert tile is not None, f"Tile #{tid} not found"

    def test_tiles_have_status_classes(self, pw_page):
        tile = pw_page.query_selector("#tile-enrichment")
        classes = tile.get_attribute("class") or ""
        assert any(c in classes for c in ("ok", "warn", "err")), \
            f"Enrichment tile must have status class, got: {classes!r}"

    def test_suggested_questions_populated(self, pw_page):
        btns = pw_page.query_selector_all("#suggested-list .sq-btn")
        assert len(btns) >= 1, "Suggested questions must render"

    def test_activity_feed_has_content(self, pw_page):
        act = pw_page.query_selector("#activity-list")
        assert (act.text_content() or "").strip() != "", \
            "Activity list must not be empty after load"

    def test_daemon_dot_is_ok(self, pw_page):
        dot = pw_page.query_selector("#daemon-dot")
        classes = dot.get_attribute("class") or ""
        assert "ok" in classes, f"Status dot must be ok after successful load: {classes!r}"


# ── 3. View switching ─────────────────────────────────────────────────────────

class TestViewSwitching:
    def test_click_chat_activates_chat(self, pw_page):
        pw_page.click("#vbtn-chat")
        pw_page.wait_for_timeout(100)
        chat = pw_page.query_selector("#view-chat")
        assert "active" in (chat.get_attribute("class") or "")

    def test_click_admin_activates_admin(self, pw_page):
        pw_page.click("#vbtn-admin")
        pw_page.wait_for_timeout(100)
        admin = pw_page.query_selector("#view-admin")
        assert "active" in (admin.get_attribute("class") or "")

    def test_only_one_view_active(self, pw_page):
        pw_page.click("#vbtn-chat")
        pw_page.wait_for_timeout(100)
        active = pw_page.query_selector_all(".view.active")
        assert len(active) == 1, f"Exactly one view must be active, found {len(active)}"

    def test_click_pulse_returns(self, pw_page):
        pw_page.click("#vbtn-chat")
        pw_page.click("#vbtn-pulse")
        pw_page.wait_for_timeout(100)
        assert "active" in (pw_page.query_selector("#view-pulse").get_attribute("class") or "")

    def test_active_vbtn_gets_active_class(self, pw_page):
        pw_page.click("#vbtn-admin")
        assert "active" in (pw_page.query_selector("#vbtn-admin").get_attribute("class") or "")

    def test_inactive_vbtn_loses_active_class(self, pw_page):
        pw_page.click("#vbtn-chat")
        classes = pw_page.query_selector("#vbtn-pulse").get_attribute("class") or ""
        assert "active" not in classes, \
            "Pulse vbtn must not have active class when Chat is active"


# ── 4. Chat interaction ───────────────────────────────────────────────────────

class TestChatInteraction:
    def _go_chat(self, p):
        p.click("#vbtn-chat")
        p.wait_for_selector("#chat-in", state="visible")

    def test_chat_input_visible(self, pw_page):
        self._go_chat(pw_page)
        assert pw_page.query_selector("#chat-in").is_visible()

    def test_send_button_visible(self, pw_page):
        self._go_chat(pw_page)
        assert pw_page.query_selector("#send-btn").is_visible()

    def test_typing_and_clicking_send_shows_user_bubble(self, pw_page):
        self._go_chat(pw_page)
        pw_page.fill("#chat-in", "how does the indexing pipeline work?")
        pw_page.click("#send-btn")
        # Wait for the full round-trip (AI response confirms user bubble was created)
        pw_page.wait_for_selector(".msg.ai:not(.thinking)", timeout=6000)
        assert len(pw_page.query_selector_all(".msg.user")) >= 1, \
            "User bubble must exist alongside AI response"

    def test_ai_response_appears_as_prose(self, pw_page):
        self._go_chat(pw_page)
        pw_page.fill("#chat-in", "explain the graph extraction")
        pw_page.click("#send-btn")
        pw_page.wait_for_selector(".msg.ai:not(.thinking)", timeout=5000)
        msgs = pw_page.query_selector_all(".msg.ai:not(.thinking)")
        assert len(msgs) >= 1, "AI response must appear"
        text = msgs[0].text_content() or ""
        assert len(text.strip()) > 20, f"Response must be prose, got: {text!r}"

    def test_ai_response_has_intent_badge(self, pw_page):
        self._go_chat(pw_page)
        pw_page.fill("#chat-in", "how does indexing work?")
        pw_page.click("#send-btn")
        pw_page.wait_for_selector(".intent-tag", timeout=5000)
        assert pw_page.query_selector(".intent-tag") is not None

    def test_ai_response_has_source_chips(self, pw_page):
        self._go_chat(pw_page)
        pw_page.fill("#chat-in", "what are the entry points?")
        pw_page.click("#send-btn")
        pw_page.wait_for_selector(".src-chip", timeout=5000)
        assert len(pw_page.query_selector_all(".src-chip")) >= 1

    def test_enter_key_sends_message(self, pw_page):
        self._go_chat(pw_page)
        pw_page.fill("#chat-in", "what calls the pipeline?")
        pw_page.press("#chat-in", "Enter")
        # Wait for the AI response — proves the Enter triggered sendChat()
        pw_page.wait_for_selector(".msg.ai:not(.thinking)", timeout=6000)
        assert len(pw_page.query_selector_all(".msg.ai")) >= 1

    def test_shift_enter_does_not_send(self, pw_page):
        self._go_chat(pw_page)
        pw_page.fill("#chat-in", "first line")
        pw_page.press("#chat-in", "Shift+Enter")
        pw_page.wait_for_timeout(200)
        assert len(pw_page.query_selector_all(".msg.user")) == 0, \
            "Shift+Enter must not send message"

    def test_input_clears_after_send(self, pw_page):
        self._go_chat(pw_page)
        pw_page.fill("#chat-in", "test question")
        pw_page.click("#send-btn")
        pw_page.wait_for_selector(".msg.user", timeout=3000)
        value = pw_page.input_value("#chat-in")
        assert value == "", f"Input must clear after send, got: {value!r}"

    def test_clicking_suggested_question_navigates_to_chat(self, pw_page):
        # Pulse is already active after pw_page boot — don't re-click (avoids re-render)
        pw_page.wait_for_selector(".sq-btn", timeout=3000)
        pw_page.locator(".sq-btn").first.click()
        pw_page.wait_for_selector("#view-chat.active", timeout=3000)
        pw_page.wait_for_selector(".msg.ai:not(.thinking)", timeout=6000)
        assert len(pw_page.query_selector_all(".msg.ai")) >= 1


# ── 5. Admin view ─────────────────────────────────────────────────────────────

class TestAdminView:
    def _go_admin(self, p):
        p.click("#vbtn-admin")
        p.wait_for_selector("#view-admin.active", timeout=2000)

    def test_projects_table_renders(self, pw_page):
        self._go_admin(pw_page)
        table = pw_page.query_selector(".projects-table")
        assert table is not None and table.is_visible()

    def test_project_row_appears(self, pw_page):
        self._go_admin(pw_page)
        pw_page.wait_for_selector("#projects-body tr", timeout=3000)
        rows = pw_page.query_selector_all("#projects-body tr")
        assert len(rows) >= 1, "Project row must appear"

    def test_ops_buttons_present(self, pw_page):
        self._go_admin(pw_page)
        btns = pw_page.query_selector_all(".op-btn")
        labels = [b.text_content() or "" for b in btns]
        for expected in ("Vacuum", "Dedup", "Re-index"):
            assert any(expected in lbl for lbl in labels), \
                f"Op button '{expected}' not found in: {labels}"

    def test_vacuum_logs_result(self, pw_page):
        self._go_admin(pw_page)
        for btn in pw_page.query_selector_all(".op-btn"):
            if "Vacuum" in (btn.text_content() or ""):
                btn.click()
                break
        pw_page.wait_for_timeout(400)
        log_text = pw_page.query_selector("#op-log").text_content() or ""
        assert log_text.strip() != "", "Op log must show result"

    def test_dedup_logs_result(self, pw_page):
        self._go_admin(pw_page)
        for btn in pw_page.query_selector_all(".op-btn"):
            if "Dedup" in (btn.text_content() or ""):
                btn.click()
                break
        pw_page.wait_for_timeout(400)
        log_text = pw_page.query_selector("#op-log").text_content() or ""
        assert log_text.strip() != ""


# ── 6. Command palette ────────────────────────────────────────────────────────

class TestCommandPalette:
    def test_palette_hidden_on_load(self, pw_page):
        classes = pw_page.query_selector("#cmd-overlay").get_attribute("class") or ""
        assert "hidden" in classes, "Palette must be hidden initially"

    def test_ctrl_k_opens_palette(self, pw_page):
        pw_page.keyboard.press("Control+k")
        pw_page.wait_for_timeout(200)
        classes = pw_page.query_selector("#cmd-overlay").get_attribute("class") or ""
        assert "hidden" not in classes, "Ctrl+K must open palette"

    def test_palette_shows_items(self, pw_page):
        pw_page.keyboard.press("Control+k")
        pw_page.wait_for_timeout(150)
        items = pw_page.query_selector_all("#cmd-results li")
        assert len(items) >= 3, f"Palette must show items, found {len(items)}"

    def test_typing_filters_results(self, pw_page):
        pw_page.keyboard.press("Control+k")
        pw_page.wait_for_timeout(100)
        pw_page.fill("#cmd-input", "vacuum")
        pw_page.wait_for_timeout(150)
        items = pw_page.query_selector_all("#cmd-results li")
        texts = [i.text_content() or "" for i in items]
        assert any("vacuum" in t.lower() for t in texts), \
            f"Filtering must show vacuum item, got: {texts}"

    def test_escape_closes_palette(self, pw_page):
        pw_page.keyboard.press("Control+k")
        pw_page.wait_for_timeout(100)
        pw_page.keyboard.press("Escape")
        pw_page.wait_for_timeout(150)
        classes = pw_page.query_selector("#cmd-overlay").get_attribute("class") or ""
        assert "hidden" in classes, "Escape must close palette"

    def test_backdrop_click_closes_palette(self, pw_page):
        pw_page.keyboard.press("Control+k")
        pw_page.wait_for_timeout(100)
        pw_page.click("#cmd-overlay", position={"x": 5, "y": 5})
        pw_page.wait_for_timeout(150)
        classes = pw_page.query_selector("#cmd-overlay").get_attribute("class") or ""
        assert "hidden" in classes, "Clicking backdrop must close palette"

    def test_arrow_down_highlights_item(self, pw_page):
        pw_page.keyboard.press("Control+k")
        pw_page.wait_for_timeout(100)
        pw_page.keyboard.press("ArrowDown")
        pw_page.wait_for_timeout(50)
        highlighted = pw_page.query_selector_all("#cmd-results li.hi")
        assert len(highlighted) == 1, "Arrow key must highlight exactly one item"


# ── 7. Project selector ───────────────────────────────────────────────────────

class TestProjectSelector:
    def test_selector_populated(self, pw_page):
        sel = pw_page.query_selector("#project-sel")
        opts = sel.query_selector_all("option")
        assert len(opts) >= 1, "Project selector must have options"

    def test_selected_value_is_project_path(self, pw_page):
        val = pw_page.input_value("#project-sel")
        assert val == _MOCK_PROJECT, f"Selected value must be mock project path, got: {val!r}"


# ── 8. Theme toggle ───────────────────────────────────────────────────────────

class TestThemeToggle:
    def test_theme_btn_visible(self, pw_page):
        btn = pw_page.query_selector("#theme-btn")
        assert btn is not None and btn.is_visible()

    def test_click_changes_background_color(self, pw_page):
        dark_bg = pw_page.evaluate(
            "getComputedStyle(document.documentElement).getPropertyValue('--bg').trim()"
        )
        pw_page.click("#theme-btn")
        pw_page.wait_for_timeout(100)
        light_bg = pw_page.evaluate(
            "getComputedStyle(document.documentElement).getPropertyValue('--bg').trim()"
        )
        assert dark_bg != light_bg, \
            f"Theme toggle must change --bg: {dark_bg!r} → {light_bg!r}"


# ── 9. Visual screenshots ─────────────────────────────────────────────────────

class TestVisualScreenshots:
    """Take screenshots at key moments — saved to /tmp for review."""

    def setup_method(self):
        _SCREENSHOT_DIR.mkdir(exist_ok=True)

    def test_pulse_view_screenshot(self, pw_page):
        pw_page.set_viewport_size({"width": 1440, "height": 900})
        pw_page.wait_for_timeout(300)
        path = str(_SCREENSHOT_DIR / "01-pulse.png")
        pw_page.screenshot(path=path, full_page=False)
        fsize = Path(path).stat().st_size
        assert fsize > 5000, f"Screenshot must be non-trivial, got {fsize} bytes"

    def test_chat_with_response_screenshot(self, pw_page):
        pw_page.set_viewport_size({"width": 1440, "height": 900})
        pw_page.click("#vbtn-chat")
        pw_page.fill("#chat-in", "how does the indexing pipeline work?")
        pw_page.click("#send-btn")
        pw_page.wait_for_selector(".msg.ai:not(.thinking)", timeout=5000)
        path = str(_SCREENSHOT_DIR / "02-chat-response.png")
        pw_page.screenshot(path=path)
        assert Path(path).exists()

    def test_admin_view_screenshot(self, pw_page):
        pw_page.set_viewport_size({"width": 1440, "height": 900})
        pw_page.click("#vbtn-admin")
        pw_page.wait_for_timeout(300)
        path = str(_SCREENSHOT_DIR / "03-admin.png")
        pw_page.screenshot(path=path)
        assert Path(path).exists()

    def test_command_palette_screenshot(self, pw_page):
        pw_page.set_viewport_size({"width": 1440, "height": 900})
        pw_page.keyboard.press("Control+k")
        pw_page.wait_for_timeout(200)
        path = str(_SCREENSHOT_DIR / "04-cmd-palette.png")
        pw_page.screenshot(path=path)
        assert Path(path).exists()

    def test_mobile_viewport_screenshot(self, pw_page):
        pw_page.set_viewport_size({"width": 390, "height": 844})
        pw_page.wait_for_timeout(300)
        path = str(_SCREENSHOT_DIR / "05-mobile-390.png")
        pw_page.screenshot(path=path)
        assert Path(path).exists()

    def test_tablet_viewport_screenshot(self, pw_page):
        pw_page.set_viewport_size({"width": 768, "height": 1024})
        pw_page.wait_for_timeout(300)
        path = str(_SCREENSHOT_DIR / "06-tablet-768.png")
        pw_page.screenshot(path=path)
        assert Path(path).exists()


# ── 9. Visual quality ─────────────────────────────────────────────────────────

class TestVisualQuality:
    """CSS design tokens, layout, and accessibility properties."""

    def test_dark_background_color(self, pw_page):
        pw_page.set_viewport_size({"width": 1440, "height": 900})
        bg = pw_page.evaluate(
            "getComputedStyle(document.body).backgroundColor"
        )
        # e.g. "rgb(15, 17, 23)" for #0f1117 — just check it's a dark color
        # Parse r,g,b and verify lightness < 30 (0–255 scale)
        import re
        m = re.findall(r"\d+", bg or "")
        if m and len(m) >= 3:
            r, g, b = int(m[0]), int(m[1]), int(m[2])
            lightness = (max(r, g, b) + min(r, g, b)) / 2
            assert lightness < 60, \
                f"Background must be dark (lightness < 60/255), got {bg} => lightness={lightness:.0f}"

    def test_no_horizontal_overflow(self, pw_page):
        pw_page.set_viewport_size({"width": 1440, "height": 900})
        overflow = pw_page.evaluate(
            "document.body.scrollWidth <= window.innerWidth"
        )
        assert overflow, "Page must not have horizontal scrollbar at 1440px desktop width"

    def test_bento_tiles_visible_above_fold(self, pw_page):
        pw_page.set_viewport_size({"width": 1440, "height": 900})
        # The bento grid tiles (.tile class inside #bento-grid) must be visible without scrolling
        tiles = pw_page.query_selector_all("#bento-grid .tile, .bento .tile")
        assert len(tiles) >= 4, f"Expected >= 4 bento tiles above fold, found: {len(tiles)}"
        for tile in tiles[:4]:
            box = tile.bounding_box()
            assert box is not None, "bento tile must have a bounding box"
            assert box["y"] < 900, \
                f"Bento tile not visible in 900px viewport (y={box['y']:.0f})"

    def test_view_buttons_keyboard_accessible(self, pw_page):
        for btn_id in ("#vbtn-pulse", "#vbtn-chat", "#vbtn-admin"):
            tab_idx = pw_page.evaluate(
                f"document.querySelector('{btn_id}')?.tabIndex ?? -99"
            )
            assert tab_idx >= 0, \
                f"{btn_id} must have tabIndex >= 0 (keyboard accessible), got {tab_idx}"

    def test_chat_input_keyboard_focusable(self, pw_page):
        pw_page.click("#vbtn-chat")
        pw_page.wait_for_timeout(200)
        tab_idx = pw_page.evaluate("document.querySelector('#chat-in')?.tabIndex ?? -99")
        assert tab_idx >= 0, \
            f"#chat-in must be keyboard focusable (tabIndex >= 0), got {tab_idx}"

    def test_body_font_size_readable(self, pw_page):
        font_size = pw_page.evaluate(
            "parseFloat(getComputedStyle(document.body).fontSize)"
        )
        assert font_size >= 13, \
            f"Body font size must be >= 13px for readability, got {font_size}px"

    def test_no_old_sidebar_class(self, pw_page):
        sidebar = pw_page.query_selector('.sidebar, #sidebar')
        assert sidebar is None, \
            "Old .sidebar element must not be present in the redesigned dashboard"
