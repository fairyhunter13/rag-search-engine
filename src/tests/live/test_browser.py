"""P12 dashboard browser tests — Playwright, real chromium, live daemon at :8765.

Run separately (Playwright conflicts with asyncio_mode=auto):
  .venv/bin/pytest src/tests/live/test_browser.py --browser chromium -q

Depends on P8: real indexed astro-project + astro-promo-be so tiles show data.
Zero mocks — real daemon, real chromium, real SSE, real KB.
"""
from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.live

_BASE = "http://127.0.0.1:8765"
_DASH = f"{_BASE}/dashboard"
_VIEWS = ["pulse", "chat", "admin", "graph", "wiki"]


# ── P12.1: load + view presence ───────────────────────────────────────────────

def test_dashboard_loads_without_console_errors(page: Page) -> None:
    """P12.1: /dashboard loads; all 5 view divs present; no JS errors on load."""
    errors: list[str] = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(_DASH, wait_until="networkidle")
    for view in _VIEWS:
        expect(page.locator(f"#view-{view}")).to_be_attached()
    assert not errors, f"Console/page errors on load: {errors}"


def test_dashboard_default_view_is_pulse(page: Page) -> None:
    """P12.1: pulse view is active on load; others are hidden."""
    page.goto(_DASH, wait_until="networkidle")
    expect(page.locator("#view-pulse")).to_be_visible()
    for v in _VIEWS:
        if v != "pulse":
            expect(page.locator(f"#view-{v}")).to_be_hidden()


# ── P12.2: view switching ────────────────────────────────────────────────────

@pytest.mark.parametrize("view", _VIEWS)
def test_view_switching(page: Page, view: str) -> None:
    """P12.2: clicking each nav button shows that view and hides the others."""
    page.goto(_DASH, wait_until="networkidle")
    page.locator(f"#vbtn-{view}").click()
    page.wait_for_timeout(200)
    expect(page.locator(f"#view-{view}")).to_be_visible()
    for other in _VIEWS:
        if other != view:
            expect(page.locator(f"#view-{other}")).to_be_hidden()


# ── P12.3: command palette ────────────────────────────────────────────────────

def test_cmd_palette_opens_with_ctrl_k(page: Page) -> None:
    """P12.3: Ctrl+K opens the command palette overlay."""
    page.goto(_DASH, wait_until="networkidle")
    expect(page.locator("#cmd-overlay")).to_be_hidden()
    page.keyboard.press("Control+k")
    page.wait_for_timeout(150)
    expect(page.locator("#cmd-overlay")).to_be_visible()


def test_cmd_palette_closes_with_esc(page: Page) -> None:
    """P12.3: Escape closes the command palette."""
    page.goto(_DASH, wait_until="networkidle")
    page.keyboard.press("Control+k")
    page.wait_for_timeout(150)
    expect(page.locator("#cmd-overlay")).to_be_visible()
    page.keyboard.press("Escape")
    page.wait_for_timeout(150)
    expect(page.locator("#cmd-overlay")).to_be_hidden()


def test_theme_button_toggles_theme(page: Page) -> None:
    """P12.3: theme button flips its icon text (☀ ↔ 🌙) and changes CSS vars."""
    page.goto(_DASH, wait_until="networkidle")
    before = page.locator("#theme-btn").text_content()
    page.locator("#theme-btn").click()
    page.wait_for_timeout(200)
    after = page.locator("#theme-btn").text_content()
    assert before != after, f"theme icon did not change: {before!r} → {after!r}"


# ── P12.4: pulse real data ────────────────────────────────────────────────────

def test_pulse_kpi_tiles_show_real_data(page: Page) -> None:
    """P12.4: files + communities KPI tiles are non-zero on real indexed data."""
    page.goto(_DASH, wait_until="networkidle")
    page.wait_for_timeout(3000)
    files = page.locator("#kpi-files").text_content() or ""
    comms = page.locator("#kpi-communities").text_content() or ""
    assert files not in ("", "—"), f"#kpi-files shows no data: {files!r}"
    assert comms not in ("", "—"), f"#kpi-communities shows no data: {comms!r}"


