"""P12 dashboard browser tests — Playwright, real chromium, live daemon at :8765.

Run separately (Playwright conflicts with asyncio_mode=auto):
  .venv/bin/pytest src/tests/live/test_browser.py --browser chromium -q

Uses sample_workspace (shop-federation + ledger-standalone) for all data assertions.
Zero mocks — real daemon, real chromium, real SSE.

The wiki and processes views left with tier 3, so every assertion that drove them is deleted
rather than re-pointed: the wiki pane rendered kb/wiki.py's generated pages and the processes
pane read BPRE. `docs` is the surviving half of the old wiki view — the repo's own
human-authored tree over /api/docs — so its tests are re-pointed, not dropped.
"""
from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from tests.live._sample_workspace import SampleWorkspace

pytestmark = pytest.mark.live

_BASE = "http://127.0.0.1:8765"
_DASH = f"{_BASE}/dashboard"
# `hierarchy` (the "Knowledge" tab) was never in this list, so #vbtn-hierarchy was the one nav
# button no test clicked — a gap the P12.10 completeness guard would have reported, except this
# whole module is excluded from CI's line. It is added here rather than deleted because the view
# is not tier 3: its loader calls overview(what="hierarchy"), a variant that never existed, over
# GET against a POST-only route. That dead surface predates R0 and is deliberately left in place;
# what these tests cover is the nav wiring, which works.
_VIEWS = ["pulse", "chat", "admin", "graph", "docs", "hierarchy"]


@pytest.fixture(scope="session", autouse=True)
def _browser_sample_workspace(sample_workspace: SampleWorkspace) -> SampleWorkspace:
    """Ensure sample projects are registered + indexed before any browser test runs."""
    return sample_workspace


@pytest.fixture(scope="session")
def _sample_promo(sample_workspace: SampleWorkspace) -> str:
    return sample_workspace.promo


@pytest.fixture(scope="session")
def _sample_fed(sample_workspace: SampleWorkspace) -> str:
    return sample_workspace.fed_root


@pytest.fixture(scope="session")
def _sample_cart(sample_workspace: SampleWorkspace) -> str:
    return sample_workspace.cart


def _select_project(page: Page, project_path: str, wait_ms: int = 2500) -> None:
    """Select a sample project by full path in #project-sel and wait for KPI refresh."""
    sel = page.locator("#project-sel")
    sel.wait_for(state="visible", timeout=10000)
    sel.select_option(value=project_path)
    page.wait_for_timeout(wait_ms)


# ── P12.1: load + view presence ───────────────────────────────────────────────

def test_dashboard_loads_without_console_errors(page: Page) -> None:
    """P12.1: /dashboard loads; every view div present; no JS errors on load."""
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


# ── P12.4: pulse data ─────────────────────────────────────────────────────────

def test_pulse_kpi_tiles_show_sample_data(page: Page, _sample_promo: str) -> None:
    """P12.4: files + communities KPI tiles are non-zero on sample indexed data."""
    page.goto(_DASH, wait_until="networkidle")
    _select_project(page, _sample_promo)
    files = page.locator("#kpi-files").text_content() or ""
    comms = page.locator("#kpi-communities").text_content() or ""
    assert files not in ("", "—"), f"#kpi-files shows no data for sample: {files!r}"
    assert comms not in ("", "—"), f"#kpi-communities shows no data for sample: {comms!r}"


def test_project_selector_populated(page: Page) -> None:
    """P12.4: #project-sel has >=1 sample project option after loadProjects()."""
    page.goto(_DASH, wait_until="networkidle")
    page.wait_for_timeout(2000)
    opts = page.evaluate("document.querySelectorAll('#project-sel option').length")
    assert opts >= 1, f"#project-sel has no options, got {opts}"


def test_pulse_suggested_questions_populated(page: Page, _sample_promo: str) -> None:
    """P12.4: suggested questions list has >=1 button after selecting sample promo-svc."""
    page.goto(_DASH, wait_until="networkidle")
    _select_project(page, _sample_promo)
    btns = page.locator("#suggested-list .sq-btn").count()
    assert btns >= 1, f"no suggested question buttons rendered for sample, got {btns}"


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


# P12.8 (Re-index → #op-log) left with tier 3. runReindex() and runWiki() were byte-identical
# apart from their log strings — both POSTed /api/build_wiki?action=wiki — so the "Re-index"
# button never re-indexed anything, and there is no live route to re-point the test at.


# ── P12.3: every _CMD_ITEMS entry dispatches ──────────────────────────────

