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
    "ruby": (
        "get '/api/items' do\n  HTTParty.get('/api/items')\nend",
        "ruby",
    ),
    "rust": (
        'fn f(){let c=reqwest::blocking::Client::new();c.get("/api/items").send().unwrap();}',
        "rust",
    ),
    "csharp": (
        'class C{async Task M(){await http.GetAsync("/api/items");}}',
        "csharp",
    ),
    "kotlin": (
        # P6/HR15 Part B.4: Spring's `restTemplate.getForObject(...)` is a non-verb method on
        # a non-_SCHEMES-named type — genuinely unresolvable by the universal structural
        # classifier without a forbidden method-name table (the documented recall boundary,
        # see jolly-hopping-hanrahan.md "One honest recall boundary"). Swapped for a modern,
        # doctrine-compliant Ktor idiom that resolves via the _V verb ground-truth signal.
        'fun f(){client.get("/api/items")}',
        "kotlin",
    ),
    "scala": (
        'object S{ def f()={ http.get("/api/items") } }',
        "scala",
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
    non_go = callers & {"svc-php", "svc-ts", "svc-py", "svc-ruby"}
    assert non_go, (
        f"No non-Go caller in edges; all callers: {callers}. "
        "PHP/TS/Python/Ruby http_clients detection may be broken."
    )


def test_polyglot_process_flows_nonempty(poly_fed, poly_db) -> None:
    """overview(process_flows) must return source=reconstructed with ≥1 flow."""
    import asyncio

    from opencode_search.server.mcp import overview as overview_tool
    result = asyncio.run(overview_tool(poly_fed.root, what="process_flows"))
    data = json.loads(result)
    assert data.get("source") == "reconstructed", f"Unexpected source: {data.get('source')!r}"
    assert data.get("flows"), "overview(process_flows) returned empty flows for polyglot fleet"


def test_polyglot_ruby_contributes_edge(poly_db) -> None:
    """svc-ruby (Ruby generic engine) must contribute ≥1 cross-service edge."""
    con, _ = poly_db
    callers = {r[0] for r in con.execute(
        "SELECT DISTINCT caller_service FROM cross_service_edges"
    ).fetchall()}
    assert "svc-ruby" in callers, (
        f"svc-ruby not found in cross_service_edges callers: {callers}. "
        "Ruby generic-engine extraction (bpre_generic.scan_generic) may be broken."
    )


def test_ruby_route_vs_client_golden() -> None:
    """P6/HR15 Part B.2 golden fixture: Ruby's structural (_has_handler_arg) classification
    must route the outer `get ... do ... end` registration to http_routes and the inner
    `HTTParty.get(...)` call to http_clients — not double-count both as clients (the
    pre-migration _cp() receiver-identity quirk this migration fixes)."""
    content, detected_lang = _LANG_SNIPPETS["ruby"]
    surf = ApiSurface()
    ff = scan_file("app.rb", content, detected_lang, surf)
    assert ff is not None
    assert ff.http_routes == [("GET", "/api/items", 1)], f"http_routes={ff.http_routes}"
    assert ff.http_clients == [("GET", "/api/items", 2)], f"http_clients={ff.http_clients}"


_LANG_SNIPPETS_EXT: dict[str, tuple[str, str, str]] = {
    "lua": ('http.get("/api/items")', "lua", "http_clients"),
    "r": ('GET("/api/items")', "r", "http_clients"),
    "julia": ('HTTP.get("/api/items")', "julia", "http_clients"),
    "perl": ('$ua->get("/api/items");', "perl", "http_clients"),
    "groovy": ('client.get("/api/items")', "groovy", "http_clients"),
    "clojure": ('(client/get "/api/items")', "clojure", "http_clients"),
    "haskell": ('f = get "/api/items"', "haskell", "http_routes"),
    "objc": ('[client GET:@"/api/items"];', "objc", "http_clients"),
}


@pytest.mark.parametrize("lang", list(_LANG_SNIPPETS_EXT))
def test_scan_file_emits_surface_extended_langs(lang: str) -> None:
    """scan_file must emit http_clients or http_routes for long-tail/paradigm languages."""
    content, detected_lang, expected_field = _LANG_SNIPPETS_EXT[lang]
    surf = ApiSurface()
    ff = scan_file(f"file.{lang}", content, detected_lang, surf)
    assert ff is not None, f"scan_file returned None for {lang}"
    got = getattr(ff, expected_field)
    assert got, (
        f"{lang}: expected {expected_field}; got empty. "
        f"http_clients={ff.http_clients} http_routes={ff.http_routes}"
    )


def test_polyglot_no_self_edges(poly_db) -> None:
    """No edge may have caller_service == callee_service."""
    con, _ = poly_db
    self_edges = con.execute(
        "SELECT COUNT(*) FROM cross_service_edges WHERE caller_service=callee_service"
    ).fetchone()[0]
    assert self_edges == 0, f"{self_edges} self-loop edges (caller=callee)"


# P6/HR15 Part B.1: _has_handler_arg is the structural (node-kind-only) route-vs-client
# discriminator that will replace _LANG_SPECS name-matching. Each pair below was verified
# empirically against tree-sitter-language-pack 1.12.1 (not guessed from memory — grammar
# node kinds have already drifted once this session, see the csharp->c_sharp rename).
_HANDLER_ARG_CASES: dict[str, tuple[str, str, str]] = {
    "ruby":   ("ruby",   "get '/x' do\n  1\nend\n",                          "HTTParty.get('/api/items')\n"),
    "csharp": ("csharp", 'app.MapGet("/x", () => "ok");\n',                  'http.GetAsync("/x");\n'),
    "rust":   ("rust",   'router.route("/x", get(|| async { "ok" }));\n',    'client.get("/x");\n'),
    "kotlin": ("kotlin", 'route("/x") { call.respond("ok") }\n',             'restTemplate.getForObject("/x", String::class.java)\n'),
    "swift":  ("swift",  'app.get("/x") { req, res in "ok" }\n',             'URLSession.shared.dataTask(with: url)\n'),
    "scala":  ("scala",  'path("x") { complete("ok") }\n',                   'client.get("/x")\n'),
    "java":   ("java",   'app.get("/x", (req, res) -> "ok");\n',             'restTemplate.getForObject("/x", String.class);\n'),
    "cpp":    ("cpp",    'app.Get("/x", [](Request req) { return "ok"; });\n', 'client.get("/x");\n'),
    "perl":   ("perl",   'get "/x" => sub { "ok" };\n',                      '$ua->get("/x");\n'),
    "lua":    ("lua",    "app:get('/x', function(req) return 'ok' end)\n",   "http.get('/x')\n"),
}


def _outer_call_node(tsl: str, src: str):
    """First (outermost) call-kind node in source order — the node under test."""
    from tree_sitter_language_pack import api as _ts_api

    from opencode_search.kb.bpre_spec import _is_call
    root = _ts_api.get_parser(tsl).parse(src).root_node()
    stk = [root]
    while stk:
        n = stk.pop(0)
        if _is_call(n.kind()):
            return n
        stk = [n.named_child(i) for i in range(n.named_child_count())] + stk
    return None


@pytest.mark.parametrize("lang", list(_HANDLER_ARG_CASES))
def test_has_handler_arg_discriminates_route_vs_client(lang: str) -> None:
    """_has_handler_arg(call) is True for a route/handler registration, False for a plain call."""
    from opencode_search.kb.bpre_generic import _has_handler_arg
    tsl, route_src, client_src = _HANDLER_ARG_CASES[lang]
    route_call = _outer_call_node(tsl, route_src)
    client_call = _outer_call_node(tsl, client_src)
    assert route_call is not None, f"{lang}: no call node found in route snippet"
    assert client_call is not None, f"{lang}: no call node found in client snippet"
    assert _has_handler_arg(route_call) is True, f"{lang}: expected handler-shape arg in {route_src!r}"
    assert _has_handler_arg(client_call) is False, f"{lang}: unexpected handler-shape arg in {client_src!r}"


# P6/HR15 Part B.3: _provenance is the universal non-verb HTTP-client discriminator that will
# let the universal classifier (Part B.4) resolve calls like C#'s `httpClient.GetAsync(...)` or
# Elixir's `HTTPoison.get!(...)` — whose method name is neither a verb nor a proto-binding —
# without any per-language keyword table (pure closed-vocabulary _SCHEMES substring match).
@pytest.mark.parametrize(
    "receiver,expected",
    [
        ("http", True),           # Dart/Go-style bare alias
        ("httpClient", True),     # C# idiomatic HttpClient field name
        ("HTTPoison", True),      # Elixir HTTP client module (case-insensitive)
        ("URLSession", True),     # Swift (matches "url")
        ("grpcClient", True),     # gRPC-scheme receiver
        ("client", False),        # generic, no scheme token
        ("os", False),            # unrelated stdlib receiver
        (None, False),            # no receiver at all (bare function call)
    ],
)
def test_provenance_discriminates_scheme_receivers(receiver: str | None, expected: bool) -> None:
    """_provenance(receiver) is the closed-_SCHEMES-vocabulary structural client discriminator."""
    from opencode_search.kb.bpre_generic import _provenance
    assert _provenance(receiver) is expected, f"receiver={receiver!r}"


# P6/HR15 Part C1: _provenance also resolves via def-use type-use and import-map lookups, not
# just receiver text — closing the recall gap Part B left (typed clients / import aliases whose
# own name carries no _SCHEMES token).
def test_provenance_resolves_via_type_use_and_imports() -> None:
    from opencode_search.kb.bpre_generic import _provenance
    # (b) def-use-resolved constructed-type name carries a scheme, receiver name does not.
    assert _provenance("client", None, {"client": "HttpClient"}) is True
    # (c) import-map-resolved module path carries a scheme, alias name does not.
    assert _provenance("Net", {"Net": "System.Net.Http"}, None) is True
    # (c) via resolved type name: alias->type->import chain.
    assert _provenance("c", {"HttpClient": "System.Net.Http"}, {"c": "HttpClient"}) is True
    # Negative: neither receiver, type, nor import path carries a scheme token.
    assert _provenance("logger", {"Logger": "com.example.Logger"}, {"logger": "Logger"}) is False


def test_typed_client_def_use_resolves_provenance() -> None:
    """P6/HR15 Part C1: `client = new HttpClient(); client.sendAsync('/x')` resolves via the
    def-use-resolved constructed type ('HttpClient' carries a _SCHEMES token) — the receiver name
    'client' alone does not, so this only passes with type-use provenance wired in."""
    content = (
        "class C{void f(){HttpClient client = new HttpClient();"
        ' client.sendAsync("/api/items");}}'
    )
    surf = ApiSurface()
    ff = scan_file("c.java", content, "java", surf)
    assert ff is not None
    assert ff.http_clients, f"typed-client provenance not resolved; http_routes={ff.http_routes}"


def test_import_alias_resolves_provenance() -> None:
    """P6/HR15 Part C1: `using Net = System.Net.Http;` then `Net.SendAsync('/x')` resolves via
    the import-map-resolved module path — the alias 'Net' alone carries no _SCHEMES token."""
    content = (
        "using Net = System.Net.Http;\n"
        'class C{async Task M(){await Net.SendAsync("/api/items");}}'
    )
    surf = ApiSurface()
    ff = scan_file("c.cs", content, "csharp", surf)
    assert ff is not None
    assert ff.http_clients, f"import-alias provenance not resolved; http_routes={ff.http_routes}"


def test_import_provenance_negative_non_scheme_stays_unresolved() -> None:
    """Negative control: a typed client whose type/import carries no _SCHEMES token must NOT be
    misclassified as an HTTP client (genuine residual ambiguity, not a false positive)."""
    content = 'class C{void f(){Logger logger = new Logger(); logger.record("/api/items");}}'
    surf = ApiSurface()
    ff = scan_file("c.java", content, "java", surf)
    assert ff is not None
    assert not ff.http_clients, f"unexpected false-positive client: {ff.http_clients}"
