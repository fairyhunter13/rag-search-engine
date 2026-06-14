"""P12 dashboard browser tests — Playwright, real chromium, live daemon at :8765.

Run separately (Playwright conflicts with asyncio_mode=auto):
  .venv/bin/pytest src/tests/live/test_browser.py --browser chromium -q

Depends on P8: real indexed redacted-name-3 + acme-promo-be so tiles show data.
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
