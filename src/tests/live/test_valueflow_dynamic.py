"""Tier-1.5 value-flow live tests — multi-language, static + DYNAMIC dispatch.

Validates non-literal call arguments (const/variable) resolve through
intra-procedural def-use maps.  No mocks.  Go / Python / TypeScript / JavaScript.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.live

_PCACHE: dict = {}


def _parser(lang):
    if lang not in _PCACHE:
        from tree_sitter_language_pack import api as _ts_api
        _PCACHE[lang] = _ts_api.get_parser(lang)
    return _PCACHE[lang]


def _du(lang, src):
    from opencode_search.kb.valueflow import build_def_use
    root = _parser(lang).parse(src).root_node()
    return build_def_use(root, src.encode()), root, src.encode()


def _find(node, kind, depth=0):
    if depth > 10:
        return []
    r = [node] if node.kind() == kind else []
    for i in range(node.named_child_count()):
        r += _find(node.named_child(i), kind, depth + 1)
    return r


# ─── Go ──────────────────────────────────────────────────────────────────────

def test_go_const_resolves():
    from opencode_search.kb.valueflow import resolve_first_arg
    src = 'package main\nconst routeKey = "/cart"\nfunc f() { c.Get(routeKey) }\n'
    du, root, b = _du("go", src)
    assert du.get("routeKey") == "/cart", f"Go const not in def-use: {du}"
    for call in _find(root, "call_expression"):
        args = call.child_by_field_name("arguments")
        if args and args.named_child_count() > 0 and resolve_first_arg(args, b, du) == "/cart":
            return
    pytest.fail("resolve_first_arg did not resolve routeKey → '/cart'")


def test_go_short_var_resolves():
    src = 'package main\nfunc f() { x := "/api/orders"; c.Post(x) }\n'
    du, _, _ = _du("go", src)
    assert du.get("x") == "/api/orders", f"Go short-var-decl not in def-use: {du}"


def test_go_literal_fast_path():
    from opencode_search.kb.valueflow import resolve_first_arg
    src = 'package main\nfunc f() { c.Get("/direct") }\n'
    du, root, b = _du("go", src)
    for call in _find(root, "call_expression"):
        args = call.child_by_field_name("arguments")
        if args and args.named_child_count() > 0 and resolve_first_arg(args, b, du) == "/direct":
            return
    pytest.fail("Literal arg '/direct' not resolved")


# ─── Python ───────────────────────────────────────────────────────────────────

def test_python_assignment_resolves():
    src = 'TOPIC = "user.events"\npublisher.publish(TOPIC, data)\n'
    du, _, _ = _du("python", src)
    assert du.get("TOPIC") == "user.events", f"Python assignment not in def-use: {du}"


def test_python_identifier_arg_resolved():
    from opencode_search.kb.valueflow import resolve_first_arg
    src = 'TOPIC = "order.placed"\npublisher.emit(TOPIC)\n'
    du, root, b = _du("python", src)
    for call in _find(root, "call"):
        args = call.child_by_field_name("arguments")
        if args and args.named_child_count() > 0 and resolve_first_arg(args, b, du) == "order.placed":
            return
    pytest.fail("Python identifier arg not resolved → 'order.placed'")


# ─── TypeScript ───────────────────────────────────────────────────────────────

def test_ts_const_resolves():
    src = 'const endpoint = "/grpc/UserService/GetUser";\nclient.call(endpoint, req);\n'
    du, _, _ = _du("typescript", src)
    assert du.get("endpoint") == "/grpc/UserService/GetUser", f"TS const not in def-use: {du}"


def test_ts_let_resolves():
    src = 'let topic = "user.created";\npubsub.publish(topic, payload);\n'
    du, _, _ = _du("typescript", src)
    assert du.get("topic") == "user.created", f"TS let not in def-use: {du}"


# ─── JavaScript ───────────────────────────────────────────────────────────────

def test_js_var_resolves():
    src = 'var path = "/checkout";\nfetch(path);\n'
    du, _, _ = _du("javascript", src)
    assert du.get("path") == "/checkout", f"JS var not in def-use: {du}"


# ─── Invariants ───────────────────────────────────────────────────────────────

def test_true_dynamic_not_in_du():
    """A variable assigned from a function call is NOT in def-use (true dynamic → falls to GPU-rank)."""
    src = 'package main\nfunc f() { key := getKey(); c.Get(key) }\n'
    du, _, _ = _du("go", src)
    assert "key" not in du, f"Call-result 'key' should not appear in def-use: {du}"


def test_empty_source_no_crash():
    du, _, _ = _du("python", "")
    assert du == {}


def test_no_import_re_in_valueflow():
    import inspect

    from opencode_search.kb import valueflow
    src = inspect.getsource(valueflow)
    assert "import re" not in src and "re.compile" not in src, (
        "valueflow.py must not use re (zero-vocab + no-regex doctrine)"
    )


def test_no_framework_vocab_in_valueflow():
    import inspect

    from opencode_search.kb import valueflow
    src = inspect.getsource(valueflow)
    for name in ("Kafka", "RabbitMQ", "gRPC", "Django", "Flask", "Express"):
        assert name not in src, f"valueflow.py contains hardcoded vocab '{name}'"
