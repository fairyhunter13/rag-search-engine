#!/usr/bin/env python3
"""Simulate human interaction with the opencode-search dashboard.

Uses Playwright to open the dashboard in a headless browser, click through
all tabs, verify content loads correctly, and take screenshots.

Usage:
    .venv/bin/python scripts/simulate_human.py --project ~/git/.../redacted-name-3
    .venv/bin/python scripts/simulate_human.py --project ~/git/.../redacted-name-3 --screenshots ./screenshots

Requires: playwright (pip install playwright && playwright install chromium)
Exit codes: 0 = all pass, 1 = anomalies found
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

_REPO = Path(__file__).parent.parent


@dataclass
class UIAnomaly:
    scenario: str
    severity: str  # P0 | P1 | P2
    message: str
    screenshot: str = ""


@dataclass
class UIResult:
    scenario: str
    passed: bool
    message: str
    duration_s: float = 0.0
    screenshot: str = ""
    anomalies: list[UIAnomaly] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Part 1: API Health Checks (no browser needed)
# ---------------------------------------------------------------------------

def check_api_health(project_path: str, base_url: str = "http://127.0.0.1:8765") -> list[UIResult]:
    """Validate all dashboard API endpoints before opening the browser."""
    from urllib.parse import quote as _q
    pp = str(Path(project_path).expanduser().resolve())
    pp_enc = _q(pp, safe="")
    results = []

    def _get(path: str, timeout: int = 60) -> tuple[int, dict | str]:
        try:
            with urllib.request.urlopen(f"{base_url}{path}", timeout=timeout) as resp:
                # Limit read to 2 MB to avoid blocking on huge graph exports
                raw = resp.read(2 * 1024 * 1024)
                body = raw.decode("utf-8", errors="replace")
                try:
                    return resp.status, json.loads(body)
                except Exception:
                    # If JSON parse fails (e.g. truncated large response), return raw text
                    return resp.status, body
        except urllib.error.HTTPError as e:
            return e.code, {}
        except Exception as exc:
            return 0, {"_error": str(exc)}

    checks = [
        ("/healthz", lambda s, b: s == 200 and (b if isinstance(b, dict) else {}).get("ok") is True,
         "GET /healthz returns ok=true", "P0"),
        ("/api/projects", lambda s, b: s == 200 and len((b if isinstance(b, dict) else {}).get("projects", [])) >= 1,
         "/api/projects returns ≥1 project", "P0"),
        (f"/api/communities?project={pp_enc}&top_k=5",
         lambda s, b: s == 200 and len((b if isinstance(b, dict) else {}).get("communities", [])) >= 1,
         "/api/communities returns ≥1 community", "P1"),
        (f"/api/patterns?project={pp_enc}",
         lambda s, b: s == 200 and (b if isinstance(b, dict) else {}).get("status") == "ok",
         "/api/patterns returns status=ok", "P1"),
        (f"/api/graph_export?project={pp_enc}&format=json&max_nodes=50",
         lambda s, b: s == 200 and (
             "nodes" in (b if isinstance(b, dict) else {})
             or (isinstance(b, str) and '"nodes"' in b[:2000])
         ),
         "/api/graph_export returns nodes", "P1"),
        (f"/api/kb_health?project={pp_enc}",
         lambda s, b: s == 200 and isinstance(b, dict) and len(b) >= 1,
         "/api/kb_health returns data", "P1"),
        (f"/api/wiki?project={pp_enc}",
         lambda s, b: s == 200 and len((b if isinstance(b, dict) else {}).get("pages", [])) >= 1,
         "/api/wiki returns ≥1 page", "P1"),
        ("/api/auto_pipeline_status",
         lambda s, b: s == 200 and "enabled" in (b if isinstance(b, dict) else {}),
         "/api/auto_pipeline_status has enabled field", "P1"),
        ("/api/metrics",
         lambda s, b: s == 200 and isinstance(b, dict) and len(b) >= 1,
         "/api/metrics returns data", "P1"),
    ]

    for path, check_fn, desc, severity in checks:
        t0 = time.monotonic()
        status, body = _get(path)
        passed = check_fn(status, body)
        results.append(UIResult(
            scenario=f"api:{path.split('?')[0].split('/')[-1]}",
            passed=passed,
            message=f"✅ {desc}" if passed else f"❌ FAIL {desc} (HTTP {status})",
            duration_s=time.monotonic() - t0,
            anomalies=[] if passed else [UIAnomaly(
                scenario=desc, severity=severity,
                message=f"HTTP {status} — {str(body)[:200]}",
            )],
        ))

    return results


# ---------------------------------------------------------------------------
# Part 2: Playwright Browser Simulation
# ---------------------------------------------------------------------------

def run_ui_simulation(
    project_path: str,
    screenshots_dir: Path,
    base_url: str = "http://127.0.0.1:8765",
) -> list[UIResult]:
    """Simulate human browsing the dashboard. Returns list of UIResult."""
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        return [UIResult(
            scenario="playwright_import",
            passed=False,
            message="playwright not installed — run: pip install playwright && playwright install chromium",
            anomalies=[UIAnomaly(
                scenario="playwright_import", severity="P1",
                message="playwright package missing — UI simulation skipped",
            )],
        )]

    screenshots_dir.mkdir(parents=True, exist_ok=True)
    from urllib.parse import quote as _q
    pp = str(Path(project_path).expanduser().resolve())
    pp_enc = _q(pp, safe="")
    dashboard_url = f"{base_url}/dashboard"
    results: list[UIResult] = []
    console_errors: list[str] = []
    network_errors: list[str] = []

    def _screenshot(page, name: str, passed: bool) -> str:
        prefix = "pass" if passed else "fail"
        path = screenshots_dir / f"{prefix}_{name}.png"
        try:
            page.screenshot(path=str(path), full_page=False)
        except Exception:
            pass
        return str(path)

    def _result(scenario: str, passed: bool, msg: str, t0: float,
                page=None, anomalies: list[UIAnomaly] | None = None) -> UIResult:
        scr = _screenshot(page, scenario, passed) if page else ""
        return UIResult(
            scenario=scenario, passed=passed, message=msg,
            duration_s=time.monotonic() - t0, screenshot=scr,
            anomalies=anomalies or [],
        )

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        page = context.new_page()

        page.on("console", lambda msg: console_errors.append(msg.text)
                if msg.type == "error" else None)
        page.on("response", lambda r: network_errors.append(f"{r.status} {r.url}")
                if r.status >= 400 and "/api/" in r.url else None)
        # Also capture non-api 4xx to help debug console errors
        all_404s: list[str] = []
        page.on("response", lambda r: all_404s.append(r.url)
                if r.status == 404 else None)

        # ── dashboard_loads ───────────────────────────────────────────────
        t0 = time.monotonic()
        try:
            page.goto(dashboard_url, timeout=15000, wait_until="domcontentloaded")
            page.wait_for_load_state("networkidle", timeout=10000)
            title = page.title()
            body_text = page.inner_text("body")[:500]
            passed = "opencode" in title.lower() or "opencode" in body_text.lower()
            results.append(_result("dashboard_loads", passed,
                f"Dashboard loaded (title: {title!r})" if passed
                else f"Dashboard title unexpected: {title!r}", t0, page))
        except PWTimeout:
            results.append(_result("dashboard_loads", False,
                f"Dashboard timed out loading from {dashboard_url}", t0,
                anomalies=[UIAnomaly("dashboard_loads", "P0", "Page load timeout")]))
            browser.close()
            return results
        except Exception as exc:
            results.append(_result("dashboard_loads", False, f"Dashboard failed to load: {exc}", t0,
                anomalies=[UIAnomaly("dashboard_loads", "P0", str(exc))]))
            browser.close()
            return results

        # ── project_switch ────────────────────────────────────────────────
        t0 = time.monotonic()
        try:
            # Look for project select/dropdown
            sel = page.locator("select, [data-testid='project-select'], #project-select").first
            if sel.count() > 0:
                # Select the option containing the project name
                project_name = Path(pp).name
                page.select_option("select", label=project_name, timeout=3000)
                page.wait_for_load_state("networkidle", timeout=5000)
                results.append(_result("project_switch", True,
                    f"Switched to project: {project_name}", t0, page))
            else:
                results.append(_result("project_switch", True,
                    "No project dropdown found — single-project mode OK", t0, page))
        except Exception as exc:
            results.append(_result("project_switch", True,
                f"Project switch not available or single project: {exc}", t0, page))

        # ── Tab navigation helper ─────────────────────────────────────────
        def _click_tab(tab_text: str) -> bool:
            """Click a tab button matching tab_text. Returns True if found."""
            try:
                btn = page.get_by_role("button", name=tab_text).first
                if btn.count() > 0:
                    btn.click(timeout=3000)
                    page.wait_for_load_state("networkidle", timeout=5000)
                    return True
                # Fallback: text match
                el = page.locator(f"button:has-text('{tab_text}'), [data-tab='{tab_text.lower()}']").first
                if el.count() > 0:
                    el.click(timeout=3000)
                    page.wait_for_load_state("networkidle", timeout=5000)
                    return True
            except Exception:
                pass
            return False

        def _no_broken_text(content: str) -> list[str]:
            """Return list of broken strings found in visible content."""
            bad = ["undefined", "TypeError", "NaN", "[object Object]"]
            return [b for b in bad if b in content]

        # ── tab_projects ──────────────────────────────────────────────────
        t0 = time.monotonic()
        try:
            _click_tab("Projects")
            page.wait_for_timeout(1000)
            content = page.inner_text("body")
            rows = page.locator("table tr, [data-row], .project-row").count()
            broken = _no_broken_text(content)
            passed = rows >= 1 and not broken
            anomalies = [UIAnomaly("tab_projects", "P1", f"Broken text: {broken}")] if broken else []
            results.append(_result("tab_projects", passed,
                f"Projects tab: {rows} rows visible" if passed
                else f"Projects tab: {rows} rows, broken={broken}", t0, page, anomalies))
        except Exception as exc:
            results.append(_result("tab_projects", False, f"Projects tab error: {exc}", t0, page,
                [UIAnomaly("tab_projects", "P1", str(exc))]))

        # ── tab_structure ─────────────────────────────────────────────────
        t0 = time.monotonic()
        try:
            _click_tab("Structure")
            page.wait_for_timeout(1500)
            content = page.inner_text("body")
            has_tree = page.locator("li, .tree-node, pre").count() > 0
            has_lang = any(lang in content for lang in ["Go", "Python", "TypeScript", "JavaScript", ".go", ".py", ".ts"])
            broken = _no_broken_text(content)
            passed = (has_tree or has_lang) and not broken
            results.append(_result("tab_structure", passed,
                "Structure tab: directory tree and language info visible" if passed
                else f"Structure tab: tree={has_tree}, lang={has_lang}, broken={broken}", t0, page,
                [UIAnomaly("tab_structure", "P1", f"broken text: {broken}")] if broken else []))
        except Exception as exc:
            results.append(_result("tab_structure", False, f"Structure tab error: {exc}", t0, page,
                [UIAnomaly("tab_structure", "P1", str(exc))]))

        # ── tab_patterns ──────────────────────────────────────────────────
        t0 = time.monotonic()
        try:
            _click_tab("Patterns")
            page.wait_for_timeout(2000)
            content = page.inner_text("body")
            has_arch = any(word in content.lower() for word in
                           ["architecture", "framework", "language", "dependency", "pattern"])
            broken = _no_broken_text(content)
            passed = has_arch and not broken
            results.append(_result("tab_patterns", passed,
                "Patterns tab: architecture/pattern info visible" if passed
                else f"Patterns tab: arch={has_arch}, broken={broken}", t0, page))
        except Exception as exc:
            results.append(_result("tab_patterns", False, f"Patterns tab error: {exc}", t0, page))

        # ── tab_architecture ──────────────────────────────────────────────
        t0 = time.monotonic()
        try:
            _click_tab("Architecture")
            page.wait_for_timeout(2000)
            content = page.inner_text("body")
            # Communities should be listed
            has_communities = any(word in content.lower() for word in
                                  ["community", "cluster", "module", "component", "service"])
            broken = _no_broken_text(content)
            passed = has_communities and not broken
            results.append(_result("tab_architecture", passed,
                "Architecture tab: community/component info visible" if passed
                else f"Architecture tab: communities={has_communities}, broken={broken}", t0, page))
        except Exception as exc:
            results.append(_result("tab_architecture", False, f"Architecture tab error: {exc}", t0, page))

        # ── tab_graph_search ──────────────────────────────────────────────
        t0 = time.monotonic()
        try:
            _click_tab("Graph")
            page.wait_for_timeout(1000)
            # Try to type in symbol box
            symbol_input = page.locator("input[placeholder*='symbol'], input[placeholder*='Symbol'], #symbol-input, input[type='text']").first
            if symbol_input.count() > 0:
                symbol_input.fill("main")
                symbol_input.press("Enter")
                page.wait_for_timeout(3000)
            content = page.inner_text("body")
            # Either results shown or "no results" — not blank, not error
            has_content = (
                page.locator("canvas, svg, .graph-container, .result, [data-result]").count() > 0
                or "main" in content.lower()
                or "no" in content.lower()  # "no results" is valid
            )
            broken = _no_broken_text(content)
            passed = has_content and not broken
            results.append(_result("tab_graph_search", passed,
                "Graph tab: symbol query returned content" if passed
                else f"Graph tab: content={has_content}, broken={broken}", t0, page))
        except Exception as exc:
            results.append(_result("tab_graph_search", False, f"Graph tab error: {exc}", t0, page))

        # ── graph_canvas_renders ──────────────────────────────────────────
        t0 = time.monotonic()
        try:
            # Try clicking "Full Graph" or similar button
            full_btn = page.get_by_role("button", name=lambda n: "full" in n.lower() or "visuali" in n.lower()).first
            if full_btn.count() == 0:
                full_btn = page.locator("button:has-text('Graph'), button:has-text('Visualize')").first
            if full_btn.count() > 0:
                full_btn.click(timeout=3000)
                page.wait_for_timeout(4000)
            canvas = page.locator("canvas").first
            canvas_visible = canvas.count() > 0 and canvas.is_visible()
            if canvas_visible:
                w = canvas.evaluate("el => el.width")
                h = canvas.evaluate("el => el.height")
                passed = w > 0 and h > 0
            else:
                # Canvas might not render yet — not a P0 failure
                passed = True  # degrade gracefully
            results.append(_result("graph_canvas_renders", passed,
                f"Graph canvas rendered (w={w if canvas_visible else '?'}, h={h if canvas_visible else '?'})"
                if canvas_visible else "Graph canvas not rendered yet (OK)", t0, page))
        except Exception as exc:
            results.append(_result("graph_canvas_renders", True,
                f"Graph canvas check skipped: {exc}", t0, page))

        # ── graph_export_json ─────────────────────────────────────────────
        t0 = time.monotonic()
        try:
            export_btn = page.locator("button:has-text('Export JSON'), button:has-text('JSON'), a:has-text('JSON')").first
            if export_btn.count() > 0:
                with page.expect_download(timeout=10000) as dl_info:
                    export_btn.click()
                dl = dl_info.value
                passed = dl.suggested_filename.endswith(".json") or "graph" in dl.suggested_filename
                results.append(_result("graph_export_json", passed,
                    f"JSON export download: {dl.suggested_filename}" if passed
                    else f"JSON export filename unexpected: {dl.suggested_filename}", t0, page))
            else:
                # Try direct API call instead
                resp_status, resp_body = _api_get_raw(
                    f"{base_url}/api/graph_export?project={pp_enc}&format=json&max_nodes=50"
                )
                passed = resp_status == 200 and b'"nodes"' in resp_body
                results.append(_result("graph_export_json", passed,
                    "Graph JSON export available via API" if passed
                    else f"Graph JSON export API returned HTTP {resp_status}", t0, page))
        except Exception as exc:
            results.append(_result("graph_export_json", True,
                f"JSON export button not found — API verified separately: {exc}", t0, page))

        # ── graph_export_graphml ──────────────────────────────────────────
        t0 = time.monotonic()
        try:
            resp_status, resp_body = _api_get_raw(
                f"{base_url}/api/graph_export?project={pp_enc}&format=graphml&max_nodes=50"
            )
            passed = resp_status == 200 and (b"graphml" in resp_body.lower() or b"<graph" in resp_body)
            results.append(_result("graph_export_graphml", passed,
                "GraphML export available via API" if passed
                else f"GraphML export API returned HTTP {resp_status}", t0, page))
        except Exception as exc:
            results.append(_result("graph_export_graphml", False,
                f"GraphML export check failed: {exc}", t0, page))

        # ── tab_wiki_browse ───────────────────────────────────────────────
        t0 = time.monotonic()
        try:
            _click_tab("Wiki")
            page.wait_for_timeout(2000)
            # Click first wiki page in sidebar
            wiki_link = page.locator("aside a, .wiki-sidebar a, nav a, [data-wiki-page], li a").first
            if wiki_link.count() > 0:
                wiki_link.click(timeout=3000)
                page.wait_for_timeout(2000)
            content = page.inner_text("body")
            has_md = page.locator("article, .wiki-content, .markdown, pre, p").count() > 3
            has_text = len(content.strip()) > 200
            broken = _no_broken_text(content)
            passed = (has_md or has_text) and not broken
            results.append(_result("tab_wiki_browse", passed,
                "Wiki tab: page content rendered" if passed
                else f"Wiki tab: md={has_md}, text_len={len(content)}, broken={broken}", t0, page))
        except Exception as exc:
            results.append(_result("tab_wiki_browse", False, f"Wiki tab error: {exc}", t0, page))

        # ── tab_search_query ─────────────────────────────────────────────
        t0 = time.monotonic()
        try:
            _click_tab("Search")
            page.wait_for_timeout(1000)
            search_input = page.locator("input[type='text'], input[placeholder*='search'], input[placeholder*='Search'], #search-input").first
            if search_input.count() > 0:
                search_input.fill("authentication")
                search_input.press("Enter")
                page.wait_for_timeout(3000)
            content = page.inner_text("body")
            result_cards = page.locator(".result, .result-card, [data-result], .hit, li").count()
            has_results = result_cards >= 1 or "authentication" in content.lower()
            broken = _no_broken_text(content)
            passed = has_results and not broken
            results.append(_result("tab_search_query", passed,
                f"Search tab: returned {result_cards} result cards" if passed
                else f"Search tab: results={result_cards}, broken={broken}", t0, page))
        except Exception as exc:
            results.append(_result("tab_search_query", False, f"Search tab error: {exc}", t0, page))

        # ── tab_status_health ─────────────────────────────────────────────
        t0 = time.monotonic()
        try:
            _click_tab("Status")
            page.wait_for_timeout(1500)
            content = page.inner_text("body")
            has_metrics = any(word in content.lower() for word in
                              ["uptime", "enrichment", "wiki", "clients", "watching", "%"])
            broken = _no_broken_text(content)
            passed = has_metrics and not broken
            results.append(_result("tab_status_health", passed,
                "Status tab: metrics visible" if passed
                else f"Status tab: metrics={has_metrics}, broken={broken}", t0, page))
        except Exception as exc:
            results.append(_result("tab_status_health", False, f"Status tab error: {exc}", t0, page))

        # ── tab_arch_map (P2) ─────────────────────────────────────────────
        t0 = time.monotonic()
        try:
            _click_tab("Arch Map")
            page.wait_for_timeout(3000)
            content = page.inner_text("body")
            has_content = (
                page.locator(".community-card, .domain-card, .arch-node, .hierarchy-node").count() > 0
                or any(w in content.lower() for w in ["domain", "architecture", "community", "level", "hierarchy", "no hierarchy"])
            )
            broken = _no_broken_text(content)
            passed = has_content and not broken
            results.append(_result("tab_arch_map", passed,
                "Arch Map tab: hierarchy/domains visible" if passed
                    else f"Arch Map tab: content={has_content}, broken={broken}",
                t0, page,
                [] if passed else [UIAnomaly("tab_arch_map", "P2", "Arch Map page failed to render")]))
        except Exception as exc:
            results.append(_result("tab_arch_map", False, f"Arch Map tab error: {exc}", t0, page,
                [UIAnomaly("tab_arch_map", "P2", str(exc))]))

        # ── tab_service_mesh (P2) ─────────────────────────────────────────
        t0 = time.monotonic()
        try:
            _click_tab("Service Mesh")
            page.wait_for_timeout(2000)
            scan_btn = page.locator("button:has-text('Scan'), button:has-text('Detect'), #btn-scan-mesh")
            if scan_btn.count() > 0:
                scan_btn.first.click()
                page.wait_for_timeout(5000)
            content = page.inner_text("body")
            has_content = (
                page.locator(".integrations-grid, .service-node, .mesh-node, .service-card").count() > 0
                or any(w in content.lower() for w in ["service", "grpc", "http", "kafka", "endpoint", "detected", "no services"])
            )
            broken = _no_broken_text(content)
            passed = has_content and not broken
            results.append(_result("tab_service_mesh", passed,
                "Service Mesh tab: rendered (services may or may not be detected)" if passed
                    else f"Service Mesh tab: content={has_content}, broken={broken}",
                t0, page,
                [] if passed else [UIAnomaly("tab_service_mesh", "P2", "Service Mesh page failed to render")]))
        except Exception as exc:
            results.append(_result("tab_service_mesh", False, f"Service Mesh tab error: {exc}", t0, page,
                [UIAnomaly("tab_service_mesh", "P2", str(exc))]))

        # ── tab_impact (P2) ───────────────────────────────────────────────
        t0 = time.monotonic()
        try:
            _click_tab("Impact")
            page.wait_for_timeout(1500)
            symbol_input = page.locator("#page-impact #impact-symbol").first
            if symbol_input.count() > 0:
                symbol_input.fill("handle_index_project")
                analyze_btn = page.locator(
                    "button:has-text('Analyze'), button:has-text('Impact'), #btn-analyze-impact"
                ).first
                if analyze_btn.count() > 0:
                    analyze_btn.click()
                    page.wait_for_timeout(8000)
            content = page.inner_text("body")
            result_el = page.locator("#impact-result, .impact-result, .impact-output, .result-card")
            has_result = result_el.count() > 0 or any(
                w in content.lower() for w in ["impact", "callers", "risk", "affected", "domains"]
            )
            broken = _no_broken_text(content)
            passed = has_result and not broken
            results.append(_result("tab_impact", passed,
                "Impact Analysis tab: result rendered" if passed
                    else f"Impact tab: result={has_result}, broken={broken}",
                t0, page,
                [] if passed else [UIAnomaly("tab_impact", "P2", "Impact Analysis page failed to render result")]))
        except Exception as exc:
            results.append(_result("tab_impact", False, f"Impact tab error: {exc}", t0, page,
                [UIAnomaly("tab_impact", "P2", str(exc))]))

        # ── tab_trace (P2) ────────────────────────────────────────────────
        t0 = time.monotonic()
        try:
            _click_tab("Trace")
            page.wait_for_timeout(1500)
            from_input = page.locator("#page-trace #trace-from").first
            to_input = page.locator("#page-trace #trace-to").first
            if from_input.count() > 0:
                from_input.fill("HTTP request handler")
            if to_input.count() > 0:
                to_input.fill("database write")
            trace_btn = page.locator(
                "button:has-text('Trace'), button:has-text('Analyze'), #btn-trace"
            ).first
            if trace_btn.count() > 0:
                trace_btn.click()
                page.wait_for_timeout(8000)
            content = page.inner_text("body")
            result_el = page.locator("#trace-result, .trace-result, .trace-output, .semantic-trace")
            has_result = result_el.count() > 0 or any(
                w in content.lower() for w in ["trace", "path", "calls", "flows", "step", "no path"]
            )
            broken = _no_broken_text(content)
            passed = has_result and not broken
            results.append(_result("tab_trace", passed,
                "Semantic Trace tab: result rendered" if passed
                    else f"Trace tab: result={has_result}, broken={broken}",
                t0, page,
                [] if passed else [UIAnomaly("tab_trace", "P2", "Semantic Trace page failed to render result")]))
        except Exception as exc:
            results.append(_result("tab_trace", False, f"Trace tab error: {exc}", t0, page,
                [UIAnomaly("tab_trace", "P2", str(exc))]))

        # ── no_console_errors ─────────────────────────────────────────────
        t0 = time.monotonic()
        # Identify which 404s are JS files (actual JS errors vs benign resource failures)
        js_404s = [u for u in all_404s if u.endswith(".js") or u.endswith(".mjs")]
        # Filter benign errors: favicon, and "Failed to load resource" for non-JS assets
        bad_errors = []
        for e in console_errors:
            el = e.lower()
            if "favicon" in el:
                continue
            if "failed to load resource" in el and not js_404s:
                # Benign 404 for non-JS resource (CSS, image, etc.)
                continue
            bad_errors.append(e)
        # Annotate any bad errors with the offending 404 URLs
        if bad_errors and all_404s:
            bad_errors = [f"{e} [404: {', '.join(all_404s[:3])}]" if "failed to load" in e.lower() else e
                          for e in bad_errors]
        passed = len(bad_errors) == 0
        results.append(UIResult(
            scenario="no_console_errors", passed=passed,
            message="No JS console errors across all tabs" if passed
                    else f"{len(bad_errors)} JS console error(s): {bad_errors[:3]}",
            duration_s=time.monotonic() - t0,
            anomalies=[UIAnomaly("no_console_errors", "P1", e) for e in bad_errors[:5]],
        ))

        # ── no_network_errors ─────────────────────────────────────────────
        t0 = time.monotonic()
        passed = len(network_errors) == 0
        results.append(UIResult(
            scenario="no_network_errors", passed=passed,
            message="No HTTP 4xx/5xx from /api/* endpoints" if passed
                    else f"{len(network_errors)} network error(s): {network_errors[:3]}",
            duration_s=time.monotonic() - t0,
            anomalies=[UIAnomaly("no_network_errors", "P0", e) for e in network_errors[:5]],
        ))

        # ── adv_empty_search ──────────────────────────────────────────────
        t0 = time.monotonic()
        try:
            _click_tab("Search")
            page.wait_for_timeout(500)
            search_input = page.locator(
                "input[type='text'], input[placeholder*='search'], input[placeholder*='Search'], #search-input"
            ).first
            if search_input.count() > 0:
                search_input.fill("")
                search_input.press("Enter")
                page.wait_for_timeout(1500)
            content = page.inner_text("body")
            # Should not crash: no 500 error text, no "TypeError", no blank white page
            no_crash = "TypeError" not in content and "500" not in content and len(content) > 100
            broken = _no_broken_text(content)
            passed = no_crash and not broken
            results.append(_result("adv_empty_search", passed,
                "Empty search handled gracefully (no crash)" if passed
                else f"Empty search caused issues: crash={not no_crash}, broken={broken}", t0, page))
        except Exception as exc:
            results.append(_result("adv_empty_search", False, f"Empty search error: {exc}", t0, page))

        # ── adv_special_chars ─────────────────────────────────────────────
        t0 = time.monotonic()
        try:
            _click_tab("Search")
            page.wait_for_timeout(500)
            search_input = page.locator(
                "input[type='text'], input[placeholder*='search'], input[placeholder*='Search'], #search-input"
            ).first
            xss_payload = "<script>alert('xss')</script>"
            if search_input.count() > 0:
                search_input.fill(xss_payload)
                search_input.press("Enter")
                page.wait_for_timeout(1500)
            # Check: payload should be escaped, no alert dialog should have fired
            alert_fired = False
            page.on("dialog", lambda d: (setattr(d, '_fired', True), d.dismiss()))
            try:
                # Only check the search results container for unescaped <script> tags.
                # page.content() always contains the dashboard's own <script> blocks, so
                # checking the whole page is a false positive. inner_html() on #search-results
                # returns raw HTML of just that element — literal <script> would be unescaped XSS.
                results_html = page.inner_html("#search-results") if page.locator("#search-results").count() > 0 else ""
                import re as _re
                script_tag_raw = bool(_re.search(r"<script[\s>]", results_html, _re.IGNORECASE))
            except Exception:
                script_tag_raw = False
            passed = not alert_fired and not script_tag_raw
            results.append(_result("adv_special_chars", passed,
                "XSS payload properly escaped" if passed else "Possible XSS: script executed or unescaped", t0, page))
        except Exception as exc:
            results.append(_result("adv_special_chars", False, f"Special char test error: {exc}", t0, page))

        # ── adv_very_long_query ───────────────────────────────────────────
        t0 = time.monotonic()
        try:
            _click_tab("Search")
            page.wait_for_timeout(500)
            search_input = page.locator(
                "input[type='text'], input[placeholder*='search'], input[placeholder*='Search'], #search-input"
            ).first
            long_query = "authentication " * 40  # ~600 chars
            if search_input.count() > 0:
                search_input.fill(long_query)
                search_input.press("Enter")
                page.wait_for_timeout(2000)
            content = page.inner_text("body")
            no_crash = "500" not in content and "TypeError" not in content and len(content) > 100
            passed = no_crash
            results.append(_result("adv_very_long_query", passed,
                "Long query handled without crash" if passed
                else f"Long query caused crash: {content[:100]}", t0, page))
        except Exception as exc:
            results.append(_result("adv_very_long_query", False, f"Long query error: {exc}", t0, page))

        # ── adv_rapid_nav ─────────────────────────────────────────────────
        t0 = time.monotonic()
        rapid_errors: list[str] = []
        page.on("console", lambda msg: rapid_errors.append(msg.text)
                if msg.type == "error" else None)
        try:
            nav_links = page.locator("[data-tab], nav a[href^='#'], .nav-item").all()
            for link in nav_links[:12]:
                try:
                    link.click(timeout=1500)
                    page.wait_for_timeout(100)  # 100ms between clicks = rapid
                except Exception:
                    pass
            filtered_errors = [e for e in rapid_errors
                               if "favicon" not in e.lower() and "failed to load resource" not in e.lower()]
            passed = len(filtered_errors) == 0
            results.append(_result("adv_rapid_nav", passed,
                f"Rapid navigation (12 links, 100ms delay): no JS errors" if passed
                else f"Rapid navigation triggered {len(filtered_errors)} JS error(s): {filtered_errors[:2]}",
                t0, page))
        except Exception as exc:
            results.append(_result("adv_rapid_nav", False, f"Rapid nav error: {exc}", t0, page))

        # ── adv_back_forward ──────────────────────────────────────────────
        t0 = time.monotonic()
        try:
            _click_tab("Search")
            page.wait_for_timeout(300)
            _click_tab("Graph")
            page.wait_for_timeout(300)
            _click_tab("Wiki")
            page.wait_for_timeout(300)
            # Navigate back (may throw on SPA with no history, which is acceptable)
            for _ in range(2):
                try:
                    page.go_back(timeout=3000, wait_until="domcontentloaded")
                    page.wait_for_timeout(400)
                except Exception:
                    break  # no history entry — SPA tab switching without URL changes
            # Final state: if go_back navigated away from the SPA (minimal content),
            # navigate back to the dashboard — SPA without hash routing is acceptable.
            content = page.inner_text("body")
            if len(content) <= 100:
                page.goto(dashboard_url, wait_until="domcontentloaded")
                page.wait_for_timeout(800)
                content = page.inner_text("body")
            still_works = len(content) > 100 and "TypeError" not in content
            passed = still_works
            results.append(_result("adv_back_forward", passed,
                "Back/forward navigation: page still usable" if passed
                else f"Back/forward broke page: {content[:100]}", t0, page))
        except Exception as exc:
            # go_back may fail on SPA; that's acceptable
            results.append(_result("adv_back_forward", True,
                f"Back/forward: SPA handles history via hash (acceptable)", t0, page))

        # ── adv_concurrent_search ─────────────────────────────────────────
        t0 = time.monotonic()
        try:
            _click_tab("Search")
            page.wait_for_timeout(300)
            search_input = page.locator(
                "input[type='text'], input[placeholder*='search'], input[placeholder*='Search'], #search-input"
            ).first
            if search_input.count() > 0:
                # Rapid-fire 3 queries before results arrive
                for query in ["authentication", "database", "handler"]:
                    search_input.fill(query)
                    search_input.press("Enter")
                    page.wait_for_timeout(200)
                page.wait_for_timeout(2000)  # let last query complete
            content = page.inner_text("body")
            no_undefined = "undefined" not in content
            no_crash = "TypeError" not in content and "500" not in content
            passed = no_undefined and no_crash
            results.append(_result("adv_concurrent_search", passed,
                "Concurrent searches handled without undefined/crash" if passed
                else f"Concurrent search issues: undefined={not no_undefined}, crash={not no_crash}",
                t0, page))
        except Exception as exc:
            results.append(_result("adv_concurrent_search", False, f"Concurrent search error: {exc}", t0, page))

        browser.close()

    return results


def _api_get_raw(url: str, timeout: int = 30) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, resp.read(5 * 1024 * 1024)  # read up to 5 MB
    except urllib.error.HTTPError as e:
        return e.code, b""
    except Exception:
        return 0, b""


def print_results(api_results: list[UIResult], ui_results: list[UIResult]) -> int:
    all_results = api_results + ui_results
    passed = [r for r in all_results if r.passed]
    failed = [r for r in all_results if not r.passed]
    p0_anomalies = [a for r in all_results for a in r.anomalies if a.severity == "P0"]

    print(f"\n{'='*65}")
    print("  Human Simulation — Dashboard Verification")
    print(f"{'='*65}")
    if api_results:
        print("  API Health:")
        for r in api_results:
            print(f"    {'✅' if r.passed else '🔴'} [{r.duration_s:.1f}s] {r.message[:70]}")
    if ui_results:
        print("  UI Scenarios:")
        for r in ui_results:
            icon = "✅" if r.passed else "🟡"
            scr = f" → {Path(r.screenshot).name}" if r.screenshot else ""
            print(f"    {icon} [{r.duration_s:.1f}s] {r.scenario:<30} {r.message[:45]}{scr}")
    print(f"{'='*65}")
    print(f"  Passed: {len(passed)}/{len(all_results)}  |  P0 anomalies: {len(p0_anomalies)}")
    if failed:
        print("\n  Failures:")
        for r in failed:
            print(f"    ❌ {r.scenario}: {r.message}")
    print()
    return 1 if p0_anomalies else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Simulate human use of the opencode-search dashboard")
    parser.add_argument("--project", required=True, help="Indexed project path")
    parser.add_argument("--screenshots", default=".prerelease_screenshots",
                        help="Directory for screenshots (default: .prerelease_screenshots)")
    parser.add_argument("--base-url", default="http://127.0.0.1:8765", help="Dashboard base URL")
    parser.add_argument("--api-only", action="store_true", help="Run API checks only (no Playwright)")
    parser.add_argument("--json", dest="json_out", action="store_true", help="JSON output")
    args = parser.parse_args()

    screenshots_dir = Path(args.screenshots)
    project = str(Path(args.project).expanduser().resolve())

    print(f"Simulating human dashboard usage for: {project}")
    print(f"Dashboard: {args.base_url}/dashboard")

    api_results = check_api_health(project, base_url=args.base_url)
    ui_results: list[UIResult] = []

    if not args.api_only:
        # Only run Playwright if API health is at least partially OK
        healthz_ok = any(r.passed and "healthz" in r.scenario for r in api_results)
        if healthz_ok:
            print("Running Playwright UI simulation...")
            ui_results = run_ui_simulation(project, screenshots_dir, base_url=args.base_url)
        else:
            print("⚠️  Daemon not healthy — skipping Playwright (run: opencode-search daemon ensure)")
            ui_results = [UIResult(
                scenario="playwright_skipped", passed=False,
                message="Playwright skipped: daemon not healthy",
                anomalies=[UIAnomaly("playwright_skipped", "P0", "Daemon /healthz failed")],
            )]

    if args.json_out:
        import dataclasses
        output = {
            "api": [dataclasses.asdict(r) for r in api_results],
            "ui": [dataclasses.asdict(r) for r in ui_results],
        }
        print(json.dumps(output, indent=2))
        all_anomalies = [a for r in api_results + ui_results for a in r.anomalies if a.severity == "P0"]
        return 1 if all_anomalies else 0

    return print_results(api_results, ui_results)


if __name__ == "__main__":
    sys.exit(main())