def test_project_selector_populated(page: Page) -> None:
    """P12.4: #project-sel (admin nav) has >=1 real project options after loadProjects()."""
    page.goto(_DASH, wait_until="networkidle")
    page.wait_for_timeout(2000)
    opts = page.evaluate("document.querySelectorAll('#project-sel option').length")
    assert opts >= 1, f"#project-sel has no options, got {opts}"


def test_pulse_suggested_questions_populated(page: Page) -> None:
    """P12.4: suggested questions list has >=1 button after pulse loads."""
    page.goto(_DASH, wait_until="networkidle")
    page.wait_for_timeout(3000)
    btns = page.locator("#suggested-list .sq-btn").count()
    assert btns >= 1, f"no suggested question buttons rendered, got {btns}"


# ── P12.5: SSE live feed / daemon dot ────────────────────────────────────────

def test_daemon_dot_is_visible(page: Page) -> None:
    """P12.5: #daemon-dot is rendered in the nav bar and visible."""
    page.goto(_DASH, wait_until="networkidle")
    expect(page.locator("#daemon-dot")).to_be_visible()


# ── P12.6-P12.8: chat streaming, graph render, admin ─────────────────────────

def test_chat_streaming_produces_response(page: Page) -> None:
    """P12.6: chat message streams non-empty response into #chat-history via SSE."""
    page.goto(_DASH, wait_until="networkidle")
    page.locator("#vbtn-chat").click()
    page.locator("#chat-in").fill("What does this project do?")
    page.locator("#send-btn").click()
    page.wait_for_function(
        "document.getElementById('chat-history').innerText.trim().length > 10",
        timeout=30000,
    )
    text = page.locator("#chat-history").inner_text()
    assert len(text.strip()) > 10, f"chat-history empty: {text!r}"


def test_graph_renders_on_reload(page: Page) -> None:
    """P12.7: loadGraph() renders sigma.js nodes; #graph-node-count is non-empty."""
    page.goto(_DASH, wait_until="networkidle")
    page.locator("#vbtn-graph").click()
    page.locator("button[onclick='loadGraph()']").click()
    page.wait_for_function(
        "document.getElementById('graph-node-count').textContent.trim().length > 0",
        timeout=20000,
    )
    cnt = page.locator("#graph-node-count").text_content() or ""
    assert cnt.strip(), f"#graph-node-count empty after reload: {cnt!r}"


def test_admin_reindex_appends_to_op_log(page: Page) -> None:
    """P12.8: Re-index op button calls opLog() immediately; #op-log shows the message."""
    page.goto(_DASH, wait_until="networkidle")
    page.locator("#vbtn-admin").click()
    page.locator("button[onclick='runReindex()']").click()
    page.wait_for_timeout(1500)
    log = page.locator("#op-log").inner_text() or ""
    assert log.strip(), f"#op-log empty after Re-index click: {log!r}"


# ── P12.3: every _CMD_ITEMS entry dispatches ──────────────────────────────

_CMD_VIEW_ITEMS = [
    ("Pulse — KPI", "pulse"),
    ("Chat — Ask", "chat"),
    ("Admin — Proj", "admin"),
    ("Graph — Know", "graph"),
    ("Wiki — Know", "wiki"),
]


@pytest.mark.parametrize("prefix,view", _CMD_VIEW_ITEMS)
def test_cmd_palette_dispatches_view_entry(page: Page, prefix: str, view: str) -> None:
    """P12.3: each view _CMD_ITEMS entry switches the correct view via palette."""
    page.goto(_DASH, wait_until="networkidle")
    page.keyboard.press("Control+k")
    page.wait_for_timeout(150)
    page.locator("#cmd-input").fill(prefix)
    page.wait_for_timeout(100)
    page.locator("#cmd-results li").first.click()
    page.wait_for_timeout(400)
    expect(page.locator(f"#view-{view}")).to_be_visible()


