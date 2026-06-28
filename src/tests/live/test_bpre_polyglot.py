"""Multi-language BPRE caller-side support — regression guard + integration.

Ensures scan_file emits http_clients/grpc_clients for go/php/python/typescript/javascript
and that reconstruct_processes finds cross-service edges in a synthetic polyglot fleet.
No real device paths. All assertions are structural (no LLM calls).
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path

import pytest

from opencode_search.core.config import root_process_db
from opencode_search.kb.bpre import reconstruct_processes
from opencode_search.kb.bpre_ast import ApiSurface, federation_discover, scan_file

pytestmark = pytest.mark.live

_LANG_SNIPPETS: dict[str, tuple[str, str]] = {
    "go": (
        'package main\nimport "net/http"\n'
        'func f() { http.NewRequest("GET", "/api/items", nil) }',
        "go",
    ),
    "php": (
        "<?php\n$http = new GuzzleHttp\\Client();\n$http->get('/api/items');",
        "php",
    ),
    "python": (
        "import requests\ndef f():\n    return requests.get('/api/items')",
        "python",
    ),
    "typescript": (
        "async function f() { return fetch('/api/items'); }",
        "typescript",
    ),
    "javascript": (
        "async function f() { return fetch('/api/items'); }",
        "javascript",
    ),
}


@pytest.mark.parametrize("lang", list(_LANG_SNIPPETS))
def test_scan_file_emits_http_clients(lang: str) -> None:
    """scan_file must populate http_clients for every supported language."""
    content, detected_lang = _LANG_SNIPPETS[lang]
    surf = ApiSurface()
    ff = scan_file(f"file.{lang}", content, detected_lang, surf)
    assert ff is not None, f"scan_file returned None for {lang}"
    assert ff.http_clients, (
        f"{lang}: expected http_clients; got empty. http_routes={ff.http_routes}"
    )


def test_scan_file_php_grpc_client() -> None:
    """PHP: new OrderServiceClient($ch) where OrderService ∈ proto_services → grpc_clients."""
    surf = ApiSurface()
    surf.proto_services.add("OrderService")
    ff = scan_file("c.php", "<?php\n$c = new OrderServiceClient($ch);", "php", surf)
    assert ff and ff.grpc_clients, "PHP grpc_clients not detected"
    assert "OrderService" in [g[1] for g in ff.grpc_clients]


def test_federation_discover_reads_proto_services() -> None:
    """federation_discover must populate proto_services from .proto files."""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "svc.proto"
        p.write_text('syntax = "proto3";\nservice FooService {\n  rpc Bar() returns ();\n}\n')
        surf = federation_discover([d])
    assert "FooService" in surf.proto_services, (
        "proto service 'FooService' not found in surf.proto_services — "
        "federation_discover proto-file parsing broken"
    )


@pytest.fixture(scope="module")
def poly_fed():
    from tests.live._bpre_fixture import (
        build_polyglot_federation,
        teardown_polyglot_federation,
    )
    fed = build_polyglot_federation()
    yield fed
    teardown_polyglot_federation(fed)


@pytest.fixture(scope="module")
def poly_db(poly_fed):
    from opencode_search.graph.llm import no_deepseek
    with no_deepseek():
        count = reconstruct_processes(poly_fed.root)
    db = root_process_db(poly_fed.root)
    assert db.exists(), "process_graph.db must be created"
    con = sqlite3.connect(str(db))
    yield con, count
    con.close()


def test_polyglot_cross_service_edges_nonzero(poly_db) -> None:
    """At least 1 cross-service edge must be found in the polyglot fleet."""
    con, _ = poly_db
    total = con.execute("SELECT COUNT(*) FROM cross_service_edges").fetchone()[0]
    assert total > 0, (
        "reconstruct_processes found 0 cross_service_edges in polyglot fleet — "
        "check PHP/TS/Python caller-side extraction"
    )


def test_polyglot_non_go_member_contributes_edge(poly_db) -> None:
    """At least 1 edge must have a PHP, TS, or Python member as caller."""
    con, _ = poly_db
    callers = {r[0] for r in con.execute(
        "SELECT DISTINCT caller_service FROM cross_service_edges"
    ).fetchall()}
    non_go = callers & {"svc-php", "svc-ts", "svc-py"}
    assert non_go, (
        f"No non-Go caller in edges; all callers: {callers}. "
        "PHP/TS/Python http_clients detection may be broken."
    )


def test_polyglot_process_flows_nonempty(poly_fed, poly_db) -> None:
    """overview(process_flows) must return source=reconstructed with ≥1 flow."""
    import asyncio

    from opencode_search.server.mcp import overview as overview_tool
    result = asyncio.run(overview_tool(poly_fed.root, what="process_flows"))
    data = json.loads(result)
    assert data.get("source") == "reconstructed", f"Unexpected source: {data.get('source')!r}"
    assert data.get("flows"), "overview(process_flows) returned empty flows for polyglot fleet"


def test_polyglot_no_self_edges(poly_db) -> None:
    """No edge may have caller_service == callee_service."""
    con, _ = poly_db
    self_edges = con.execute(
        "SELECT COUNT(*) FROM cross_service_edges WHERE caller_service=callee_service"
    ).fetchone()[0]
    assert self_edges == 0, f"{self_edges} self-loop edges (caller=callee)"
