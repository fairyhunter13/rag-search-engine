"""Synthetic 2-service Go gRPC federation fixture for BPRE isolation.

Builds a minimal but real multi-service mesh under ~/.local/share/ocs-test-dirs:
  svc-cart   — cart.pb.go with NewCartServiceClient / RegisterCartServiceServer / GetCart
  svc-checkout — checkout.go with HTTP route + NewCartServiceClient(conn).GetCart(...) call

Both members get a GPU-free graph.db via _rederive_graph.  A root dir ties them together.

Usage (module-scope fixture):
    from tests.live._bpre_fixture import bpre_root_fixture
    @pytest.fixture(scope="module")
    def synth_root(tmp_path_factory):
        return bpre_root_fixture(tmp_path_factory)
"""
from __future__ import annotations

import contextlib
import shutil
import tempfile
from collections.abc import Generator
from pathlib import Path

import pytest

from opencode_search.core.config import ProjectEntry, project_graph_db
from opencode_search.core.registry import remove_project, upsert_project
from opencode_search.graph.store import GraphStore

_SAFE_BASE = Path.home() / ".local" / "share" / "ocs-test-dirs"

_CART_PB_GO = '''\
package cart

import "google.golang.org/grpc"

type CartServiceClient interface{ GetCart(string) string }
type cartServiceClient struct{ cc *grpc.ClientConn }

func NewCartServiceClient(cc *grpc.ClientConn) CartServiceClient {
    return &cartServiceClient{cc}
}
func (c *cartServiceClient) GetCart(userID string) string { return "" }

type CartServiceServer interface{ GetCart(string) string }

func RegisterCartServiceServer(s *grpc.Server, srv CartServiceServer) {}
'''

_CART_SERVER_GO = '''\
package main

import (
	"google.golang.org/grpc"
	"example.com/svc-cart/cart"
)

type cartSvr struct{ cart.CartServiceServer }

func main() {
	s := grpc.NewServer()
	cart.RegisterCartServiceServer(s, &cartSvr{})
}
'''

_CHECKOUT_GO = '''\
package checkout

import (
	"net/http"
	"google.golang.org/grpc"
	"example.com/svc-cart/cart"
)

// Register wires the checkout HTTP route and calls the cart gRPC service.
// Both http.HandleFunc and cart.NewCartServiceClient are in this function so
// _rederive_graph maps them to the same symbol SID — making _call_in_reachable
// trivially pass even in the minimal synthetic fixture (no call-edge enrichment).
func Register(conn *grpc.ClientConn) {
	http.HandleFunc("/checkout", func(w http.ResponseWriter, r *http.Request) {
		cart.NewCartServiceClient(conn).GetCart(r.URL.Query().Get("user"))
	})
}
'''


def _make_member(base: Path, name: str, src_files: dict[str, str],
                 stype: str) -> str:
    """Create a member dir with source files + GPU-free graph.db + one L2 community."""
    d = base / name
    d.mkdir(parents=True)
    for fname, content in src_files.items():
        p = d / fname
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)

    # Build graph.db via GPU-free _rederive_graph.
    upsert_project(ProjectEntry(path=str(d), enabled=True))
    try:
        from opencode_search.daemon.sweeps import _rederive_graph
        _rederive_graph(str(d))
    except Exception:
        pass  # graph.db may be empty on rare parse failure; tests tolerate

    return str(d)


def _make_root(base: Path, members: list[str]) -> str:
    root = base / "root"
    root.mkdir()
    upsert_project(ProjectEntry(path=str(root), enabled=True, federation=members))
    gdb = project_graph_db(str(root))
    gdb.parent.mkdir(parents=True, exist_ok=True)
    GraphStore(gdb).close()
    return str(root)