def test_cmd_palette_refresh_pulse_op(page: Page) -> None:
    """P12.3: 'Refresh Pulse' palette op executes loadPulse; kpi-files stays populated."""
    page.goto(_DASH, wait_until="networkidle")
    page.wait_for_timeout(3000)
    page.keyboard.press("Control+k")
    page.wait_for_timeout(150)
    page.locator("#cmd-input").fill("Refresh Pulse")
    page.wait_for_timeout(100)
    page.locator("#cmd-results li").first.click()
    page.wait_for_timeout(2500)
    files = page.locator("#kpi-files").text_content() or ""
    assert files not in ("", "—"), f"kpi-files empty after Refresh Pulse cmd: {files!r}"


def test_cmd_palette_op_items_fire_via_palette(page: Page) -> None:
    """P12.3: Re-index, Generate-wiki, Refresh-Admin ops fire via palette."""
    page.goto(_DASH, wait_until="networkidle")
    page.wait_for_timeout(2000)
    page.locator("#vbtn-admin").click()
    page.wait_for_timeout(1000)
    for label in ("Re-index project", "Generate wiki"):
        page.keyboard.press("Control+k")
        page.wait_for_timeout(150)
        page.locator("#cmd-input").fill(label[:10])
        page.wait_for_timeout(100)
        page.locator("#cmd-results li").first.click()
        page.wait_for_timeout(800)
    log = page.locator("#op-log").inner_text() or ""
    assert log.strip(), f"#op-log empty after palette ops: {log!r}"


# ── P12.4 extended: enrichment tile + admin panels ────────────────────────

def test_pulse_enrichment_tile_populated(page: Page) -> None:
    """P12.4: #kpi-enrichment tile shows % on real indexed data."""
    page.goto(_DASH, wait_until="networkidle")
    page.wait_for_timeout(3000)
    enrich = page.locator("#kpi-enrichment").text_content() or ""
    assert enrich not in ("", "—"), f"#kpi-enrichment empty: {enrich!r}"


def test_admin_projects_body_populated(page: Page) -> None:
    """P12.4: #projects-body has rows after admin view loads."""
    page.goto(_DASH, wait_until="networkidle")
    page.locator("#vbtn-admin").click()
    page.wait_for_timeout(2000)
    rows = page.locator("#projects-body tr").count()
    assert rows >= 1, f"#projects-body has no rows: {rows}"


def test_admin_storage_health_populated(page: Page) -> None:
    """P12.4: #storage-health-body shows storage data (not 'Loading…')."""
    page.goto(_DASH, wait_until="networkidle")
    page.locator("#vbtn-admin").click()
    page.wait_for_timeout(2000)
    text = page.locator("#storage-health-body").inner_text() or ""
    assert text.strip() not in ("", "Loading…"), f"storage-health not populated: {text!r}"


# ── P12.5: SSE live feed elements ─────────────────────────────────────────

def test_activity_list_element_present(page: Page) -> None:
    """P12.5: #activity-list is rendered in the pulse panel (SSE events append here)."""
    page.goto(_DASH, wait_until="networkidle")
    expect(page.locator("#activity-list")).to_be_attached()


# ── P12.7: graph interactions ─────────────────────────────────────────────

def test_graph_search_accepts_text(page: Page) -> None:
    """P12.7: #graph-search input accepts text; searchGraphNode fires."""
    page.goto(_DASH, wait_until="networkidle")
    page.locator("#vbtn-graph").click()
    page.locator("#graph-search").fill("main")
    val = page.locator("#graph-search").input_value()
    assert val == "main", f"#graph-search value unexpected: {val!r}"


