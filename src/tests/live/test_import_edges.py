"""Import edges (`file_imports`) — the file-to-file relation e11 added.

Tests:
  IM1: a resolvable import writes a row; a specifier naming nothing in the index writes none.
       The second half is the load-bearing one — an import graph that recorded `import os` as an
       edge to nowhere would be a dangling-row generator, and `file_imports` has no
       `purge_dangling_edges` because it has no sids to dangle.
  IM2: a full re-derive retracts a row whose `import` line was deleted while *both* files
       survive. `prune_imports_to`'s reason for existing, and the exact shape of GH5's edge arm.
  IM3: `overview(what="import_cycles")` answers about imports, not calls. Two files that import
       each other and never call each other are a cycle; two that call each other and import
       nothing are not. Before e11 this ran Tarjan on the file-level projection of the call
       graph, so it got both of these backwards.
  IM4: resolution is per-family and manifest-driven — a JS relative path, a PHP PSR-4 namespace
       read from `composer.json`, and a Go import read from `go.mod` each land a row. Guards the
       three rules whose input is a checked-in declarative file rather than path arithmetic.
  IM5: the incremental watcher path subtracts a file's *outgoing* imports and leaves its
       incoming ones, mirroring `delete_file_symbols`.
"""
from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.live


def _imports(proj):
    from rag_search.core.config import project_graph_db
    from rag_search.graph.store import GraphStore
    gs = GraphStore(project_graph_db(proj))
    try:
        return {(a, b) for a, b in gs.list_imports()}
    finally:
        gs.close()


def test_im1_resolvable_import_lands_and_external_one_does_not(safe_tmp_path):
    """IM1: `from pkg.util import f` writes a row; `import os` writes nothing."""
    from rag_search.daemon.sweeps import _rederive_graph

    pkg = safe_tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "util.py").write_text("def helper():\n    return 1\n")
    (pkg / "app.py").write_text(
        "import os\nimport json\nfrom pkg.util import helper\n\n\n"
        "def run():\n    return helper(), os.sep\n")
    proj = str(safe_tmp_path)
    _rederive_graph(proj)

    rows = _imports(proj)
    assert any(a.endswith("app.py") and b.endswith("util.py") for a, b in rows), (
        f"IM1: `from pkg.util import helper` resolved to no row: {sorted(rows)}")
    # `os` and `json` are two of app.py's three imports and neither is in this index, so app.py
    # must own exactly one row. A count, not a name check: it is the *absence* of an edge to
    # nowhere that is being asserted.
    from_app = [b for a, b in rows if a.endswith("app.py")]
    assert len(from_app) == 1, (
        f"IM1: app.py imports os, json and pkg.util; only the last is in this index, so it must "
        f"own exactly one row — got {from_app}")
    # Every recorded target must be a file this index actually holds — the property that keeps
    # the table free of the dangling rows it has no sweeper for.
    for _, dst in rows:
        assert dst.startswith(str(safe_tmp_path)), f"IM1: target outside the project: {dst}"


def test_im2_rederive_retracts_a_deleted_import(safe_tmp_path):
    """IM2: both files survive, the `import` line goes, the row must go with it."""
    from rag_search.daemon.sweeps import _rederive_graph

    pkg = safe_tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "util.py").write_text("def helper():\n    return 1\n")
    app = pkg / "app.py"
    app.write_text("from pkg.util import helper\n\n\ndef run():\n    return helper()\n")
    proj = str(safe_tmp_path)
    _rederive_graph(proj)
    assert _imports(proj), "IM2 setup: no import row was written at all"

    app.write_text("def run():\n    return 1\n")
    _rederive_graph(proj)
    rows = _imports(proj)
    assert not any(a.endswith("app.py") for a, _ in rows), (
        f"IM2: the row survived after its import statement was deleted — both endpoints still "
        f"exist, so nothing else can retract it: {sorted(rows)}")