_ORDERS_PROTO = """\
syntax = "proto3";
package orders;
service OrderService {
  rpc GetOrder (GetOrderRequest) returns (GetOrderResponse);
}
message GetOrderRequest { string id = 1; }
message GetOrderResponse { string status = 1; }
"""
_ORDER_PB_GO = """\
package orders
import "google.golang.org/grpc"
type OrderServiceClient interface{ GetOrder(string) string }
type orderServiceClient struct{ cc *grpc.ClientConn }
func NewOrderServiceClient(cc *grpc.ClientConn) OrderServiceClient {
    return &orderServiceClient{cc}
}
func (c *orderServiceClient) GetOrder(id string) string { return "" }
type OrderServiceServer interface{ GetOrder(string) string }
func RegisterOrderServiceServer(s *grpc.Server, srv OrderServiceServer) {}
"""
_ORDER_SERVER_GO = """\
package main
import (
    "google.golang.org/grpc"
    "example.com/svc-orders/orders"
)
type orderSvr struct{ orders.OrderServiceServer }
func main() {
    s := grpc.NewServer()
    orders.RegisterOrderServiceServer(s, &orderSvr{})
}
"""
_ORDER_ROUTES_GO = """\
package main
import "net/http"
func register() { http.HandleFunc("/orders/status", handleOrder) }
func handleOrder(w http.ResponseWriter, r *http.Request) {}
"""
_SHIPMENT_GO = """\
package main
import "net/http"
func register() { http.HandleFunc("/shipments/status", handleShipment) }
func handleShipment(w http.ResponseWriter, r *http.Request) {}
"""
_ORDER_PHP_CLIENT = """\
<?php
// PHP API gateway: serves /orders, calls Go gRPC + Go HTTP downstream
Route::post('/orders', function() {
    $channel = new Grpc\\Channel('localhost:50051', []);
    $client = new OrderServiceClient($channel);
    $resp = $client->GetOrder(new GetOrderRequest());
    $http = new GuzzleHttp\\Client();
    $http->get('/shipments/status');
    return $resp;
});
"""
_ORDER_TS_CLIENT = """\
async function loadOrder() {
    const resp = await fetch('/orders/status');
    return resp.json();
}
"""
_ORDER_PY_CLIENT = """\
import requests
def get_order():
    return requests.get('/orders/status')
"""
_RUBY_CLIENT = """\
# Sinatra gateway: serves /ruby/status, calls svc-orders via HTTParty
get '/ruby/status' do
  HTTParty.get('/orders/status')
end
"""

class SynthFederation:
    """Holds paths and connection for a synthetic federation. Teardown via .cleanup()."""

    def __init__(self, base: Path, root: str, cart: str, checkout: str) -> None:
        self.base = base
        self.root = root
        self.cart = cart
        self.checkout = checkout
        self.members = [cart, checkout]


def build_synth_federation() -> SynthFederation:
    """Build the synthetic federation; caller is responsible for cleanup."""
    _SAFE_BASE.mkdir(parents=True, exist_ok=True)
    base = Path(tempfile.mkdtemp(dir=_SAFE_BASE))
    cart = _make_member(base, "svc-cart",
                        {"cart/cart.pb.go": _CART_PB_GO, "server.go": _CART_SERVER_GO}, "feature")
    checkout = _make_member(base, "svc-checkout",
                            {"checkout.go": _CHECKOUT_GO}, "domain")
    root = _make_root(base, [cart, checkout])
    return SynthFederation(base, root, cart, checkout)


def teardown_synth_federation(fed: SynthFederation) -> None:
    for p in (fed.root, fed.cart, fed.checkout):
        with contextlib.suppress(Exception):
            remove_project(p)
    shutil.rmtree(fed.base, ignore_errors=True)


class PolyglotFederation:
    """Synthetic 5-service polyglot federation (Go + PHP + TS + Python)."""

    def __init__(self, base: Path, root: str, members: list[str]) -> None:
        self.base = base
        self.root = root
        self.members = members


def build_polyglot_federation() -> PolyglotFederation:
    """Synthetic polyglot federation: Go servers + PHP/TS/Python callers. No real paths."""
    _SAFE_BASE.mkdir(parents=True, exist_ok=True)
    base = Path(tempfile.mkdtemp(dir=_SAFE_BASE))
    svc_orders = _make_member(base, "svc-orders", {
        "orders/orders.proto": _ORDERS_PROTO,
        "orders/orders.pb.go": _ORDER_PB_GO,
        "server.go": _ORDER_SERVER_GO,
        "routes.go": _ORDER_ROUTES_GO,
    }, "feature")
    svc_shipments = _make_member(base, "svc-shipments", {"routes.go": _SHIPMENT_GO}, "feature")
    svc_php = _make_member(base, "svc-php", {"src/Client.php": _ORDER_PHP_CLIENT}, "domain")
    svc_ts = _make_member(base, "svc-ts", {"src/client.ts": _ORDER_TS_CLIENT}, "domain")
    svc_py = _make_member(base, "svc-py", {"src/client.py": _ORDER_PY_CLIENT}, "domain")
    svc_ruby = _make_member(base, "svc-ruby", {"app.rb": _RUBY_CLIENT}, "domain")
    members = [svc_orders, svc_shipments, svc_php, svc_ts, svc_py, svc_ruby]
    root = _make_root(base, members)
    return PolyglotFederation(base, root, members)


def teardown_polyglot_federation(fed: PolyglotFederation) -> None:
    for p in [fed.root, *fed.members]:
        with contextlib.suppress(Exception):
            remove_project(p)
    shutil.rmtree(fed.base, ignore_errors=True)


@pytest.fixture(scope="module")
def synth_fed() -> Generator[SynthFederation, None, None]:
    """Module-scoped synthetic federation fixture."""
    fed = build_synth_federation()
    yield fed
    teardown_synth_federation(fed)


@pytest.fixture(scope="module")
def polyglot_fed() -> Generator[PolyglotFederation, None, None]:
    """Module-scoped synthetic polyglot federation fixture."""
    fed = build_polyglot_federation()
    yield fed
    teardown_polyglot_federation(fed)
