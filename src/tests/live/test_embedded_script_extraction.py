"""F2: embedded <script> sub-parsing — Vue/Svelte SFC symbols and calls.

Both grammars parse <script> as an opaque `raw_text` leaf; graph/extractor.py sub-parses that
leaf with the js/ts grammar and remaps line numbers. kb/bpre_ast.py did the same thing for BPRE's
HTTP-client surface and left with tier 3, taking the two http_clients cases below with it — the
remapping contract they shared is still covered by the symbol and call-line cases here.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.live

_VUE_TS = (
    "<template>\n"
    "  <div>{{ msg }}</div>\n"
    "</template>\n"
    "\n"
    "<script lang=\"ts\">\n"
    "function greet(name: string) {\n"
    "  console.log(\"hi\", name);\n"
    "  return fetchData(name);\n"
    "}\n"
    "\n"
    "function fetchData(name: string) {\n"
    "  return fetch(\"/api/data/\" + name);\n"
    "}\n"
    "</script>\n"
)

_SVELTE_JS = (
    "<script>\n"
    "function add(a, b) {\n"
    "  return sum(a, b);\n"
    "}\n"
    "\n"
    "function sum(a, b) {\n"
    "  return a + b;\n"
    "}\n"
    "</script>\n"
    "\n"
    "<div>hello</div>\n"
)

def test_vue_script_symbols_have_correct_line_offset():
    """extract_symbols must find inner <script lang=ts> functions with file-relative lines."""
    from rag_search.graph.extractor import extract_symbols

    syms = extract_symbols(Path("Test.vue"), _VUE_TS, "vue")
    by_name = {s.name: s for s in syms}
    assert {"greet", "fetchData"} <= by_name.keys(), (
        f"expected inner script functions, got {list(by_name)}"
    )
    assert by_name["greet"].start_line == 6
    assert by_name["fetchData"].start_line == 11
    assert by_name["greet"].language == "typescript"


def test_vue_script_calls_have_correct_line_offset():
    from rag_search.graph.extractor import extract_calls_with_lines

    calls = dict(extract_calls_with_lines(_VUE_TS, "vue"))
    assert calls.get("fetchData") == 8
    assert calls.get("fetch") == 12


def test_vue_call_sites_continue_order_index():
    from rag_search.graph.extractor import extract_call_sites

    sites = extract_call_sites(_VUE_TS, "vue")
    names = [s.callee_name for s in sites]
    assert names == ["log", "fetchData", "fetch"]
    assert [s.order_index for s in sites] == [0, 1, 2]


def test_svelte_script_symbols_and_calls():
    from rag_search.graph.extractor import extract_calls_with_lines, extract_symbols

    syms = extract_symbols(Path("Test.svelte"), _SVELTE_JS, "svelte")
    by_name = {s.name: s for s in syms}
    assert {"add", "sum"} <= by_name.keys()
    assert by_name["add"].start_line == 2
    assert by_name["sum"].start_line == 6
    assert by_name["add"].language == "javascript"

    calls = dict(extract_calls_with_lines(_SVELTE_JS, "svelte"))
    assert calls.get("sum") == 3


def test_html_files_are_not_code_language():
    """Regression guard: plain .html must stay text-classified so this change never fires on it."""
    from rag_search.index.discover import is_code_language

    assert is_code_language("html") is False
    assert is_code_language("vue") is True
    assert is_code_language("svelte") is True