def test_im3_cycles_are_an_import_question_not_a_call_question(safe_tmp_path):
    """IM3: `overview(what="import_cycles")` reads `file_imports`.

    Two files importing each other with no call between them is the case the old call-graph
    projection could not see; two files calling each other with no import is the case it
    reported as an import cycle when it was not one.
    """
    from rag_search.core.config import project_graph_db
    from rag_search.daemon.sweeps import _rederive_graph
    from rag_search.graph.store import GraphStore
    from rag_search.server._overview import _find_import_cycles

    pkg = safe_tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    # A mutual import that carries no call: each module binds a constant from the other.
    # Written as `from pkg.right import RIGHT` rather than `from pkg import right` because the
    # specifier an import statement *declares* is the module path, and `from pkg import right`
    # declares only `pkg` — resolving the imported name to a submodule would need a per-language
    # join rule (`.` / `/` / `\`), which is the mapping table HR15 forbids. That import lands an
    # edge to `pkg/__init__.py`, which is true and coarser; same concession as go's package rule.
    (pkg / "left.py").write_text("from pkg.right import RIGHT\n\nLEFT = RIGHT\n")
    (pkg / "right.py").write_text("from pkg.left import LEFT\n\nRIGHT = 2\n")
    proj = str(safe_tmp_path)
    _rederive_graph(proj)

    gs = GraphStore(project_graph_db(proj))
    try:
        cycles = _find_import_cycles(gs.conn)
    finally:
        gs.close()
    flat = {f for c in cycles for f in c}
    assert any(f.endswith("left.py") for f in flat) and any(
        f.endswith("right.py") for f in flat), (
        f"IM3: a mutual import with no call between the files was not reported as a cycle — "
        f"which is exactly what the call-graph projection could not see: {cycles}")


def test_im4_manifest_driven_resolution_per_family(safe_tmp_path):
    """IM4: JS relative path, PHP PSR-4 from composer.json, Go import from go.mod."""
    from rag_search.daemon.sweeps import _rederive_graph

    # --- JS: a relative specifier, resolved by extension probing.
    web = safe_tmp_path / "web"
    web.mkdir()
    (web / "util.js").write_text("export function helper() { return 1; }\n")
    (web / "app.js").write_text("import { helper } from './util';\nexport const x = 1;\n")

    # --- PHP: `use App\\Models\\User` against a declared psr-4 prefix.
    (safe_tmp_path / "composer.json").write_text(json.dumps(
        {"autoload": {"psr-4": {"App\\": "app/"}}}))
    models = safe_tmp_path / "app" / "Models"
    models.mkdir(parents=True)
    (models / "User.php").write_text("<?php\nnamespace App\\Models;\nclass User {}\n")
    (safe_tmp_path / "app" / "Service.php").write_text(
        "<?php\nnamespace App;\nuse App\\Models\\User;\nclass Service { public function f() {} }\n")

    # --- Go: an import under this module's own path.
    (safe_tmp_path / "go.mod").write_text("module example.test/proj\n\ngo 1.22\n")
    store = safe_tmp_path / "store"
    store.mkdir()
    (store / "store.go").write_text("package store\n\nfunc Get() int { return 1 }\n")
    (safe_tmp_path / "main.go").write_text(
        'package main\n\nimport "example.test/proj/store"\n\nfunc main() { _ = store.Get() }\n')

    proj = str(safe_tmp_path)
    _rederive_graph(proj)
    rows = _imports(proj)
    pairs = {(a.rsplit("/", 1)[-1], b.rsplit("/", 1)[-1]) for a, b in rows}
    assert ("app.js", "util.js") in pairs, f"IM4 js: relative specifier unresolved: {pairs}"
    assert ("Service.php", "User.php") in pairs, f"IM4 php: psr-4 unresolved: {pairs}"
    assert ("main.go", "store.go") in pairs, f"IM4 go: module path unresolved: {pairs}"


def test_im5_incremental_pass_subtracts_outgoing_imports_only(safe_tmp_path):
    """IM5: `delete_file_imports` drops a file's own imports and keeps its incoming ones."""
    from rag_search.core.config import project_graph_db
    from rag_search.daemon.sweeps import _rederive_graph
    from rag_search.graph.store import GraphStore

    pkg = safe_tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "util.py").write_text("from pkg.base import BASE\n\n\ndef helper():\n    return BASE\n")
    (pkg / "base.py").write_text("BASE = 1\n")
    (pkg / "app.py").write_text("from pkg.util import helper\n\n\ndef run():\n    return helper()\n")
    proj = str(safe_tmp_path)
    _rederive_graph(proj)

    gs = GraphStore(project_graph_db(proj))
    try:
        before = {(a, b) for a, b in gs.list_imports()}
        gs.delete_file_imports(str(pkg / "util.py"))
        gs.commit()
        after = {(a, b) for a, b in gs.list_imports()}
    finally:
        gs.close()
    assert any(a.endswith("util.py") for a, _ in before), "IM5 setup: util.py imported nothing"
    assert not any(a.endswith("util.py") for a, _ in after), (
        "IM5: outgoing imports of the deleted file survived")
    assert any(b.endswith("util.py") for _, b in after), (
        "IM5: incoming imports were dropped too — app.py was not re-scanned and its row is "
        "still true; dropping it would strip an untouched importer")
