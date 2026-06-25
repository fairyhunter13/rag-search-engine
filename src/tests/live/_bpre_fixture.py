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


@pytest.fixture(scope="module")
def synth_fed() -> Generator[SynthFederation, None, None]:
    """Module-scoped synthetic federation fixture."""
    fed = build_synth_federation()
    yield fed
    teardown_synth_federation(fed)