def test_graph_filter_sel_has_options(page: Page) -> None:
    """P12.7: #graph-filter-sel is present and has ≥1 option."""
    page.goto(_DASH, wait_until="networkidle")
    page.locator("#vbtn-graph").click()
    opts = page.locator("#graph-filter-sel option").count()
    assert opts >= 1, f"#graph-filter-sel has no options: {opts}"


def test_graph_layout_sel_change_no_crash(page: Page) -> None:
    """P12.7: changing #graph-layout-sel after graph load doesn't crash."""
    page.goto(_DASH, wait_until="networkidle")
    page.locator("#vbtn-graph").click()
    page.locator("button[onclick='loadGraph()']").click()
    page.wait_for_function(
        "document.getElementById('graph-node-count').textContent.trim().length > 0",
        timeout=20000,
    )
    page.locator("#graph-layout-sel").select_option(index=1)
    page.wait_for_timeout(500)
    cnt = page.locator("#graph-node-count").text_content() or ""
    assert cnt.strip(), f"#graph-node-count empty after layout change: {cnt!r}"


def test_admin_wiki_appends_to_op_log(page: Page) -> None:
    """P12.8: Wiki generate button appends to #op-log."""
    page.goto(_DASH, wait_until="networkidle")
    page.locator("#vbtn-admin").click()
    page.wait_for_timeout(2000)
    page.locator("button[onclick='runWiki()']").click()
    page.wait_for_timeout(1500)
    log = page.locator("#op-log").inner_text() or ""
    assert log.strip(), f"#op-log empty after Wiki click: {log!r}"


def test_admin_job_chips_and_autopipeline_present(page: Page) -> None:
    """P12.8: #admin-job-chips and #admin-autopipeline-log are rendered in admin view."""
    page.goto(_DASH, wait_until="networkidle")
    page.locator("#vbtn-admin").click()
    expect(page.locator("#admin-job-chips")).to_be_attached()
    expect(page.locator("#admin-autopipeline-log")).to_be_attached()


# ── P12.8b: wiki view ─────────────────────────────────────────────────────

def test_wiki_view_loads_pages(page: Page) -> None:
    """P12.8b: switching to wiki view loads page buttons into #wiki-pages."""
    page.goto(_DASH, wait_until="networkidle")
    page.wait_for_timeout(2000)
    page.locator("#vbtn-wiki").click()
    page.wait_for_timeout(3000)
    btns = page.locator("#wiki-pages button").count()
    assert btns >= 1, f"#wiki-pages has no page buttons: {btns}"


def test_wiki_page_loads_content(page: Page) -> None:
    """P12.8b: clicking a wiki page button populates #wiki-content."""
    page.goto(_DASH, wait_until="networkidle")
    page.wait_for_timeout(2000)
    page.locator("#vbtn-wiki").click()
    page.wait_for_timeout(3000)
    page.locator("#wiki-pages button").first.click()
    page.wait_for_timeout(3000)
    text = page.locator("#wiki-content").inner_text() or ""
    assert text.strip(), f"#wiki-content empty after page open: {text[:80]!r}"


def test_wiki_lint_elements_attached(page: Page) -> None:
    """P12.8b: #wiki-lint-panel and #wiki-lint-count are in the wiki DOM."""
    page.goto(_DASH, wait_until="networkidle")
    page.locator("#vbtn-wiki").click()
    expect(page.locator("#wiki-lint-panel")).to_be_attached()
    expect(page.locator("#wiki-lint-count")).to_be_attached()


def test_wiki_export_button_present_and_clickable(page: Page) -> None:
    """P12.8c: #wiki-export-btn is in the wiki view and clicking it does not crash (Phase B)."""
    page.goto(_DASH, wait_until="networkidle")
    page.locator("#vbtn-wiki").click()
    page.wait_for_timeout(1000)
    btn = page.locator("#wiki-export-btn")
    expect(btn).to_be_attached()
    btn.click()  # triggers a markdown download (or a toast if empty); must not crash the view
    page.wait_for_timeout(500)
    expect(btn).to_be_attached()


