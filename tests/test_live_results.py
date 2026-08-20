"""Layer 3, part two: the watcher through a symlink, and results that resolve.

These are the assertions that a broken engine passes if they are written the
obvious way. A watcher test that touches the root directly passes with the
symlink trap fully in force. A search test that checks `results != []` passes
while every line number is stale. Both are written here the awkward way on
purpose.
"""

from __future__ import annotations

import statistics
import subprocess
import time
from pathlib import Path

import pytest

from live import Rpc, require_clear_gpu, require_daemon, until

pytestmark = pytest.mark.live

NEW_FILE = "src/late_addition.py"
NEEDLE = "def reconcile_orphaned_leases"


def _repo(path, files):
    path.mkdir(parents=True, exist_ok=True)
    for rel, text in files.items():
        target = path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    return path


@pytest.fixture(scope="module")
def rpc():
    require_clear_gpu()
    client = Rpc(require_daemon())
    yield client
    client.close()


@pytest.fixture(scope="module")
def tree(tmp_path_factory, rpc):
    member = _repo(tmp_path_factory.mktemp("wmember"), {"src/keep.py": "KEEP = 1\n"})
    root = _repo(tmp_path_factory.mktemp("wroot"), {"main.py": "import keep\n"})
    (root / "link").symlink_to(member, target_is_directory=True)
    rpc.tool("index", root=str(root))
    until(
        lambda: rpc.tool("index", root=str(root))["indexed"]["chunks"] > 0,
        timeout=180,
        what="the first pass to finish",
    )
    # The member's own pass, not just the root's. Every test below writes into
    # the member, and a member whose first walk has not run yet sweeps the new
    # file in on its own -- which passes the watcher tests with the watcher
    # never firing.
    until(
        lambda: rpc.tool("index", root=str(member))["indexed"]["chunks"] > 0,
        timeout=180,
        what="the member's own first pass to finish",
    )
    # And the member's watches in place. Registration only sets a flag; the
    # rebuild lands seconds later, and a write before it is lost for good.
    until(
        lambda: rpc.tool("index", root=str(member))["watching"],
        timeout=60,
        what="the member's watches to be armed",
    )
    yield root, member
    rpc.tool("index", root=str(root), enabled=False)
    rpc.tool("index", root=str(member), enabled=False)


def _paths(rpc, query, root, **kw):
    out = rpc.tool("search", query=query, root=str(root), mode="lexical", **kw)
    assert "error" not in out, out
    return out["results"]


def test_a_write_reached_only_through_a_symlink_is_noticed(rpc, tree):
    """The inotify symlink trap. The file is created inside the member's real
    directory, but the engine only ever learned of that directory through a
    link -- if the watcher armed on the link, nothing here ever fires."""
    root, member = tree
    (member / NEW_FILE).parent.mkdir(parents=True, exist_ok=True)
    (member / NEW_FILE).write_text(f"{NEEDLE}(store):\n    return store.sweep()\n")
    hits = until(
        lambda: [r for r in _paths(rpc, NEEDLE, root) if NEW_FILE in r["path"]],
        timeout=300,
        what="the watcher to index a file written behind a symlink",
    )
    assert hits


def test_a_deleted_file_stops_being_returned(rpc, tree):
    """Deletion is the direction that fails silently: an index that only ever
    adds keeps answering with line ranges that no longer exist."""
    root, member = tree
    (member / NEW_FILE).unlink()
    until(
        lambda: not [r for r in _paths(rpc, NEEDLE, root) if NEW_FILE in r["path"]],
        timeout=300,
        what="the deleted file to leave the index",
    )


def test_every_returned_location_resolves(rpc, tree):
    """A ranked list of stale line numbers looks identical to a working one,
    so the assertion is that the range on disk contains what was asked for."""
    root = tree[0]
    results = _paths(rpc, "KEEP", root, k=5)
    assert results, "the fixture's own content must be findable"
    for hit in results:
        path = Path(hit["path"])
        assert path.is_file(), hit
        lines = path.read_text().splitlines()
        start, end = hit["lines"]
        assert 1 <= start <= end <= len(lines), (hit, len(lines))
        # Case-folded, because the lexical lane is: `KEEP` legitimately returns
        # the `import keep` line, and a literal comparison here read that
        # correct hit as a stale line number.
        assert "keep" in "\n".join(lines[start - 1 : end]).lower(), hit


def test_the_preview_is_a_location_not_a_body(rpc, tree):
    """Finding 1 of the rebuild: vector retrieval beat grep only when results
    arrived as locations to open. A tool that inlines bodies by default is the
    arm that lost."""
    root = tree[0]
    [hit] = _paths(rpc, "KEEP", root, k=1)
    assert "body" not in hit or not hit.get("body")
    assert len(hit.get("preview", "").splitlines()) <= 3


def test_scoped_latency_is_recorded_warm_and_quiet(rpc, tree, record_property):
    """Not a threshold -- a record. The kill criterion for sqlite-vec is a
    scoped p95 above ~200 ms, and a number nobody wrote down cannot trip it."""
    root = tree[0]
    for _ in range(3):
        _paths(rpc, "KEEP", root)  # warm the models and the page cache
    took = []
    for _ in range(11):
        started = time.perf_counter()
        _paths(rpc, "KEEP", root)
        took.append((time.perf_counter() - started) * 1000)
    p50 = statistics.median(took)
    p95 = sorted(took)[int(0.95 * len(took)) - 1]
    record_property("scoped_p50_ms", round(p50, 1))
    record_property("scoped_p95_ms", round(p95, 1))
    print(f"\nscoped p50 {p50:.1f} ms, p95 {p95:.1f} ms over {len(took)} queries")
    assert p50 > 0
