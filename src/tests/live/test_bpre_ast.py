"""Gap 4 Tier-1: tree-sitter scanner unit tests for kb/bpre_ast — no daemon needed."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.live

_GO_PB = (
    "package cartpb\n"
    "func NewCartServiceClient(cc *grpc.ClientConn) CartServiceClient { return nil }\n"
    "func RegisterCartServer(s *grpc.Server, srv CartServer) {}\n"
)

_GO_CALLER = (
    "package main\n"
    "import pbCart \"github.com/x/cart/cartpb\"\n"
    "func init(conn *grpc.ClientConn) { pbCart.NewCartServiceClient(conn) }\n"
)

_GO_COMMENT = (
    "package noop\n"
    "// NewFakeServiceClient and RegisterFakeServer are only in this comment.\n"
    "// topic.Publish(ctx, &pubsub.Message{Data: data}) — docs only.\n"
    "func NoOp() {}\n"
)

_JAVA_ROUTE = (
    "package com.example;\n"
    "import org.springframework.web.bind.annotation.GetMapping;\n"
    "import org.springframework.web.bind.annotation.PostMapping;\n"
    "public class CartController {\n"
    "    @GetMapping(\"/cart\") public Object get() { return null; }\n"
    "    @PostMapping(\"/cart/add\") public Object add() { return null; }\n"
    "}\n"
)

_JAVA_COMMENT = (
    "package com.example;\n"
    "public class Docs {\n"
    "    // @GetMapping(\"/fake\") and @PostMapping(\"/noop\") — docs only\n"
    "    public void noAnnotation() {}\n"
    "}\n"
)


# ─── Pass A: *.pb.go gRPC surface discovery via federation_discover ──────────

def test_pass_a_discovers_constructor_and_registrar():
    """Pass A: federation_discover populates constructors/registrars from *.pb.go."""
    from opencode_search.kb.bpre_ast import federation_discover

    with tempfile.TemporaryDirectory() as td:
        (Path(td) / "cart.pb.go").write_text(_GO_PB)
        surf = federation_discover([td])

    assert any("Cart" in v for v in surf.constructors.values()), (
        "Pass A must discover CartService from NewCartServiceClient"
    )
    assert any(fn for fn in surf.registrars if "Register" in fn), (
        "Pass A must register RegisterCartServer as a registrar"
    )


def test_pass_a_no_hardcoded_service_name():
    """Pass A extracts service names from constructor patterns — not a static dict."""
    from opencode_search.kb.bpre_ast import federation_discover

    novel = _GO_PB.replace("Cart", "Inventory")
    with tempfile.TemporaryDirectory() as td:
        (Path(td) / "inv.pb.go").write_text(novel)
        surf = federation_discover([td])

    assert any("Inventory" in v for v in surf.constructors.values()), (
        "Pass A must discover novel service names without hardcoded mapping"
    )


# ─── Pass B: per-file scan_file ───────────────────────────────────────────────

def test_pass_b_detects_grpc_client():
    """Pass B: scan_file detects pbCart.NewCartServiceClient as a gRPC client."""
    from opencode_search.kb.bpre_ast import ApiSurface, scan_file

    surf = ApiSurface()
    surf.constructors["NewCartServiceClient"] = "CartService"
    ff = scan_file("main.go", _GO_CALLER, "go", surf)
    assert ff is not None
    assert ff.grpc_clients, (
        "Pass B must detect NewCartServiceClient as a gRPC client reference"
    )


def test_pass_b_detects_spring_routes():
    """Pass B: scan_file detects @GetMapping/@PostMapping as HTTP routes."""
    from opencode_search.kb.bpre_ast import ApiSurface, scan_file

    surf = ApiSurface()
    ff = scan_file("CartController.java", _JAVA_ROUTE, "java", surf)
    assert ff is not None
    paths = [r[1] for r in ff.http_routes]
    assert "/cart" in paths or any("/cart" in p for p in paths), (
        f"Pass B must detect @GetMapping('/cart'); found: {paths}"
    )


# ─── AST-over-regex proof ─────────────────────────────────────────────────────

def test_go_comment_tokens_not_matched():
    """Tokens appearing only in Go comments must NOT be registered (AST, not regex)."""
    from opencode_search.kb.bpre_ast import federation_discover

    with tempfile.TemporaryDirectory() as td:
        (Path(td) / "noop.pb.go").write_text(_GO_COMMENT)
        surf = federation_discover([td])

    assert "NewFakeServiceClient" not in surf.constructors, (
        "Comment-only constructor must NOT be registered — AST-over-regex proof"
    )


def test_java_comment_routes_not_matched():
    """Spring annotations inside Java comments must NOT produce HTTP routes."""
    from opencode_search.kb.bpre_ast import ApiSurface, scan_file

    surf = ApiSurface()
    ff = scan_file("Docs.java", _JAVA_COMMENT, "java", surf)
    assert ff is None or not ff.http_routes, (
        "Comment-only @GetMapping/@PostMapping must NOT produce routes — AST-over-regex proof"
    )