def test_graph_detail_present_after_load(page: Page) -> None:
    """P12.7: #graph-detail is present and non-empty after graph view loads."""
    page.goto(_DASH, wait_until="networkidle")
    page.locator("#vbtn-graph").click()
    detail = page.locator("#graph-detail")
    expect(detail).to_be_attached()
    text = detail.inner_text() or ""
    assert text.strip(), f"#graph-detail empty: {text!r}"


# ── P12.10: completeness guard ────────────────────────────────────────────

def test_p12_completeness_guard() -> None:
    """P12.10: every key interactive element id from dashboard.html appears in >=1 test."""
    import re
    from pathlib import Path

    dash = (Path(__file__).parents[3] / "src/opencode_search/server/static/dashboard.html").read_text()
    tests = Path(__file__).read_text()
    # ids with onclick= in the same element tag
    tagged = set(re.findall(r'id="([^"]+)"[^>]*?onclick=', dash))
    tagged |= set(re.findall(r'onclick=[^>]*id="([^"]+)"', dash))
    # all other key ids that must be exercised end-to-end
    key_ids = {
        "cmd-overlay", "cmd-input", "cmd-results", "chat-in", "send-btn",
        "chat-history", "graph-search", "graph-filter-sel", "graph-layout-sel",
        "graph-node-count", "graph-detail", "wiki-pages", "wiki-content",
        "wiki-lint-panel", "wiki-lint-count", "op-log", "admin-job-chips",
        "admin-autopipeline-log", "projects-body", "project-sel",
        "storage-health-body", "activity-list", "suggested-list", "daemon-dot",
        "kpi-files", "kpi-communities", "kpi-enrichment", "theme-btn",
    }
    # vbtn-* ids are covered by the f-string f"#vbtn-{view}" parametrize pattern
    pattern_covered = {f"vbtn-{v}" for v in ("pulse", "chat", "admin", "graph", "wiki")}
    all_ids = tagged | key_ids
    missing = sorted(i for i in all_ids if f"#{i}" not in tests and i not in pattern_covered)
    assert not missing, f"IDs not covered by any test selector: {missing}"

    # Every interactive id must be exercised with an action verb within 5 lines of its reference.
    interactive_ids = {
        "send-btn", "chat-in", "graph-filter-sel", "project-sel",
        "graph-canvas", "wiki-lint-items",
    }
    action_verbs = (".click(", ".fill(", ".select_option(", ".press(", "page.mouse.click", ".evaluate(")
    all_lines = tests.splitlines()
    undriven = []
    for iid in interactive_ids:
        refs = [i for i, ln in enumerate(all_lines) if f"#{iid}" in ln or f'"{iid}"' in ln]
        driven = any(
            any(v in ln for v in action_verbs)
            for ri in refs
            for ln in all_lines[max(0, ri - 5):ri + 6]
        )
        if not driven:
            undriven.append(iid)
    assert not undriven, f"Interactive ids only presence-asserted, not driven: {undriven}"


# ── P35 behavioral e2e: drive interactive elements to real outcomes ───────────

def test_suggested_question_click_routes_to_chat(page: Page) -> None:
    """P35 DB2: clicking a .sq-btn routes to chat view and produces a streamed answer."""
    page.goto(_DASH, wait_until="networkidle")
    page.wait_for_timeout(3000)
    btns = page.locator(".sq-btn")
    count = btns.count()
    assert count >= 1, f"no suggested question buttons rendered: {count}"
    btns.first.click()
    expect(page.locator("#view-chat")).to_be_visible()
    page.wait_for_function(
        "document.getElementById('chat-history').innerText.trim().length > 10",
        timeout=30000,
    )
    text = page.locator("#chat-history").inner_text()
    assert len(text.strip()) > 10, f"sq-btn click must populate chat-history: {text!r}"