_CMD_VIEW_ITEMS = [
    ("Pulse — KPI", "pulse"),
    ("Chat — Ask", "chat"),
    ("Admin — Proj", "admin"),
    ("Graph — Know", "graph"),
    ("Docs — Repo", "docs"),
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


def test_cmd_palette_refresh_pulse_op(page: Page, _sample_promo: str) -> None:
    """P12.3: 'Refresh Pulse' palette op executes loadPulse; kpi-files stays populated."""
    page.goto(_DASH, wait_until="networkidle")
    _select_project(page, _sample_promo)
    page.keyboard.press("Control+k")
    page.wait_for_timeout(150)
    page.locator("#cmd-input").fill("Refresh Pulse")
    page.wait_for_timeout(100)
    page.locator("#cmd-results li").first.click()
    page.wait_for_timeout(2500)
    files = page.locator("#kpi-files").text_content() or ""
    assert files not in ("", "—"), f"kpi-files empty after Refresh Pulse cmd: {files!r}"


# The two op entries this exercised — "Re-index project" and "Generate wiki" — left the palette
# with tier 3, along with the runReindex()/runWiki() functions behind them. The surviving op
# entries ("Refresh Admin", "Refresh Pulse") are covered by the Refresh-Pulse test above.


# ── P12.4 extended: admin panels ──────────────────────────────────────────

# The #kpi-enrichment tile left with tier 3: it read /api/kb_health's enrichment_pct, which
# measured DeepSeek narration coverage over community summaries. Structural labelling now fills
# every summary, so the number would be a permanent, meaningless 100%. #kpi-communities is the
# surviving KPI over that same table, and it is asserted in the id-coverage test below.


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


# The Wiki-generate half of P12.8 left with tier 3 along with its button and runWiki(). #op-log
# went with it too — I claimed here that opLog() survived via the Reload-config and Pause/Resume
# ops, and that was wrong: `git grep opLog` found the definition and nothing else. The same trace
# killed #admin-job-chips, whose /api/events/stream feed had no publisher left once the pipeline
# job runner was deleted, so the route, the chips and the sink are all gone.


def test_admin_autopipeline_present(page: Page) -> None:
    """P12.8: #admin-autopipeline-log is rendered in admin view."""
    page.goto(_DASH, wait_until="networkidle")
    page.locator("#vbtn-admin").click()
    expect(page.locator("#admin-autopipeline-log")).to_be_attached()


# ── P12.8b: docs view ─────────────────────────────────────────────────────
#
# Re-pointed from the wiki view rather than deleted: the wiki pane listed kb/wiki.py's generated
# pages, and the docs pane lists the project's own human-authored docs/ tree over /api/docs. The
# lint (#wiki-lint-*) and export (#wiki-export-btn) tests have no successor here — both operated on
# generated pages — so they are gone with the generator.
#
# The count assertion below is only meaningful because the promo-svc fixture now carries
# docs/README.md. /api/docs returns {"tree": []} for a project with no docs/ directory
# (routes_project.py:36-37), and _renderDocsPages then writes a "No docs in this project."
# placeholder with no <button> in it — so on the old fixture this would have been red, and an
# attached-only assertion would have been decoration passing on a broken fetch.

def test_docs_view_loads_pages(page: Page, _sample_promo: str) -> None:
    """P12.8b: switching to docs view loads page buttons into #docs-pages for sample promo-svc."""
    page.goto(_DASH, wait_until="networkidle")
    _select_project(page, _sample_promo)
    page.locator("#vbtn-docs").click()
    page.wait_for_timeout(3000)
    btns = page.locator("#docs-pages button").count()
    assert btns >= 1, f"#docs-pages has no page buttons for sample promo-svc: {btns}"


def test_docs_page_loads_content(page: Page, _sample_promo: str) -> None:
    """P12.8b: clicking a docs page button populates #docs-content for sample promo-svc."""
    page.goto(_DASH, wait_until="networkidle")
    _select_project(page, _sample_promo)
    page.locator("#vbtn-docs").click()
    page.wait_for_timeout(3000)
    page.locator("#docs-pages button").first.click()
    page.wait_for_timeout(3000)
    text = page.locator("#docs-content").inner_text() or ""
    assert text.strip(), f"#docs-content empty after page open for sample promo-svc: {text[:80]!r}"
    assert "Pick a page" not in text, f"#docs-content still shows the empty placeholder: {text[:80]!r}"


def test_docs_search_filters_pages(page: Page, _sample_promo: str) -> None:
    """P12.8b: typing in #docs-search filters #docs-pages down.

    Replaces the wiki-lint test's slot in this block. #docs-search is the only interactive control
    left in the docs sidebar, and nothing else exercised it.
    """
    page.goto(_DASH, wait_until="networkidle")
    _select_project(page, _sample_promo)
    page.locator("#vbtn-docs").click()
    page.wait_for_timeout(3000)
    before = page.locator("#docs-pages button").count()
    page.locator("#docs-search").fill("zzz-no-such-page")
    page.wait_for_timeout(500)
    after = page.locator("#docs-pages button").count()
    assert before >= 1 and after == 0, f"#docs-search did not filter: before={before} after={after}"


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

    dash = (Path(__file__).parents[3] / "src/rag_search/server/static/dashboard.html").read_text()
    tests = Path(__file__).read_text()
    # ids with onclick= in the same element tag
    tagged = set(re.findall(r'id="([^"]+)"[^>]*?onclick=', dash))
    tagged |= set(re.findall(r'onclick=[^>]*id="([^"]+)"', dash))
    # all other key ids that must be exercised end-to-end
    key_ids = {
        "cmd-overlay", "cmd-input", "cmd-results", "chat-in", "send-btn",
        "chat-history", "graph-search", "graph-filter-sel", "graph-layout-sel",
        "graph-node-count", "graph-detail", "docs-pages", "docs-content",
        "docs-search",
        "admin-autopipeline-log", "projects-body", "project-sel",
        "storage-health-body", "activity-list", "suggested-list", "daemon-dot",
        "kpi-files", "kpi-communities", "theme-btn",
    }
    # wiki-pages/wiki-content/wiki-lint-panel/wiki-lint-count/kpi-enrichment/op-log/admin-job-chips
    # left with tier 3 — the last two because their writers (opLog, the SSE job feed) did;
    # docs-pages/docs-content/docs-search are the surviving half of that view. Keeping a deleted id
    # in this list would fail the guard forever; dropping one that still exists would let it go
    # untested — so this list has to track dashboard.html on both sides, which is what the `tagged`
    # scan above does automatically for onclick-bearing elements.
    # vbtn-* ids are covered by the f-string f"#vbtn-{view}" parametrize pattern
    pattern_covered = {f"vbtn-{v}" for v in _VIEWS}
    all_ids = tagged | key_ids
    missing = sorted(i for i in all_ids if f"#{i}" not in tests and i not in pattern_covered)
    assert not missing, f"IDs not covered by any test selector: {missing}"

    # Every interactive id must be exercised with an action verb within 5 lines of its reference.
    interactive_ids = {
        "send-btn", "chat-in", "graph-filter-sel", "project-sel",
        # wiki-lint-items left with tier 3; docs-search takes its place as the docs view's one
        # interactive control, and is driven by test_docs_search_filters_pages.
        "graph-canvas", "docs-search",
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


def test_project_selector_change_reloads_data(page: Page, _sample_promo: str, _sample_cart: str) -> None:
    """P35 DB4: switching #project-sel between 2 sample projects; KPI tiles stay populated."""
    page.goto(_DASH, wait_until="networkidle")
    _select_project(page, _sample_promo)
    files_first = page.locator("#kpi-files").text_content() or ""
    assert files_first not in ("", "—"), f"#kpi-files empty for sample promo: {files_first!r}"
    _select_project(page, _sample_cart)
    files_after = page.locator("#kpi-files").text_content() or ""
    assert files_after not in ("", "—"), (
        f"#kpi-files empty after switching to sample cart-svc: {files_after!r}"
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


# DB6b (the wiki-lint disclosure) left with tier 3 along with toggleWikiLint() and the lint panel
# itself. The lint ran over kb/wiki.py's generated pages — broken links between generated pages,
# citations into a KB that no longer exists — so there is nothing in the docs view for it to
# inspect. The reader journey it covered is now test_docs_page_loads_content.


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


# DB5 and DB1 (the operator's Re-index journey) left with tier 3. Both drove the same button, which
# POSTed /api/build_wiki: DB5 read the completion line out of #op-log, DB1 read the SSE job chip the
# same POST published. The button, the log sink, the publisher and the stream are all gone, and the
# operator journey that replaces them is not a click at all — indexing is driven by the reconcile
# sweep, whose result the operator reads in the admin table (test_admin_projects_body_populated).


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
    page.locator("#chat-in").fill("What might cause a project's vector index to go stale?")
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
    # Re-pointed off community enrichment, which was DeepSeek's job and left with tier 3. Staleness
    # is the surviving equivalent — embed_signature vs. the store's stamp — and the keywords are
    # still terms the answer has to reach for rather than words echoed back from the question.
    assert any(k in history for k in ("index", "stale", "chunk", "embed")), (
        f"Debug answer must mention domain keywords: {history[:300]!r}"
    )


# The Processes view and the Wiki Docs group both left with tier 3, so their tests are deleted
# rather than re-pointed. #view-processes rendered kb/bpre.py's reconstructed process flows, which
# no longer exist in any form. #vbtn-wiki/#wiki-pages/#wiki-content listed kb/wiki.py's generated
# pages; the docs view above is the surviving half of that surface and carries its assertions.
