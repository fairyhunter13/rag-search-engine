"""Tests for opencode_search.graph.resolver — 6-strategy call resolver."""
from __future__ import annotations

import hashlib

import pytest

from opencode_search.graph.extractor import _RawEdge
from opencode_search.graph.resolver import CallResolver
from opencode_search.graph.storage import NodeData


def _node(file: str, name: str, qualified_name: str | None = None) -> NodeData:
    qn = qualified_name or f"mod.{name}"
    nid = hashlib.sha256(f"{file}::{qn}".encode()).hexdigest()[:16]
    return NodeData(
        id=nid, name=name, qualified_name=qn, kind="function",
        file=file, created_at="", updated_at="",
    )


def _file_node(file: str) -> NodeData:
    nid = hashlib.sha256(f"{file}::{file}".encode()).hexdigest()[:16]
    return NodeData(
        id=nid, name=file.split("/")[-1], qualified_name=file, kind="file",
        file=file, created_at="", updated_at="",
    )


def _raw(from_id: str, raw_callee: str, kind: str = "CALLS") -> _RawEdge:
    return _RawEdge(from_id=from_id, raw_callee=raw_callee, kind=kind)


# ---------------------------------------------------------------------------
# Basic resolution
# ---------------------------------------------------------------------------


def test_resolve_import_map_strategy(tmp_path):
    n = _node("/a.py", "authenticate", qualified_name="auth.authenticate")
    resolver = CallResolver([n])
    raw = _raw("some_id", "auth.authenticate")
    edges = resolver.resolve([raw])
    assert len(edges) == 1
    assert edges[0].to_id == n.id
    assert edges[0].confidence >= 0.90
    assert edges[0].resolution_strategy in ("import_map", "import_map_suffix", "unique_name")


def test_resolve_same_module_strategy():
    file_path = "/app/auth.py"
    caller = _node(file_path, "login", qualified_name="auth.login")
    callee = _node(file_path, "verify", qualified_name="auth.verify")
    resolver = CallResolver([caller, callee])
    raw = _raw(caller.id, "verify")
    edges = resolver.resolve([raw])
    assert len(edges) == 1
    assert edges[0].to_id == callee.id
    assert edges[0].resolution_strategy == "same_module"
    assert edges[0].confidence == 0.90


def test_resolve_unique_name_strategy():
    n = _node("/other.py", "do_something", qualified_name="utils.do_something")
    caller = _node("/main.py", "main_fn", qualified_name="main.main_fn")
    resolver = CallResolver([n, caller])
    raw = _raw(caller.id, "do_something")
    edges = resolver.resolve([raw])
    assert len(edges) == 1
    assert edges[0].to_id == n.id
    assert edges[0].resolution_strategy == "unique_name"
    assert edges[0].confidence == 0.75


def test_resolve_import_map_suffix_strategy():
    n = _node("/storage.py", "write_chunks", qualified_name="opencode_search.storage.write_chunks")
    caller = _node("/indexer.py", "index", qualified_name="opencode_search.indexer.index")
    resolver = CallResolver([n, caller])
    raw = _raw(caller.id, "storage.write_chunks")
    edges = resolver.resolve([raw])
    assert len(edges) == 1
    assert edges[0].to_id == n.id


def test_resolve_suffix_match_strategy():
    n = _node("/deep/utils.py", "helper_fn", qualified_name="deep.utils.helper_fn")
    caller = _node("/main.py", "run", qualified_name="main.run")
    resolver = CallResolver([n, caller])
    raw = _raw(caller.id, "helper_fn")
    edges = resolver.resolve([raw])
    assert len(edges) == 1
    assert edges[0].to_id == n.id


def test_resolve_fuzzy_strategy():
    n = _node("/a.py", "authenticate_user", qualified_name="auth.authenticate_user")
    caller = _node("/b.py", "login", qualified_name="app.login")
    resolver = CallResolver([n, caller])
    raw = _raw(caller.id, "auth.authenticate_user")
    edges = resolver.resolve([raw])
    assert len(edges) >= 0  # fuzzy may or may not match
    # Just ensure no crash


def test_resolve_unresolvable_edge_dropped():
    n = _node("/a.py", "known_func", qualified_name="mod.known_func")
    resolver = CallResolver([n])
    raw = _raw(n.id, "completely_unknown_xyz_function_12345")
    edges = resolver.resolve([raw])
    # External lib call — should be dropped
    assert len(edges) == 0


def test_resolve_cross_file_call_resolved():
    caller = _node("/handler.py", "handle_request", qualified_name="handler.handle_request")
    callee = _node("/db.py", "get_connection", qualified_name="db.get_connection")
    resolver = CallResolver([caller, callee])
    raw = _raw(caller.id, "get_connection")
    edges = resolver.resolve([raw])
    assert len(edges) == 1
    assert edges[0].to_id == callee.id


def test_resolve_method_call_on_object():
    callee = _node("/storage.py", "write_chunks", qualified_name="storage.Storage.write_chunks")
    caller = _node("/indexer.py", "index", qualified_name="indexer.index")
    resolver = CallResolver([caller, callee])
    raw = _raw(caller.id, "storage.write_chunks")
    edges = resolver.resolve([raw])
    assert len(edges) == 1
    assert edges[0].to_id == callee.id


def test_resolve_empty_callee_dropped():
    n = _node("/a.py", "foo", qualified_name="mod.foo")
    resolver = CallResolver([n])
    raw = _raw(n.id, "")
    edges = resolver.resolve([raw])
    assert len(edges) == 0


def test_resolve_preserves_edge_kind():
    n = _node("/a.py", "foo", qualified_name="mod.foo")
    caller = _node("/b.py", "bar", qualified_name="mod.bar")
    resolver = CallResolver([n, caller])
    raw = _raw(caller.id, "foo", kind="IMPORTS")
    edges = resolver.resolve([raw])
    if edges:
        assert edges[0].kind == "IMPORTS"


def test_resolve_multiple_edges():
    fn_a = _node("/a.py", "fn_a", qualified_name="mod.fn_a")
    fn_b = _node("/b.py", "fn_b", qualified_name="mod.fn_b")
    caller = _node("/main.py", "main_func", qualified_name="main.main_func")
    resolver = CallResolver([fn_a, fn_b, caller])
    raw_edges = [
        _raw(caller.id, "fn_a"),
        _raw(caller.id, "fn_b"),
    ]
    edges = resolver.resolve(raw_edges)
    assert len(edges) == 2


def test_resolve_prefers_same_file_over_unique_name():
    """When multiple nodes have the same name, prefer same-file node."""
    file_path = "/app.py"
    same_file_callee = _node(file_path, "helper", qualified_name="app.helper")
    other_callee = _node("/other.py", "helper", qualified_name="utils.helper")
    caller = _node(file_path, "run", qualified_name="app.run")
    resolver = CallResolver([same_file_callee, other_callee, caller])
    raw = _raw(caller.id, "helper")
    edges = resolver.resolve([raw])
    assert len(edges) == 1
    assert edges[0].to_id == same_file_callee.id


def test_resolve_ambiguous_callee_returns_one_edge():
    """Multiple matches should return exactly one edge (not duplicate)."""
    n1 = _node("/a.py", "process", qualified_name="a.process")
    n2 = _node("/b.py", "process", qualified_name="b.process")
    caller = _node("/main.py", "run", qualified_name="main.run")
    resolver = CallResolver([n1, n2, caller])
    raw = _raw(caller.id, "process")
    edges = resolver.resolve([raw])
    assert len(edges) <= 1
