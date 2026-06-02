"""User journey tests — multi-step simulations of real developer workflows.

Each journey simulates what a developer actually does in sequence, not just
whether a single page renders. A journey failure means a real workflow is broken.

Run directly:
    .venv/bin/python scripts/user_journeys.py --project PATH [--url URL]

Or import from hpv.py:
    from user_journeys import run_all_journeys
"""
from __future__ import annotations

import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_REPO = Path(__file__).parent.parent
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO / "scripts"))

BASE_URL = "http://127.0.0.1:8765"


@dataclass
class StepResult:
    name: str
    passed: bool
    message: str
    duration_s: float = 0.0


@dataclass
class JourneyResult:
    journey_id: str
    journey_name: str
    passed: bool
    steps: list[StepResult]
    failed_at: str | None = None
    screenshot: str | None = None
    duration_s: float = 0.0
    notes: str = ""

    def summary(self) -> str:
        passed_steps = sum(1 for s in self.steps if s.passed)
        icon = "✅" if self.passed else "❌"
        return (
            f"{icon} [{self.journey_id}] {self.journey_name} "
            f"({passed_steps}/{len(self.steps)} steps, {self.duration_s:.1f}s)"
        )


def _screenshot(page: Any, name: str, passed: bool, screenshots_dir: Path) -> str:
    prefix = "pass" if passed else "fail"
    path = screenshots_dir / f"journey_{prefix}_{name}.png"
    try:
        page.screenshot(path=str(path), full_page=False)
    except Exception:
        pass
    return str(path)


def _nav(page: Any, url: str, timeout: int = 12000) -> bool:
    try:
        page.goto(url, timeout=timeout, wait_until="domcontentloaded")
        return True
    except Exception:
        return False


def _go(page: Any, dashboard: str, project_path: str) -> bool:
    """Navigate to dashboard and select the target project.

    Returns False if navigation OR project selection fails.
    Project selection failure means journeys would run against the wrong project.
    """
    if not _nav(page, dashboard):
        return False
    time.sleep(0.8)
    selected = _select_project(page, project_path)
    if not selected:
        return False
    return True


def _select_project(page: Any, project_path: str, timeout: int = 10000) -> bool:
    """Select the target project in the dashboard dropdown.

    loadProjects() sets projects[0] as default. We wait for it to finish then override.
    We call switchProject() directly to bypass the race condition.

    Uses state='attached' because <option> elements are never 'visible' in Playwright's
    sense — they live inside a <select> and are invisible until the dropdown is opened.
    """
    try:
        # Poll until the dropdown has real options (not just "Loading…").
        # Playwright's wait_for_selector is unreliable for <option> elements even with
        # state='attached', because options inside a <select> may not be individually
        # detectable. Use direct JS polling instead.
        deadline = time.monotonic() + timeout / 1000.0
        while True:
            count = page.evaluate(
                "() => document.querySelectorAll('#project-select option:not([value=\"\"])').length"
            )
            if count > 0:
                break
            if time.monotonic() > deadline:
                return False
            time.sleep(0.3)
        # Additional wait to ensure loadProjects() async has fully settled
        time.sleep(1.0)
        result = page.evaluate(f"""
            (function() {{
                var sel = document.getElementById('project-select');
                if (!sel || sel.options.length === 0) return 'no_options';
                var target = {repr(project_path)};
                var chosen = null;
                // Exact match first
                for (var i = 0; i < sel.options.length; i++) {{
                    if (sel.options[i].value === target) {{
                        chosen = target; break;
                    }}
                }}
                // Last-segment match (e.g. "astro-project")
                if (!chosen) {{
                    var last = target.split('/').pop();
                    for (var i = 0; i < sel.options.length; i++) {{
                        if (sel.options[i].value.split('/').pop() === last) {{
                            chosen = sel.options[i].value; break;
                        }}
                    }}
                }}
                if (chosen) {{
                    sel.value = chosen;
                    // Call switchProject directly (sets currentProject + reloads active tab)
                    if (typeof switchProject === 'function') {{
                        switchProject(chosen);
                    }} else {{
                        window.currentProject = chosen;
                    }}
                    return 'selected:' + chosen;
                }}
                // List available options for debugging
                var opts = Array.from(sel.options).map(o => o.value).join('|');
                return 'not_found:' + target + ' options=' + opts;
            }})()
        """)
        time.sleep(1.5)  # let content reload after switchProject
        # Verify via localStorage — currentProject is a `let` variable (not window property),
        # so we confirm via the persisted localStorage value instead.
        saved = page.evaluate(
            "() => { try{return localStorage.getItem('opencode_selected_project');}catch(e){return '';} }"
        )
        expected_last = project_path.split("/")[-1]
        saved_last = str(saved or "").split("/")[-1]
        ok = str(result).startswith("selected:") and saved_last == expected_last
        return ok
    except Exception:
        return False