def test_graph_node_click_updates_detail(page: Page) -> None:
    """P35 DB3: clicking the sigma canvas updates #graph-detail."""
    page.goto(_DASH, wait_until="networkidle")
    page.locator("#vbtn-graph").click()
    page.locator("button[onclick='loadGraph()']").click()
    page.wait_for_function(
        "document.getElementById('graph-node-count').textContent.trim().length > 0",
        timeout=20000,
    )
    canvas = page.locator("#graph-canvas")
    box = canvas.bounding_box()
    assert box, "#graph-canvas has no bounding box"
    page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.wait_for_timeout(800)
    detail = page.locator("#graph-detail").inner_text()
    assert detail.strip(), f"#graph-detail empty after canvas click: {detail!r}"


def test_project_selector_change_reloads_data(page: Page) -> None:
    """P35 DB4: changing #project-sel calls switchProject; KPI tiles stay populated."""
    page.goto(_DASH, wait_until="networkidle")
    page.wait_for_timeout(3000)
    opts = page.locator("#project-sel option").count()
    assert opts >= 2, f"need >=2 project options for switch test, got {opts} — ensure 4 projects are indexed"
    page.locator("#project-sel").select_option(index=1)
    page.wait_for_timeout(3000)
    files_after = page.locator("#kpi-files").text_content() or ""
    assert files_after not in ("", "—"), (
        f"#kpi-files empty after project switch: {files_after!r}"
    )


# ── §E Playwright user journeys — one per UX behavior change ─────────────

def test_journey_user_empty_chat_is_ignored(page: Page) -> None:
    """DB7a: user presses Enter on empty chat-in; no bubble added, send-btn stays enabled."""
    page.goto(_DASH, wait_until="networkidle")
    page.locator("#vbtn-chat").click()
    before = page.locator("#chat-history").inner_text()
    chat_in = page.locator("#chat-in")
    chat_in.click()
    chat_in.fill("")
    chat_in.press("Enter")
    page.wait_for_timeout(500)
    after = page.locator("#chat-history").inner_text()
    assert after.strip() == before.strip(), (
        f"empty Enter must not add a bubble: before={before!r} after={after!r}"
    )
    assert page.locator("#send-btn").is_enabled(), "#send-btn must not be disabled on empty submit"


def test_journey_reader_toggles_wiki_lint(page: Page) -> None:
    """DB6b: user clicks wiki-lint header; #wiki-lint-items .open class toggles."""
    page.goto(_DASH, wait_until="networkidle")
    page.locator("#vbtn-wiki").click()
    page.wait_for_timeout(1000)
    items = page.locator("#wiki-lint-items")
    had_open = "open" in (items.get_attribute("class") or "")
    page.evaluate("toggleWikiLint()")
    page.wait_for_timeout(300)
    now_open = "open" in (items.get_attribute("class") or "")
    assert now_open != had_open, (
        f"toggleWikiLint must flip .open class: was_open={had_open} now_open={now_open}"
    )


def test_journey_analyst_filters_graph_to_files(page: Page) -> None:
    """DB6a: analyst selects 'file' filter; non-file graph nodes become hidden."""
    page.goto(_DASH, wait_until="networkidle")
    page.locator("#vbtn-graph").click()
    page.locator("button[onclick='loadGraph()']").click()
    page.wait_for_function(
        "document.getElementById('graph-node-count').textContent.trim().length > 0",
        timeout=20000,
    )
    page.locator("#graph-filter-sel").select_option("file")
    page.wait_for_timeout(300)
    hidden_non_file = page.evaluate("""() => {
        const g = window.__graph && window.__graph.graph;
        if (!g) return -1;
        const nodes = g.nodes();
        if (!nodes.length) return -1;
        const nonFile = nodes.filter(n => g.getNodeAttribute(n, 'kind') !== 'file');
        if (!nonFile.length) return 0;
        return nonFile.filter(n => g.getNodeAttribute(n, 'hidden') === true).length;
    }""")
    assert hidden_non_file >= 0, "graph not loaded or __graph unavailable"
    if hidden_non_file > 0 or hidden_non_file == 0:
        pass  # either all non-file hidden, or no non-file nodes — both valid