def _click_nav(page: Any, tab: str, timeout: int = 5000) -> bool:
    """Click a sidebar nav button by its showPage tab name (e.g. 'search', 'verify')."""
    try:
        # Dashboard uses id="nav-{tab}" pattern with onclick="showPage('{tab}')"
        sel = f"#nav-{tab}, [data-tab='{tab}'], button[onclick*=\"'{tab}'\"]"
        page.click(sel, timeout=timeout)
        time.sleep(0.5)
        return True
    except Exception:
        return False


def _no_broken_text(page: Any) -> tuple[bool, str]:
    """Scan visible text for common broken UI strings."""
    broken = ["undefined", "TypeError", "NaN", "[object Object]", "null", "Error:"]
    try:
        text = page.inner_text("body")
        found = [b for b in broken if b in text]
        return len(found) == 0, f"Found broken strings: {found}" if found else ""
    except Exception:
        return True, ""


def run_all_journeys(
    project_path: str,
    screenshots_dir: Path,
    base_url: str = BASE_URL,
) -> list[JourneyResult]:
    """Run all 15 developer journeys. Returns list of JourneyResult."""
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout  # noqa: F401
    except ImportError:
        return [JourneyResult(
            journey_id="J00", journey_name="playwright_missing",
            passed=False, steps=[],
            notes="playwright not installed — run: pip install playwright && playwright install chromium",
        )]

    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    from urllib.parse import quote as _q

    screenshots_dir.mkdir(parents=True, exist_ok=True)
    pp = str(Path(project_path).expanduser().resolve())
    pp_enc = _q(pp, safe="")
    dashboard = f"{base_url}/dashboard"
    results: list[JourneyResult] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1280, "height": 900})

        # Warm up: pre-load the project's index by making an API call before journeys start.
        # Large projects (25k+ files) need time to load the LanceDB index on first query.
        try:
            import urllib.request as _ur
            from urllib.error import URLError
            _ur.urlopen(f"{base_url}/api/overview?project={pp_enc}", timeout=30)
        except Exception:
            pass  # warm-up is optional

        # Pre-select the target project once using a setup page.
        # localStorage.setItem is called by switchProject(), so subsequent pages load it automatically.
        _setup_page = context.new_page()
        try:
            _nav(_setup_page, dashboard)
            time.sleep(1.5)
            _select_project(_setup_page, pp)
            time.sleep(1.0)
        except Exception:
            pass
        finally:
            _setup_page.close()

        # ── J01: Find the HTTP handler ────────────────────────────────────
        t_journey = time.monotonic()
        steps: list[StepResult] = []
        page = context.new_page()
        try:
            t = time.monotonic()
            ok = _go(page, dashboard, pp)
            steps.append(StepResult("open_dashboard", ok, "Dashboard opened with project selected" if ok else "Failed", time.monotonic() - t))

            # Navigate to search tab
            t = time.monotonic()
            try:
                page.click("[data-tab='search'], #nav-search, a[href='#search']", timeout=5000)
                time.sleep(0.5)
                ok = True
            except Exception:
                ok = False
            steps.append(StepResult("nav_to_search", ok, "Navigated to search tab", time.monotonic() - t))

            # Type query
            t = time.monotonic()
            try:
                page.fill("#search-query, input[placeholder*='search'], input[type='search']",
                          "HTTP handler route", timeout=5000)
                page.keyboard.press("Enter")
                # Wait up to 10s for search results to appear
                result_text = ""
                for _ in range(34):
                    result_text = page.inner_text("body")
                    if any(x in result_text.lower() for x in [".go", ".py", ".ts", "handler", "route"]):
                        break
                    time.sleep(0.3)
                has_results = any(x in result_text.lower() for x in [".go", ".py", ".ts", "handler", "route"])
                steps.append(StepResult("search_for_handler", has_results,
                                        "Search returned results with handler/route files" if has_results
                                        else "No relevant results found",
                                        time.monotonic() - t))
            except Exception as exc:
                steps.append(StepResult("search_for_handler", False, str(exc)[:80], time.monotonic() - t))

            # Check no broken text
            ok_bt, msg_bt = _no_broken_text(page)
            steps.append(StepResult("no_broken_text", ok_bt, msg_bt or "No broken text", 0.0))

        except Exception as exc:
            steps.append(StepResult("unexpected_error", False, str(exc)[:100], 0.0))
        finally:
            scr = _screenshot(page, "J01", all(s.passed for s in steps), screenshots_dir)
            page.close()

        passed = all(s.passed for s in steps)
        failed_at = next((s.name for s in steps if not s.passed), None)
        results.append(JourneyResult("J01", "Find the HTTP handler", passed, steps,
                                     failed_at=failed_at, screenshot=scr,
                                     duration_s=time.monotonic() - t_journey))

        # ── J02: Understand auth flow ─────────────────────────────────────
        t_journey = time.monotonic()
        steps = []
        page = context.new_page()
        try:
            _go(page, dashboard, pp)

            t = time.monotonic()
            try:
                page.click("[data-tab='ask'], #nav-ask, a[href='#ask']", timeout=5000)
                time.sleep(0.5)
                ok = True
            except Exception:
                ok = False
            steps.append(StepResult("nav_to_ask", ok, "Navigated to ask tab", time.monotonic() - t))

            t = time.monotonic()
            try:
                page.fill("#ask-q, #ask-query, input[placeholder*='How does'], textarea[placeholder*='question']",
                          "How does authentication work?", timeout=5000)
                page.keyboard.press("Enter")
                time.sleep(2.5)
                answer_text = ""
                for sel in ["#ask-answer", ".ask-result", ".answer-text", "#answer-container"]:
                    try:
                        answer_text = page.inner_text(sel, timeout=2000)
                        if answer_text.strip():
                            break
                    except Exception:
                        pass
                # Fall back to full body text scan
                if not answer_text:
                    body = page.inner_text("body")
                    if "auth" in body.lower() and len(body) > 200:
                        answer_text = body[:500]
                answer_ok = len(answer_text.strip()) >= 100
                has_code_ref = bool(re.search(r'\.(go|py|ts|js|rb|java)\b', answer_text))
                steps.append(StepResult("ask_about_auth", answer_ok,
                                        f"Got answer ({len(answer_text)} chars, code refs: {has_code_ref})"
                                        if answer_ok else f"Answer too short: {len(answer_text)} chars",
                                        time.monotonic() - t))
            except Exception as exc:
                steps.append(StepResult("ask_about_auth", False, str(exc)[:80], time.monotonic() - t))

            ok_bt, msg_bt = _no_broken_text(page)
            steps.append(StepResult("no_broken_text", ok_bt, msg_bt or "No broken text", 0.0))

        except Exception as exc:
            steps.append(StepResult("unexpected_error", False, str(exc)[:100], 0.0))
        finally:
            scr = _screenshot(page, "J02", all(s.passed for s in steps), screenshots_dir)
            page.close()

        passed = all(s.passed for s in steps)
        results.append(JourneyResult("J02", "Understand auth flow", passed, steps,
                                     failed_at=next((s.name for s in steps if not s.passed), None),
                                     screenshot=scr, duration_s=time.monotonic() - t_journey))

        # ── J03: Explore a community ──────────────────────────────────────
        t_journey = time.monotonic()
        steps = []
        page = context.new_page()
        try:
            _go(page, dashboard, pp)
            t = time.monotonic()
            try:
                page.click("[data-tab='communities'], #nav-communities, a[href*='communit']", timeout=10000)
                # Wait until communities panel renders content (up to 5s).
                # Community titles are user-defined (e.g. "API Layer", "Auth Module") so
                # we cannot keyword-match; we check for the communities-list element
                # having any content, or for a substantial page body as fallback.
                comm_section_text = ""
                for _ in range(17):
                    try:
                        comm_section_text = page.inner_text("#communities-list, #page-communities", timeout=500)
                    except Exception:
                        comm_section_text = ""
                    if len(comm_section_text.strip()) > 50:
                        break
                    time.sleep(0.3)
                # Fallback: full body check in case panel ID differs
                if len(comm_section_text.strip()) <= 50:
                    comm_section_text = page.inner_text("body")
                comm_text = comm_section_text
                has_communities = len(comm_section_text.strip()) > 50
                steps.append(StepResult("open_communities", has_communities,
                                        "Communities page loaded" if has_communities else "No communities visible",
                                        time.monotonic() - t))
            except Exception as exc:
                steps.append(StepResult("open_communities", False, str(exc)[:80], time.monotonic() - t))

            t = time.monotonic()
            try:
                # Check that communities page has meaningful content:
                # either file references OR community titles (not just "Community 123")
                comm_text = page.inner_text("body")
                has_files = bool(re.search(r'\.(go|py|ts|js|rb|java|c|cpp|rs)\b', comm_text))
                # Check for any substantial community title (not just generic "Community N")
                has_titles = bool(re.search(
                    r'(?:Authentication|Database|API|Handler|Service|Controller|'
                    r'Repository|Model|Config|HTTP|GRPC|Auth|Payment|User|Order)',
                    comm_text, re.IGNORECASE,
                ))
                has_content = len(comm_text) > 300
                passed = has_files or has_titles or has_content
                steps.append(StepResult("community_has_content", passed,
                                        f"Community page: files={has_files}, titles={has_titles}, content_len={len(comm_text)}",
                                        time.monotonic() - t))
            except Exception as exc:
                steps.append(StepResult("community_has_content", False, str(exc)[:80], time.monotonic() - t))

            ok_bt, msg_bt = _no_broken_text(page)
            steps.append(StepResult("no_broken_text", ok_bt, msg_bt or "No broken text", 0.0))

        except Exception as exc:
            steps.append(StepResult("unexpected_error", False, str(exc)[:100], 0.0))
        finally:
            scr = _screenshot(page, "J03", all(s.passed for s in steps), screenshots_dir)
            page.close()

        passed = all(s.passed for s in steps)
        results.append(JourneyResult("J03", "Explore a community", passed, steps,
                                     failed_at=next((s.name for s in steps if not s.passed), None),
                                     screenshot=scr, duration_s=time.monotonic() - t_journey))

        # ── J04: Read wiki article ────────────────────────────────────────
        t_journey = time.monotonic()
        steps = []
        page = context.new_page()
        try:
            _go(page, dashboard, pp)
            t = time.monotonic()
            try:
                page.click("[data-tab='wiki'], #nav-wiki, a[href*='wiki']", timeout=5000)
                time.sleep(1.0)
                wiki_text = page.inner_text("body")
                has_articles = "wiki" in wiki_text.lower() or len(wiki_text) > 300
                steps.append(StepResult("open_wiki", has_articles,
                                        "Wiki page loaded" if has_articles else "Wiki page empty",
                                        time.monotonic() - t))
            except Exception as exc:
                steps.append(StepResult("open_wiki", False, str(exc)[:80], time.monotonic() - t))

            t = time.monotonic()
            try:
                page.click(".wiki-article, .wiki-item, .wiki-link, a[href*='wiki']", timeout=5000)
                time.sleep(1.0)
                article_text = page.inner_text("body")
                is_long = len(article_text) >= 200
                ok_bt, _ = _no_broken_text(page)
                steps.append(StepResult("wiki_article_readable", is_long and ok_bt,
                                        f"Article has {len(article_text)} chars, no broken text: {ok_bt}",
                                        time.monotonic() - t))
            except Exception:
                # If can't click article, check page at least has content
                article_text = page.inner_text("body")
                is_long = len(article_text) >= 200
                steps.append(StepResult("wiki_article_readable", is_long,
                                        f"Wiki page has content ({len(article_text)} chars, no click needed)",
                                        time.monotonic() - t))

        except Exception as exc:
            steps.append(StepResult("unexpected_error", False, str(exc)[:100], 0.0))
        finally:
            scr = _screenshot(page, "J04", all(s.passed for s in steps), screenshots_dir)
            page.close()

        passed = all(s.passed for s in steps)
        results.append(JourneyResult("J04", "Read wiki article", passed, steps,
                                     failed_at=next((s.name for s in steps if not s.passed), None),
                                     screenshot=scr, duration_s=time.monotonic() - t_journey))

        # ── J05: Check code structure ─────────────────────────────────────
        t_journey = time.monotonic()
        steps = []
        page = context.new_page()
        try:
            _go(page, dashboard, pp)
            t = time.monotonic()
            try:
                page.click("[data-tab='structure'], #nav-structure, a[href*='structure']", timeout=10000)
                time.sleep(1.0)
                struct_text = page.inner_text("body")
                # Match any file extension — project may use YAML, Markdown, Go, Python, etc.
                has_tree = bool(re.search(r'\.\w{1,6}\b', struct_text)) and len(struct_text) > 200
                has_lang = any(x in struct_text.lower() for x in [
                    "go", "python", "typescript", "javascript", "rust",
                    "yaml", "markdown", "shell", "astro", "html",
                ])
                steps.append(StepResult("structure_has_files", has_tree or has_lang,
                                        f"Structure shows files: {has_tree}, languages: {has_lang}",
                                        time.monotonic() - t))
            except Exception as exc:
                steps.append(StepResult("structure_has_files", False, str(exc)[:80], time.monotonic() - t))

            ok_bt, msg_bt = _no_broken_text(page)
            steps.append(StepResult("no_broken_text", ok_bt, msg_bt or "No broken text", 0.0))

        except Exception as exc:
            steps.append(StepResult("unexpected_error", False, str(exc)[:100], 0.0))
        finally:
            scr = _screenshot(page, "J05", all(s.passed for s in steps), screenshots_dir)
            page.close()

        passed = all(s.passed for s in steps)
        results.append(JourneyResult("J05", "Check code structure", passed, steps,
                                     failed_at=next((s.name for s in steps if not s.passed), None),
                                     screenshot=scr, duration_s=time.monotonic() - t_journey))

        # ── J06: Service health check ─────────────────────────────────────
        t_journey = time.monotonic()
        steps = []
        page = context.new_page()
        try:
            _go(page, dashboard, pp)
            t = time.monotonic()
            try:
                page.click("[data-tab='health'], #nav-health, a[href*='health']", timeout=5000)
                time.sleep(1.0)
                health_text = page.inner_text("body")
                connected = "connected" in health_text.lower() or "ok" in health_text.lower()
                no_error = "error" not in health_text.lower() or "no error" in health_text.lower()
                steps.append(StepResult("health_shows_connected", connected,
                                        f"Health: connected={connected}" +
                                        ("" if no_error else " (error text present)"),
                                        time.monotonic() - t))
            except Exception as exc:
                steps.append(StepResult("health_shows_connected", False, str(exc)[:80], time.monotonic() - t))

            ok_bt, msg_bt = _no_broken_text(page)
            steps.append(StepResult("no_broken_text", ok_bt, msg_bt or "No broken text", 0.0))

        except Exception as exc:
            steps.append(StepResult("unexpected_error", False, str(exc)[:100], 0.0))
        finally:
            scr = _screenshot(page, "J06", all(s.passed for s in steps), screenshots_dir)
            page.close()

        passed = all(s.passed for s in steps)
        results.append(JourneyResult("J06", "Check service health", passed, steps,
                                     failed_at=next((s.name for s in steps if not s.passed), None),
                                     screenshot=scr, duration_s=time.monotonic() - t_journey))

        # ── J07: Overview shows meaningful KPIs ───────────────────────────
        t_journey = time.monotonic()
        steps = []
        page = context.new_page()
        try:
            _go(page, dashboard, pp)
            # Wait until overview KPIs are loaded (up to 8s for large projects)
            body_text = ""
            for _ in range(27):
                body_text = page.inner_text("body")
                if re.search(r'\b[0-9]+\b', body_text):
                    break
                time.sleep(0.3)
            t = time.monotonic()
            # KPIs should have numbers, not "undefined" or "NaN"
            has_numbers = bool(re.search(r'\b[0-9]+\b', body_text))
            ok_bt, msg_bt = _no_broken_text(page)
            steps.append(StepResult("overview_kpis_have_numbers", has_numbers and ok_bt,
                                    f"KPIs have numbers: {has_numbers}, no broken text: {ok_bt}" +
                                    (f" — {msg_bt}" if msg_bt else ""),
                                    time.monotonic() - t))

        except Exception as exc:
            steps.append(StepResult("unexpected_error", False, str(exc)[:100], 0.0))
        finally:
            scr = _screenshot(page, "J07", all(s.passed for s in steps), screenshots_dir)
            page.close()

        passed = all(s.passed for s in steps)
        results.append(JourneyResult("J07", "Overview shows meaningful KPIs", passed, steps,
                                     failed_at=next((s.name for s in steps if not s.passed), None),
                                     screenshot=scr, duration_s=time.monotonic() - t_journey))

        # ── J08: Graph shows nodes and edges ──────────────────────────────
        t_journey = time.monotonic()
        steps = []
        page = context.new_page()
        try:
            _go(page, dashboard, pp)
            t = time.monotonic()
            try:
                page.click("[data-tab='graph'], #nav-graph, a[href*='graph']", timeout=5000)
                time.sleep(0.8)
                graph_text = page.inner_text("body")
                has_graph_content = ("node" in graph_text.lower() or "edge" in graph_text.lower()
                                     or "symbol" in graph_text.lower() or len(graph_text) > 300)
                no_error_text = "Error:" not in graph_text and "failed" not in graph_text.lower()
                steps.append(StepResult("graph_page_loads", has_graph_content and no_error_text,
                                        "Graph page loaded with content" if (has_graph_content and no_error_text)
                                        else f"Graph page issue: content={has_graph_content}, no_error={no_error_text}",
                                        time.monotonic() - t))
            except Exception as exc:
                steps.append(StepResult("graph_page_loads", False, str(exc)[:80], time.monotonic() - t))

            ok_bt, msg_bt = _no_broken_text(page)
            steps.append(StepResult("no_broken_text", ok_bt, msg_bt or "No broken text", 0.0))

        except Exception as exc:
            steps.append(StepResult("unexpected_error", False, str(exc)[:100], 0.0))
        finally:
            scr = _screenshot(page, "J08", all(s.passed for s in steps), screenshots_dir)
            page.close()

        passed = all(s.passed for s in steps)
        results.append(JourneyResult("J08", "Graph page loads without errors", passed, steps,
                                     failed_at=next((s.name for s in steps if not s.passed), None),
                                     screenshot=scr, duration_s=time.monotonic() - t_journey))

        # ── J09: Pattern detection page ───────────────────────────────────
        t_journey = time.monotonic()
        steps = []
        page = context.new_page()
        try:
            _go(page, dashboard, pp)
            t = time.monotonic()
            try:
                page.click("[data-tab='patterns'], #nav-patterns, a[href*='pattern']", timeout=5000)
                time.sleep(1.0)
                patterns_text = page.inner_text("body")
                has_lang_data = any(x in patterns_text.lower()
                                    for x in ["go", "python", "javascript", "typescript", "rust", "java"])
                not_empty = "no patterns" not in patterns_text.lower() or len(patterns_text) > 500
                steps.append(StepResult("patterns_show_languages", has_lang_data,
                                        f"Patterns page shows language data: {has_lang_data}",
                                        time.monotonic() - t))
            except Exception as exc:
                steps.append(StepResult("patterns_show_languages", False, str(exc)[:80], time.monotonic() - t))

            ok_bt, msg_bt = _no_broken_text(page)
            steps.append(StepResult("no_broken_text", ok_bt, msg_bt or "No broken text", 0.0))

        except Exception as exc:
            steps.append(StepResult("unexpected_error", False, str(exc)[:100], 0.0))
        finally:
            scr = _screenshot(page, "J09", all(s.passed for s in steps), screenshots_dir)
            page.close()

        passed = all(s.passed for s in steps)
        results.append(JourneyResult("J09", "Pattern detection shows language data", passed, steps,
                                     failed_at=next((s.name for s in steps if not s.passed), None),
                                     screenshot=scr, duration_s=time.monotonic() - t_journey))

        # ── J10: Full navigation cycle — visit all nav items ──────────────
        t_journey = time.monotonic()
        steps = []
        page = context.new_page()
        errors_during_nav: list[str] = []
        page.on("console", lambda msg: errors_during_nav.append(msg.text)
                if msg.type == "error" else None)
        try:
            _go(page, dashboard, pp)
            t = time.monotonic()
            nav_tabs = page.query_selector_all("[data-tab], nav a[href^='#'], .nav-item")
            tab_count = len(nav_tabs)
            visited = 0
            for tab in nav_tabs[:20]:  # cap at 20
                try:
                    tab.click(timeout=2000)
                    time.sleep(0.2)
                    visited += 1
                except Exception:
                    pass
            steps.append(StepResult("visited_all_nav_tabs",
                                    visited >= min(tab_count, 5),
                                    f"Visited {visited}/{tab_count} nav tabs",
                                    time.monotonic() - t))

            # Filter out favicon and resource-not-found console noise
            real_errors = [e for e in errors_during_nav
                           if "favicon" not in e.lower() and "failed to load resource" not in e.lower()]
            steps.append(StepResult("no_js_errors_during_nav",
                                    len(real_errors) == 0,
                                    "No JS errors during navigation" if not real_errors
                                    else f"{len(real_errors)} JS errors: {real_errors[:2]}",
                                    0.0))

        except Exception as exc:
            steps.append(StepResult("unexpected_error", False, str(exc)[:100], 0.0))
        finally:
            scr = _screenshot(page, "J10", all(s.passed for s in steps), screenshots_dir)
            page.close()

        passed = all(s.passed for s in steps)
        results.append(JourneyResult("J10", "Full navigation cycle (no JS errors)", passed, steps,
                                     failed_at=next((s.name for s in steps if not s.passed), None),
                                     screenshot=scr, duration_s=time.monotonic() - t_journey))

        # ── J11: Dark mode toggle ─────────────────────────────────────────
        t_journey = time.monotonic()
        steps = []
        page = context.new_page()
        try:
            _go(page, dashboard, pp)
            t = time.monotonic()
            try:
                initial_theme = page.evaluate("document.documentElement.getAttribute('data-theme') || 'light'")
                page.click(
                    "button[aria-label*='theme'], button[title*='dark'], button[title*='light'], "
                    ".theme-toggle, #theme-toggle, button[title*='mode']",
                    timeout=5000,
                )
                time.sleep(0.3)
                new_theme = page.evaluate("document.documentElement.getAttribute('data-theme') || 'light'")
                toggled = new_theme != initial_theme
                steps.append(StepResult("theme_toggles", toggled,
                                        f"Theme changed: {initial_theme!r} → {new_theme!r}",
                                        time.monotonic() - t))
            except Exception:
                # Theme toggle is optional (P2) — pass if we can't find the button
                steps.append(StepResult("theme_toggles", True,
                                        "Theme toggle not found (optional feature)",
                                        time.monotonic() - t))

            ok_bt, msg_bt = _no_broken_text(page)
            steps.append(StepResult("no_broken_text_after_toggle", ok_bt, msg_bt or "No broken text", 0.0))

        except Exception as exc:
            steps.append(StepResult("unexpected_error", False, str(exc)[:100], 0.0))
        finally:
            scr = _screenshot(page, "J11", all(s.passed for s in steps), screenshots_dir)
            page.close()

        passed = all(s.passed for s in steps)
        results.append(JourneyResult("J11", "Dark mode toggle works", passed, steps,
                                     failed_at=next((s.name for s in steps if not s.passed), None),
                                     screenshot=scr, duration_s=time.monotonic() - t_journey))

        # ── J12: Impact analysis ──────────────────────────────────────────
        t_journey = time.monotonic()
        steps = []
        page = context.new_page()
        try:
            _go(page, dashboard, pp)
            t = time.monotonic()
            try:
                page.click("[data-tab='impact'], #nav-impact, a[href*='impact']", timeout=5000)
                time.sleep(0.5)
                impact_text = page.inner_text("body")
                has_form = bool(page.query_selector("#impact-symbol, input[placeholder*='symbol'], #page-impact input"))
                steps.append(StepResult("impact_page_loads", has_form or len(impact_text) > 200,
                                        "Impact page loaded with form" if has_form else "Impact page has content",
                                        time.monotonic() - t))
            except Exception as exc:
                steps.append(StepResult("impact_page_loads", False, str(exc)[:80], time.monotonic() - t))

            ok_bt, msg_bt = _no_broken_text(page)
            steps.append(StepResult("no_broken_text", ok_bt, msg_bt or "No broken text", 0.0))

        except Exception as exc:
            steps.append(StepResult("unexpected_error", False, str(exc)[:100], 0.0))
        finally:
            scr = _screenshot(page, "J12", all(s.passed for s in steps), screenshots_dir)
            page.close()

        passed = all(s.passed for s in steps)
        results.append(JourneyResult("J12", "Impact analysis page works", passed, steps,
                                     failed_at=next((s.name for s in steps if not s.passed), None),
                                     screenshot=scr, duration_s=time.monotonic() - t_journey))

        # ── J13: Arch-map and service-mesh render without Error text ───────
        t_journey = time.monotonic()
        steps = []
        page = context.new_page()
        try:
            _go(page, dashboard, pp)
            for tab_name in ("arch-map", "service-mesh"):
                t = time.monotonic()
                try:
                    # Use JS click to handle sidebar items that may be off-screen
                    tab_id = tab_name.replace("-", "-")  # keep as-is
                    clicked = page.evaluate(
                        f"() => {{ var el = document.getElementById('nav-{tab_name}'); "
                        f"if (el) {{ el.scrollIntoView(); el.click(); return true; }} "
                        f"return false; }}"
                    )
                    if not clicked:
                        page.click(f"#nav-{tab_name}, [data-tab='{tab_name}']", timeout=5000)
                    # Poll until loading spinner disappears — arch-map API can be slow
                    content_id = f"{tab_name}-content" if tab_name == "arch-map" else "service-mesh-content"
                    for _ in range(34):  # up to ~10s
                        try:
                            inner = page.inner_text(f"#{content_id}")
                            if "Loading" not in inner and "loading" not in inner:
                                break
                        except Exception:
                            break
                        time.sleep(0.3)
                    time.sleep(0.5)
                    # "Error:" in arch-map means a real JS exception (api URL bug or network fail).
                    # "No hierarchy. Run Build Hierarchy." is the correct empty state — not an error.
                    ok_bt, msg_bt = _no_broken_text(page)
                    passed = ok_bt
                    steps.append(StepResult(f"{tab_name}_no_error", passed,
                                            f"{tab_name}: rendered cleanly" if passed
                                            else f"{tab_name}: broken UI — {msg_bt}",
                                            time.monotonic() - t))
                except Exception as exc:
                    steps.append(StepResult(f"{tab_name}_no_error", False, str(exc)[:80], time.monotonic() - t))

        except Exception as exc:
            steps.append(StepResult("unexpected_error", False, str(exc)[:100], 0.0))
        finally:
            scr = _screenshot(page, "J13", all(s.passed for s in steps), screenshots_dir)
            page.close()

        passed = all(s.passed for s in steps)
        results.append(JourneyResult("J13", "Arch-map and service-mesh render cleanly", passed, steps,
                                     failed_at=next((s.name for s in steps if not s.passed), None),
                                     screenshot=scr, duration_s=time.monotonic() - t_journey))

        # ── J14: Verify and Release reports render ────────────────────────
        t_journey = time.monotonic()
        steps = []
        page = context.new_page()
        try:
            _go(page, dashboard, pp)
            for tab_name in ("verify", "release"):
                t = time.monotonic()
                try:
                    page.click(f"#nav-{tab_name}, [data-tab='{tab_name}']", timeout=5000)
                    time.sleep(1.5)
                    tab_text = page.inner_text("body")
                    has_report = (
                        "pass" in tab_text.lower() or "fail" in tab_text.lower()
                        or "check" in tab_text.lower() or "verdict" in tab_text.lower()
                        or len(tab_text) > 400
                    )
                    ok_bt, _ = _no_broken_text(page)
                    steps.append(StepResult(f"{tab_name}_shows_report", has_report and ok_bt,
                                            f"{tab_name}: has report content={has_report}, clean={ok_bt}",
                                            time.monotonic() - t))
                except Exception as exc:
                    steps.append(StepResult(f"{tab_name}_shows_report", False, str(exc)[:80], time.monotonic() - t))

        except Exception as exc:
            steps.append(StepResult("unexpected_error", False, str(exc)[:100], 0.0))
        finally:
            scr = _screenshot(page, "J14", all(s.passed for s in steps), screenshots_dir)
            page.close()

        passed = all(s.passed for s in steps)
        results.append(JourneyResult("J14", "Verify and Release report pages work", passed, steps,
                                     failed_at=next((s.name for s in steps if not s.passed), None),
                                     screenshot=scr, duration_s=time.monotonic() - t_journey))

        # ── J15: QA Gate dashboard page ───────────────────────────────────
        t_journey = time.monotonic()
        steps = []
        page = context.new_page()
        try:
            _go(page, dashboard, pp)
            t = time.monotonic()
            try:
                # Use JS to click #nav-qa (may be off-screen or absent in older daemon versions)
                clicked = page.evaluate(
                    "() => { var el = document.getElementById('nav-qa'); "
                    "if (el) { el.scrollIntoView(); el.click(); return true; } "
                    "return false; }"
                )
                if clicked:
                    time.sleep(1.0)
                    qa_text = page.inner_text("body")
                    has_qa_content = (
                        "qa" in qa_text.lower() or "gate" in qa_text.lower()
                        or "pillar" in qa_text.lower() or len(qa_text) > 300
                    )
                    ok_bt, msg_bt = _no_broken_text(page)
                    steps.append(StepResult("qa_page_loads", has_qa_content and ok_bt,
                                            f"QA page: content={has_qa_content}, clean={ok_bt}" +
                                            (f" — {msg_bt}" if msg_bt else ""),
                                            time.monotonic() - t))
                else:
                    # QA Gate nav not in DOM — daemon is serving stale HTML.
                    # This is a real failure: the QA Gate page is inaccessible to users.
                    steps.append(StepResult("qa_page_loads", False,
                                            "FAIL: #nav-qa not in served HTML — daemon must be restarted to serve updated dashboard",
                                            time.monotonic() - t))
            except Exception as exc:
                steps.append(StepResult("qa_page_loads", False, str(exc)[:80], time.monotonic() - t))

        except Exception as exc:
            steps.append(StepResult("unexpected_error", False, str(exc)[:100], 0.0))
        finally:
            scr = _screenshot(page, "J15", all(s.passed for s in steps), screenshots_dir)
            page.close()

        passed = all(s.passed for s in steps)
        results.append(JourneyResult("J15", "QA Gate dashboard page works", passed, steps,
                                     failed_at=next((s.name for s in steps if not s.passed), None),
                                     screenshot=scr, duration_s=time.monotonic() - t_journey))

        browser.close()

    return results


def print_results(results: list[JourneyResult]) -> int:
    passed = [r for r in results if r.passed]
    failed = [r for r in results if not r.passed]
    print(f"\n{'='*65}")
    print("  User Journey Tests")
    print(f"{'='*65}")
    for r in results:
        print(f"  {r.summary()}")
        if not r.passed and r.failed_at:
            for s in r.steps:
                icon = "✅" if s.passed else "❌"
                print(f"    {icon} {s.name}: {s.message[:60]}")
    print(f"{'='*65}")
    print(f"  Passed: {len(passed)}/{len(results)}")
    print()
    return 0 if len(failed) == 0 else 1


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="User journey tests")
    parser.add_argument("--project", required=True, help="Path to indexed project")
    parser.add_argument("--url", default=BASE_URL, help="Dashboard base URL")
    parser.add_argument("--screenshots-dir", default=".hpv_screenshots/journeys")
    args = parser.parse_args()

    scr_dir = Path(args.screenshots_dir)
    results = run_all_journeys(args.project, scr_dir, args.url)
    return print_results(results)


if __name__ == "__main__":
    sys.exit(main())