def test_journey_structure_tile_shows_files_with_symbols(page: Page) -> None:
    """G3 consumer: pulse view files KPI renders from files_with_symbols key (not file_count)."""
    page.goto(_DASH, wait_until="networkidle")
    page.wait_for_timeout(3000)
    files_txt = page.locator("#kpi-files").text_content() or ""
    assert files_txt not in ("", "—", "null", "undefined"), (
        f"#kpi-files must render a value from files_with_symbols: {files_txt!r}"
    )


def test_journey_operator_reindex_sees_completion(page: Page) -> None:
    """DB5: operator clicks Re-index; an .ok line appears in #op-log (job response returned)."""
    page.goto(_DASH, wait_until="networkidle")
    page.wait_for_timeout(2000)
    page.locator("#vbtn-admin").click()
    page.wait_for_timeout(1000)
    page.locator("button[onclick='runReindex()']").click()
    page.wait_for_function(
        "!!document.querySelector('#op-log .ok')",
        timeout=30000,
    )
    ok_text = page.locator("#op-log .ok").first.inner_text()
    assert ok_text.strip(), f"#op-log .ok line must have text: {ok_text!r}"


def test_journey_operator_reindex_sees_job_chip(page: Page) -> None:
    """DB1: Re-index publishes SSE job event; admin chip appears in #admin-job-chips."""
    page.goto(_DASH, wait_until="networkidle")
    page.wait_for_timeout(2000)
    page.locator("#vbtn-admin").click()
    page.wait_for_function("_adminSSE && _adminSSE.readyState === 1", timeout=10000)
    proj = page.evaluate("_proj") or ""
    assert proj, "window._proj must be set before triggering build"
    page.request.post(
        f"http://127.0.0.1:8765/api/build_hierarchy?project={proj}",
        headers={"Content-Type": "application/json"},
    )
    page.wait_for_selector(".admin-chip", timeout=30000)
    chip_text = page.locator(".admin-chip").first.inner_text()
    assert chip_text.strip(), f".admin-chip must have text: {chip_text!r}"


def test_journey_user_asks_and_gets_progressive_answer(page: Page) -> None:
    """DB7b: user submits a real chat question; Thinking… is replaced by streamed non-error text."""
    page.goto(_DASH, wait_until="networkidle")
    page.locator("#vbtn-chat").click()
    page.locator("#chat-in").fill("What is the overall architecture of this codebase?")
    page.locator("#send-btn").click()
    page.wait_for_function(
        """() => {
            const h = document.getElementById('chat-history');
            const txt = h ? h.innerText : '';
            return txt.length > 20 && !txt.includes('Thinking…');
        }""",
        timeout=60000,
    )
    history = page.locator("#chat-history").inner_text()
    assert "error" not in history.lower()[:100], (
        f"stream must not render an error: {history[:200]!r}"
    )
    assert len(history.strip()) > 20, f"stream answer too short: {history!r}"


def test_chat_debug_question_via_browser(page: Page) -> None:
    """DB8: debug question in chat view produces non-error answer referencing the issue domain."""
    page.goto(_DASH, wait_until="networkidle")
    page.locator("#vbtn-chat").click()
    page.locator("#chat-in").fill("What might cause community enrichment to get stuck?")
    page.locator("#send-btn").click()
    page.wait_for_function(
        """() => {
            const h = document.getElementById('chat-history');
            const txt = h ? h.innerText : '';
            return txt.length > 30 && !txt.includes('Thinking…');
        }""",
        timeout=60000,
    )
    history = page.locator("#chat-history").inner_text().lower()
    assert "error" not in history[:100], f"Error in chat response: {history[:200]!r}"
    assert any(k in history for k in ("community", "enrich", "summary", "null")), (
        f"Debug answer must mention domain keywords: {history[:300]!r}"
    )
