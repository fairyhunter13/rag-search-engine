"""Background sweep jobs: maintenance; event-driven on_change graph re-derive and labelling."""
from __future__ import annotations

import hashlib
import json as _json
import logging
import threading
import time

_PAUSED: bool = False
# Monotonic start of the current pause, None while sweeping. Written only by `set_paused` and read
# only by /healthz, so that "paused and forgotten" is observable at all. Pausing has no refcount
# and no lease (see `reconcile_projects`), and what that hides is total rather than partial: on
# 2026-07-30 eleven consecutive pause calls from separate live-test sessions left `_PAUSED` True for
# ~4 h, every reconcile tick logged "abandoned before start", and no other signal looked wrong.
_PAUSED_SINCE: float | None = None
# Monotonic instant the pause stops being honoured. Unlike `_PAUSED_SINCE` this *is* re-armed by a
# re-pause: the caller that pauses again is demonstrably still alive, and a live suite run lasts far
# longer than one lease. What the lease bounds is the caller that stops calling — a killed session
# now decays in `_PAUSE_TTL_S` instead of holding every sweep until an operator notices.
_PAUSE_DEADLINE: float | None = None
_PAUSE_TTL_S: float = 1800.0  # a pause nobody renews for this long is a leak, not a decision
_LANE_DEBOUNCE_S: float = 45.0  # min seconds between graph-lane passes per project after a change
_INDEX_BACKOFF_S: float = 120.0  # min seconds before retrying a failed incremental reindex
_INCREMENTAL_COMPACT_AT: int = 50  # incremental graph passes before one authoritative re-derive
_last_lane_run: dict[str, float] = {}
_last_index_fail: dict[str, float] = {}
# Files whose graph rows are owed but whose event lost the debounce race, per project. The
# debounce *drops* events rather than queueing them, which cost only a delayed labelling pass
# before the graph rode this gate: labelling is project-scoped, so the next event redid it. The
# graph path is file-list-scoped, and it stamps source_sig as if the whole tree were re-derived —
# so a dropped list is invisible to _graph_stale afterwards and only the compaction counter ever
# recovers it. Measured live: a delete 26s behind an add left a symbol for a deleted file while
# the stored sig matched the tree exactly.
_pending_graph_files: dict[str, set[str]] = {}
# Drift gate: path → sig at last successful _label_project. Skips the heavy pass when unchanged.
_last_labelled_sig: dict[str, str] = {}
# Serializes CPU-bound graph work (symbol extraction / community recompute / labelling) across
# the watcher and reconcile threads so at most one heavy pass runs at a time — caps daemon CPU at
# ~one core instead of pinning two concurrently. Never held around index/embed or GPU queries.
_HEAVY_LOCK = threading.Lock()

log = logging.getLogger(__name__)


def set_paused(paused: bool, ttl_s: float = _PAUSE_TTL_S, now: float | None = None) -> bool:
    """Set the pause flag, returning what it was, keeping stamp and lease deadline in step.

    Re-pausing while already paused deliberately does **not** restamp `_PAUSED_SINCE`. The timestamp
    measures how long the pause has been in force, and the leak it exists to expose is repeated
    pause calls from callers that never resume — restamping would reset the clock on precisely the
    signal it is for. The *deadline* is re-armed on every call, which is the opposite rule for the
    opposite reason: see `_PAUSE_DEADLINE`.

    `now` is injected only by the lease's own tests, which must assert what happens minutes past a
    deadline without sleeping through it.
    """
    global _PAUSED, _PAUSED_SINCE, _PAUSE_DEADLINE
    was = _PAUSED
    _PAUSED = paused
    if not paused:
        _PAUSED_SINCE = _PAUSE_DEADLINE = None
        return was
    at = time.monotonic() if now is None else now
    if _PAUSED_SINCE is None:
        _PAUSED_SINCE = at
    _PAUSE_DEADLINE = at + ttl_s
    return was


def paused_seconds() -> float:
    """Seconds sweeps have been paused; 0.0 while sweeping. The /healthz view of `_PAUSED`.

    Reads the stamp rather than repairing a missing one, because in the daemon the unstamped-while-
    paused state is unreachable by construction: `set_paused` is the only writer of `_PAUSED` in
    `rag_search/`, which `test_no_raw_sweeps_toggle.py` enforces statically. A test that assigns the
    flag directly drives its own imported module in its own process, never the daemon serving this.
    """
    since = _PAUSED_SINCE
    return 0.0 if not _PAUSED or since is None else time.monotonic() - since


def is_paused(now: float | None = None) -> bool:
    """Whether sweeps are paused *and* the pause is still within its lease.

    The single read every sweep goes through, because the flag alone had no way back: four early
    returns honoured `_PAUSED` forever, so a killed live-test session held the daemon's whole
    maintenance until someone thought to read /healthz. Expiry resumes and says so at WARNING,
    naming the duration — the resume is the recovery, the log line is what stops it being silent
    (a fault that heals itself without a trace is indistinguishable from one that never happened).
    """
    if not _PAUSED:
        return False
    deadline = _PAUSE_DEADLINE
    at = time.monotonic() if now is None else now
    if deadline is None or at < deadline:
        return True
    held = at - _PAUSED_SINCE if _PAUSED_SINCE is not None else 0.0
    set_paused(False)
    log.warning(
        "sweeps: pause lease expired after %.0fs (ttl %.0fs) — resuming. Whoever paused never "
        "resumed; if that was a live-test session, it died mid-run.", held, _PAUSE_TTL_S,
    )
    return False


def pause_lease_remaining_s(now: float | None = None) -> float:
    """Seconds left on the current pause lease; 0.0 while sweeping, or once expired."""
    deadline = _PAUSE_DEADLINE
    if not _PAUSED or deadline is None:
        return 0.0
    at = time.monotonic() if now is None else now
    return max(0.0, deadline - at)


# Composite pipeline algorithm version — bump either component constant to trigger re-derive.
# Also folds a SHA-4 of key pipeline modules so code-only changes self-heal without a manual bump.
# The modules whose bytes determine graph output, relative to src/rag_search/. A missing file
# contributes nothing to the hash, so a stale entry would go dead silently — SH2b in
# test_self_heal_code_fp.py asserts every entry exists.
# Editing this tuple, or any file it names, re-derives all fleet graphs — and it takes
# effect the moment the bytes change on disk, because `_fingerprint_paths` re-reads them per call
# with no memo. Under an editable install that means a live daemon, no restart and no deploy: an
# edit to extractor.py during a fleet repair puts a 160-graph re-derive in contention with it.

_FINGERPRINT_MODULES = ("graph/extractor.py", "graph/community.py", "graph/php_receivers.py")

# Emit a call edge only when the narrowest scope holding a candidate holds exactly one. Raising
# this admits groups of size C, which contribute one correct edge and C-1 wrong ones — the marginal
# edge is correct with probability 1/C. Measured over 155 stores, 193,309 call-site groups:
#
#   cap    edges emitted   precision   recall
#     1         122,324       1.000     0.633
#     2         157,854       0.887     0.725
#     4         193,176       0.780     0.779
#   none      1,021,043       0.182     0.960     <- 2,320,130 before the scope tiers below
#
# 1 is the only value at which an edge in this table *means* a resolved call, and `graph_handler`
# has no confidence column, no `ORDER BY` and no other way to tell a resolution from a guess.
#
# This constant sits here for edit locality only. `sweeps.py` is deliberately NOT in
# `_FINGERPRINT_MODULES` above, and `_pipeline_algo_version`'s S3 note says what that costs: a
# resolution change here is invisible to `_code_fingerprint` and would serve stale edges forever.
# **Changing this value, or the tiers that feed it, requires an `EXTRACTOR_REV` bump in the same
# commit.** Nothing will tell you otherwise.
_MAX_CALLEE_FANOUT = 1


def _fingerprint_paths(paths) -> str:
    """4-char SHA over the concatenated bytes of `paths`, in order. Missing files contribute none."""
    import contextlib
    h = hashlib.sha1()
    for p in paths:
        with contextlib.suppress(OSError):
            h.update(p.read_bytes())
    return h.hexdigest()[:4]


def _code_fingerprint() -> str:
    """4-char SHA over source bytes of modules that determine graph output."""
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]  # src/rag_search/
    return _fingerprint_paths(root / m for m in _FINGERPRINT_MODULES)


def _pipeline_algo_version() -> str:
    """The stamp a stored graph must match to be considered current.

    S3: `EXTRACTOR_REV` is carried explicitly because the byte fingerprint above cannot see
    every input to extraction. Call resolution lives in `_extract_graph`, in *this* file, which
    is deliberately not fingerprinted — hashing a module this large would re-derive 160 graphs
    for an unrelated log line. So a resolution change is invisible to `_code_fingerprint` and
    would have served stale edges forever. Bump `EXTRACTOR_REV` in the same commit as any change
    to what extraction emits.

    No test can force that bump — it is a human step, and this docstring named a
    `test_extractor_rev.py` that was never written, which is worse than naming nothing. What the
    suite does hold is the pair either side of it: `test_extraction_phase5.py`'s TS3 pins the
    identity's *composition* (a pre-S3 stamp must read stale), and TS0/TS8 pin what extraction
    *emits*, so a silent change to edge semantics goes red there even though the stamp cannot.
    """
    from rag_search.graph.community import ALGO_VERSION
    from rag_search.graph.extractor import EXTRACTOR_REV
    return f"{ALGO_VERSION}+{EXTRACTOR_REV}+{_code_fingerprint()}"


# Code-only fingerprint memo (HR38): 'relpath:mtime' filtered to is_code_language files only,
# so the labelling gate and the graph re-derive gate are both code-only — non-code churn
# (docs/config/images) never wakes either.
_code_fingerprint_cache: dict[str, tuple[float, str, float, list[str]]] = {}

# How long a scan's fingerprint, watermark and discoverable-file set may be reused before the
# tree is walked again. Bounds the cost of being honest; it does not bound the drift, which the
# walk itself is what detects. Read per call, not at import, so a gate can shorten it rather
# than sleep out the real one.
_CODE_SCAN_TTL_S = 300.0

# Files the set-drift trigger will re-index for one project in one pass. Not a throughput bound —
# it makes the repair *fair* and *resumable*. `_index_files` does not yield, so uncapped, the
# largest stranded store (7,693 files, ~85 min) would hold the walk while every project behind it
# waited, and a pause abandons the walk where it stands: exactly the starvation that left the last
# fleet migration discarding 104 of 202 projects. Capped, each pass keeps what it stamped and the
# next re-derives the remainder from the store, needing no progress state. The cost is wall clock,
# since the loop sleeps RSE_RECONCILE_RESYNC_S between walks regardless of outstanding work.
_DRIFT_REPAIR_MAX = 500


def _code_scan_ttl() -> float:
    import os
    try:
        return float(os.environ.get("RSE_CODE_SCAN_TTL_S", _CODE_SCAN_TTL_S))
    except ValueError:
        return _CODE_SCAN_TTL_S


def _code_source_fingerprint(path: str) -> str:
    """SHA-1 over sorted 'relpath:mtime' for CODE files only — stat-only, GPU-free."""
    return _code_scan(path)[0]


def _newest_code_mtime(path: str) -> float:
    """Newest mtime across the same code files the fingerprint walks; 0.0 when there are none.

    Deliberately the *same* walk rather than a second one — reconcile asks for the
    fingerprint first, so this is a memo hit by the time it is called.
    """
    return _code_scan(path)[1]


def _code_scan(path: str) -> tuple[str, float]:
    from pathlib import Path

    from rag_search.index.discover import (
        detect_language,
        is_code_language,
        is_generated_path,
        iter_files,
    )
    root = Path(path)
    # The memo is keyed on elapsed time, NOT on the root directory's mtime.
    #
    # Not `root.stat().st_mtime`: a directory's mtime moves only when its *direct* entries
    # change, so every edit nested below the root leaves that key identical and returns a frozen
    # fingerprint and watermark — and since the baseline `_vectors_content_stale` compares against
    # keeps advancing, a frozen watermark reads "clean" permanently. Measured on the live fleet
    # before this changed: keys 1.5 to 9.1 hours older than the project's newest file.
    #
    # A walk is the only thing that can answer this honestly, so the cache now just bounds how
    # often we pay for one: 43 s of stat-walking for all 160 projects, against a reconcile cadence
    # measured in half-hours.
    now = time.monotonic()
    cached = _code_fingerprint_cache.get(path)
    if cached is not None and (now - cached[0]) < _code_scan_ttl():
        return cached[1], cached[2]
    parts: list[str] = []
    newest = 0.0
    seen: list[str] = []
    try:
        for f in iter_files(root, federation_mode=True):
            # Every discoverable file, before the code-only filter below: this is the set an
            # index pass would visit, and `_index_set_drift` compares it against what the store
            # has actually processed. Collected here rather than in a second walk because this
            # loop already visits all of them and then throws the non-code ones away.
            seen.append(str(f))
            if not is_code_language(detect_language(f)):
                continue
            if is_generated_path(f.name):
                continue  # derived codegen output — regen is not source drift (HR38)
            try:
                rel = str(f.relative_to(root))
                mtime = int(f.stat().st_mtime)
                newest = max(newest, float(mtime))
                parts.append(f"{rel}:{mtime}")
            except (OSError, ValueError):
                pass
    except Exception:
        pass
    parts.sort()
    sig = hashlib.sha1("\n".join(parts).encode()).hexdigest()
    _code_fingerprint_cache[path] = (now, sig, newest, seen)
    return sig, newest


def _discoverable(path: str) -> list[str]:
    """Every file an index pass would visit for this project, off `_code_scan`'s walk."""
    _code_scan(path)
    cached = _code_fingerprint_cache.get(path)
    return list(cached[3]) if cached else []


# Watermark: the newest source mtime the vectors in a store were built from.
_VECTORS_MTIME_KEY = "source_mtime"


def _vectors_baseline(path: str) -> float | None:
    """When this project's vectors were last brought up to date, as an mtime.

    The store's own watermark when it has one, else the registry's `indexed_at`. That
    fallback is load-bearing: stores written before this key existed must still be
    checked, or precisely the projects that have already rotted would be grandfathered
    into staying rotten. None means never indexed — `_needs_index`'s business, not ours.
    """
    from datetime import datetime

    from rag_search.core.config import project_vector_db
    from rag_search.core.registry import get_project
    from rag_search.index.store import VectorStore

    vdb = project_vector_db(path)
    if not vdb.exists():
        return None
    vs = VectorStore(vdb)
    try:
        raw = vs.get_meta(_VECTORS_MTIME_KEY)
    except Exception:
        return None
    finally:
        vs.close()
    if raw is not None:
        return float(raw)
    e = get_project(path)
    if e is None or not e.indexed_at:
        return None
    try:
        return datetime.fromisoformat(e.indexed_at).timestamp()
    except ValueError:
        return None


def _vectors_stale(path: str) -> str | None:
    """The index's recorded embed signature if it disagrees with the running config.

    Mirrors _graph_stale for the vector side. Vectors are only comparable to a query
    embedded the same way, so a model or token-budget change silently invalidates
    every stored vector — this is what makes that visible instead of permanent.
    """
    from rag_search.core.config import project_vector_db
    from rag_search.index.store import VectorStore

    vdb = project_vector_db(path)
    if not vdb.exists():
        return None
    vs = VectorStore(vdb)
    try:
        return vs.stale_signature()
    except Exception:
        return None
    finally:
        vs.close()


def _vectors_content_stale(path: str) -> list:
    """Code files written after this store's vectors were last brought up to date.

    `_vectors_stale` sees only *signature* drift, and `_graph_stale`'s mtime drift routes
    to `_rederive_graph`, which never touches vectors. So a project that loses its watcher
    stream keeps a maintained graph over months-old vectors and looks healthy from every
    angle except the answers it gives. This is the trigger that closes that hole.

    Returns the files rather than a bool so the caller can re-embed just those — a full
    rebuild here would re-index the whole fleet on the first pass after deploy, for
    projects the watcher may have been maintaining correctly all along.
    """
    import contextlib
    from pathlib import Path

    from rag_search.index.discover import (
        detect_language,
        is_code_language,
        is_generated_path,
        iter_files,
    )
    baseline = _vectors_baseline(path)
    # The cheap check first, off the memoised scan reconcile has already paid for. Only a
    # project that fails it walks the tree again, which after convergence is rare.
    if baseline is None or _newest_code_mtime(path) <= baseline:
        return []
    out = []
    try:
        for f in iter_files(Path(path), federation_mode=True):
            if not is_code_language(detect_language(f)) or is_generated_path(f.name):
                continue
            with contextlib.suppress(OSError):
                if f.stat().st_mtime > baseline:
                    out.append(f)
    except Exception:
        pass
    return out


def _index_set_drift(path: str) -> tuple[list, list[str]]:
    """(files discoverable but never processed, processed paths no longer discoverable).

    The completeness check `_vectors_content_stale` is not. That one asks "has anything changed
    since I last ran" — but the index's contents are decided by discovery policy, not only by file
    writes, so a lowered size cap or a newly excluded suffix retroactively changes what should be
    indexed and no comparison of timestamps can notice, in either direction. Measured on the live
    fleet the day this landed: 42,952 chunks indexed that discovery rejects today, and 378 files
    discoverable and never indexed, with every existing check reporting it perfectly converged.

    Compared against `file_hashes`, NOT `chunks`: a file that legitimately chunks to nothing (an
    empty `__init__.py`) still gets a hash, so it is processed-and-done rather than eternally
    missing — 68 of the fleet's 446 were exactly that, and keying on `chunks` would requeue them
    every pass forever, the never-satisfiable gate `_graph_needs_full_index` had to be rescued from.

    The two sides are deliberately asymmetric. Orphans come from `file_hashes` UNION `chunks`,
    because what keeps retrieving is a *chunk*, and a chunk row can outlive its hash row: 2,091 of
    the largest workspace's 5,242 indexed paths had no hash row at all, written by an index generation that
    pre-dates the table. Keying the orphan side on `file_hashes` alone would leave every one of
    those permanently unpurgeable the moment discovery stopped yielding it — the exact defect this
    function exists to close, reintroduced through the other door.
    """
    from pathlib import Path

    from rag_search.core.config import project_vector_db
    from rag_search.index.store import VectorStore

    vdb = project_vector_db(path)
    if not vdb.exists():
        return [], []
    live = _discoverable(path)
    if not live:
        # An empty walk means the tree is gone or unreadable, not that every indexed file was
        # deleted. Repairing on that reading would purge a whole project on a transient error.
        return [], []
    vs = VectorStore(vdb, migrate=False)
    try:
        known = {p for (p,) in vs._con.execute("SELECT path FROM file_hashes")}
        charted = {p for (p,) in vs._con.execute("SELECT DISTINCT path FROM chunks")}
    except Exception:
        return [], []
    finally:
        vs.close()
    if not known and not charted:
        return [], []  # genuinely empty store: a first full index_project's business, not ours
    # An empty `known` with a non-empty `charted` is NOT an unindexed project — it is a store
    # written before file_hashes existed, and deferring it to `_needs_index` (as this did) meant
    # deferring it to a predicate that returns False for it, since the store has chunks and
    # communities and looks healthy. Measured: one member 3,066 of 3,066 live files unhashed,
    # the fleet's largest project 7,697 of 7,697, both invisible to all four triggers. Repairing them is the
    # whole point; skipping them is the escape hatch this function exists to remove.
    live_set = set(live)
    return (
        [Path(p) for p in live if p not in known and _readable(p)],
        sorted((known | charted) - live_set),
    )


def _readable(path: str) -> bool:
    """Would an index pass be able to produce anything for this path?

    `index_files` purges an unreadable path and returns *without stamping a hash* — it has no
    content to hash. So a discoverable file that cannot be read (a broken symlink, a mode-000
    file, a FIFO) is reported unindexed, handed over, purged, and reported again on the next
    pass, forever: the never-satisfiable gate SD3 guards against for zero-chunk files, in the one
    shape a zero-chunk file does not cover. Screening here keeps the trigger convergent and keeps
    its log line meaning "there is work to do" rather than becoming a permanent alarm.
    """
    import os
    return os.path.isfile(path) and os.access(path, os.R_OK)


def _purge_paths(project_path: str, paths: list[str]) -> None:
    """Drop every trace of paths the index holds but discovery no longer yields.

    Not routed through `_index_files`: most of these files still exist and are perfectly
    readable — they are excluded by policy, not gone — so handing them to `index_files` would
    read them straight back in. Only an explicit delete expresses "should not be indexed".
    """
    from rag_search.core.config import project_vector_db
    from rag_search.index.store import VectorStore

    vdb = project_vector_db(project_path)
    if not vdb.exists():
        return
    vs = VectorStore(vdb)
    try:
        for p in paths:
            vs.delete_by_path(p)
        vs.flush()
    finally:
        vs.close()


def _graph_needs_full_index(gs) -> bool:  # gs: GraphStore
    """True if the graph holds symbols that were never clustered.

    Zero communities means the clustering pass never ran — but only when there was
    something to cluster. Keying on `community_count() == 0` alone made this trigger
    permanently true for any project whose extractor legitimately yields no symbols
    (config/docs trees, or a language no extraction rung covers): nothing to cluster,
    so no communities, so re-chunk and re-embed the entire project — on every reconcile
    pass, with nothing that can ever satisfy the gate. Measured before it: 9 of 160
    projects, 27,301 chunks per pass, 22,741 of them one Katalon repo that yields 0
    symbols by construction and completed a full 76s re-index at 17:40:39-17:41:55.
    Cadence is configuration: the fleet was on RSE_RECONCILE_RESYNC_S=1800 (the vector
    migration drop-in), and at the default 0 it is once per daemon start instead — so
    the cost is a slower restart rather than a permanent burn. Wrong either way.

    A symbol-free graph is not an un-derived one. `_graph_stale` decides whether it
    needs re-deriving, on the same fingerprint as every other project.
    """
    return gs.symbol_count() > 0 and gs.community_count() == 0


def _graph_stale(path: str, gs) -> bool:  # gs: GraphStore
    """True if algo-version or code-only source fingerprint has drifted from stored stamps.

    Also true once enough incremental watcher passes have accumulated: `symbol_id` is
    (file, name, start_line), so inserting a line shifts every symbol below it and silently
    drops the *incoming* edges from files the watcher did not re-scan. Full re-derive stays
    the backstop — the incremental path is a fast path, not a replacement.
    """
    return (
        gs.get_meta("algo_version") != _pipeline_algo_version()
        or int(gs.get_meta("incremental_since_full") or 0) >= _INCREMENTAL_COMPACT_AT
        or gs.get_meta("source_sig") != _code_source_fingerprint(path)
    )


_JS_EXT = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue", ".svelte")


class _ImportResolver:
    """Turns a module specifier into the file it names, for one repo root.

    Why this lives here and not in `extractor.py`: a specifier is a *string the source declares*
    and the extractor's job ends at reading it, but resolving it needs the repo root, the
    manifests, and the filesystem — none of which a per-file parse has. Same split as call
    resolution, which is in `_extract_graph` for the same reason and not in the extractor either.

    HR15: nothing here infers code semantics. Each rule is a *published* path convention read
    from a declarative file the repo checked in — `go.mod`'s module line, `composer.json`'s
    `autoload.psr-4` map — or, for the JS family and python, path arithmetic whose answer is then
    checked against the filesystem. A resolution either names a file that exists or it is dropped;
    nothing is guessed and nothing falls back to a name match.
    """

    def __init__(self, root) -> None:
        self.root = root
        self._go_module = self._read_go_module()
        # Read on first php specifier, not here: finding composer.json is an `rglob` over the
        # whole tree, and the overwhelming majority of stores in the fleet hold no php at all.
        self._psr4: list[tuple[str, list]] | None = None
        # python's second anchor. `src/` layout is a packaging convention, not a guess: it is
        # resolved against the filesystem like every other candidate below.
        self._py_anchors = [root]
        if (root / "src").is_dir():
            self._py_anchors.append(root / "src")

    def _read_go_module(self) -> str:
        gomod = self.root / "go.mod"
        if not gomod.is_file():
            return ""
        try:
            for line in gomod.read_text(errors="replace").splitlines():
                if line.startswith("module "):
                    return line.split(None, 1)[1].strip()
        except OSError:
            pass
        return ""

    def _read_psr4(self) -> list[tuple[str, list]]:
        """PSR-4 prefix -> directories, from every non-vendor composer.json under the root.

        Longest prefix first, because PSR-4 resolves by longest match: a repo declaring both
        `App\\` and `App\\Domain\\` means the second to win for `App\\Domain\\Order`.
        """
        from rag_search.index.discover import is_ignored_path
        out: dict[str, list] = {}
        try:
            manifests = [p for p in self.root.rglob("composer.json")
                         if not is_ignored_path(p, self.root)]
        except OSError:
            return []
        for cj in manifests[:200]:
            try:
                data = _json.loads(cj.read_text(errors="replace"))
            except (ValueError, OSError):
                continue
            if not isinstance(data, dict):
                continue
            for block in ("autoload", "autoload-dev"):
                psr4 = (data.get(block) or {}).get("psr-4") if isinstance(
                    data.get(block), dict) else None
                if not isinstance(psr4, dict):
                    continue
                for prefix, dirs in psr4.items():
                    if isinstance(dirs, str):
                        dirs = [dirs]
                    if not isinstance(dirs, list):
                        continue
                    out.setdefault(prefix.rstrip("\\"), []).extend(
                        cj.parent / d for d in dirs if isinstance(d, str))
        return sorted(out.items(), key=lambda kv: -len(kv[0]))

    def resolve(self, spec: str, src, family: str) -> str:
        if family == "js":
            return self._js(spec, src)
        if family == "python":
            return self._py(spec)
        if family == "go":
            return self._go(spec)
        if family == "php":
            return self._php(spec)
        return ""

    def _js(self, spec: str, src) -> str:
        # Bare specifiers are package names, and a package resolves into `node_modules`, which is
        # in IGNORED_DIRS and so holds no indexed file. Only relative paths can name one.
        if not spec.startswith("."):
            return ""
        try:
            base = (src.parent / spec).resolve()
        except (OSError, ValueError):
            return ""
        for cand in (base, *(base.with_suffix(e) for e in _JS_EXT),
                     *(base / f"index{e}" for e in _JS_EXT)):
            if cand.is_file():
                return str(cand)
        return ""

    def _py(self, spec: str) -> str:
        parts = [p for p in spec.split(".") if p]
        if not parts:
            return ""
        for anchor in self._py_anchors:
            cand = anchor.joinpath(*parts)
            if cand.with_suffix(".py").is_file():
                return str(cand.with_suffix(".py"))
            if (cand / "__init__.py").is_file():
                return str(cand / "__init__.py")
        return ""

    def _go(self, spec: str) -> str:
        """A go import names a *package*, which is a directory. Name its first file.

        Directory-not-file is the one place this layer is lossy, and deliberately: a file-level
        edge into a package's first file is a truthful statement that the importer depends on
        that package, and fanning out to every file in it would multiply one declared fact into
        N asserted ones.
        """
        if not self._go_module or not spec.startswith(self._go_module):
            return ""
        rel = spec[len(self._go_module):].lstrip("/")
        d = self.root / rel if rel else self.root
        if not d.is_dir():
            return ""
        try:
            gos = sorted(p for p in d.glob("*.go") if not p.name.endswith("_test.go"))
        except OSError:
            return ""
        return str(gos[0]) if gos else ""

    def _php(self, spec: str) -> str:
        spec = spec.lstrip("\\")
        if "\\" not in spec:
            return ""
        if self._psr4 is None:
            self._psr4 = self._read_psr4()
        for prefix, dirs in self._psr4:
            if not spec.startswith(prefix):
                continue
            rel = spec[len(prefix):].lstrip("\\").replace("\\", "/")
            if not rel:
                continue
            for d in dirs:
                cand = (d / rel).with_suffix(".php")
                if cand.is_file():
                    return str(cand)
        return ""


def _extract_graph(gs, root, only: list | None = None) -> None:
    """Extract symbols + call edges from source into gs.

    `only` restricts the walk to those files (the watcher's incremental path); the callee
    resolution tables are still built from the whole symbol table, so calls into untouched
    files still resolve. Pass `only=None` for a full re-derive.

    S10, stated because it is a real gap and not worth papering over: emission now depends on how
    many definitions share a name *globally*, so on the incremental path a newly added definition
    can turn a resolved call ambiguous for call sites in files that pass never re-walks, and the
    now-wrong edge survives until the next full re-derive. The unconditional fan-out had the same
    shape of gap for the same reason; the repair is reconcile, not a wider walk here.

    A full re-derive subtracts *after* it adds, and the caller must no longer `gs.clear()` first.
    `upsert_symbol`/`upsert_edge` are keyed, so the pass writes straight into the live tables and
    every concurrent reader sees `old | new` — a superset — until the prune below cuts it to
    exactly `new`. Clearing up front committed an empty graph and served it for the length of the
    extraction; see `docs/decisions/2026-07-31-atomic-graph-rederive.md`.

    The incremental path prunes nothing here: it walked a subset, so "not re-recorded" would mean
    "belongs to a file this pass never opened". `delete_file_symbols` is its subtraction, per file.
    """
    from pathlib import Path

    from rag_search.graph.extractor import (
        extract_symbols_calls_with_stats,
        symbol_id,
    )
    from rag_search.graph.php_receivers import FileFacts, Resolver
    from rag_search.index.bounded_parse import PARSE_CRASHED, PARSE_TIMEOUT, run_bounded
    from rag_search.index.discover import detect_language, iter_files, language_family
    targets = list(only) if only is not None else None
    # S8: the host language of a file, cached. Symbols carry the *inner* language — a symbol
    # lifted out of a .vue <script> block is stamped "javascript" — so the symbol row cannot
    # answer "what grammar was this file written in". The path can, and deriving it here is
    # what S9 asks for without adding a column that would have to be kept in sync.
    fam_of: dict[str, str] = {}
    # What this pass re-recorded, and so what survives the prune. Only populated for a full
    # re-derive — `targets is not None` walks a subset and must not subtract on that basis.
    seen_sids: set[str] = set()
    seen_files: set[str] = set()
    seen_edges: set[tuple[str, str]] = set()
    # S11: call sites, held between the two halves below. Resolution needs the whole symbol
    # table, which does not exist until the walk finishes — that ordering is why there were two
    # passes. It never required a second walk, a second `read_text` or a second `run_bounded`
    # round trip. Measured at ~11 MB for the largest project in the fleet (2,516 files, 94,867
    # call sites), against a daemon already resident at ~1 GB.
    calls_by_file: dict[str, list[tuple[str, int]]] = {}
    # S12: import specifiers, held for the same reason as S11 — resolution has to check the
    # target against the *indexed* file set, which is `seen_files` plus whatever the store
    # already holds, and neither exists until the walk finishes.
    specs_by_file: dict[str, list[str]] = {}
    seen_imports: set[tuple[str, str]] = set()
    # S14: PHP receiver facts, held for the third instance of the same reason — narrowing a callee
    # pool by the receiver's declared type needs the *fleet* of classes this root declares, which
    # the walk has not finished collecting. Empty for a root with no PHP, and the whole tier below
    # is skipped in that case, which is most of the fleet.
    php_facts: dict[str, FileFacts] = {}

    def _fam(fstr: str) -> str:
        f = fam_of.get(fstr)
        if f is None:
            f = fam_of[fstr] = language_family(detect_language(Path(fstr)))
        return f

    for fpath in (targets if targets is not None else iter_files(root, federation_mode=True)):
        try:
            content = fpath.read_text(errors="replace")
        except OSError:
            continue
        lang = detect_language(fpath)
        # Walked, so it keeps its `file_extraction` row whichever branch below records it — a file
        # that yields no symbols is exactly what that table exists to account for.
        seen_files.add(str(fpath))
        res = run_bounded(extract_symbols_calls_with_stats, (fpath, content, lang),
                          path_for_log=str(fpath))
        # Recorded, never skipped silently — and recorded as the three *different* things they
        # are. A worker that ran past its deadline is load-dependent and may pass next sweep; a
        # worker that died is reproducible and points at the input (measured: `process()`
        # segfaults on a 10,000-deep expression); a worker-side exception is a bug in the
        # extraction function itself. They were one rung until `run_bounded` learned to tell the
        # first two apart, which meant a systematically crashing grammar read as a scattering of
        # slow files and `parse_timeout_count` corroborated it.
        if res is None or res in (PARSE_TIMEOUT, PARSE_CRASHED):
            rung = {PARSE_TIMEOUT: "timeout", PARSE_CRASHED: "crashed"}.get(res, "error")
            gs.record_extraction(str(fpath), lang, rung, 0, 0, 1)
            continue
        syms, st, call_sites, import_specs, facts = res
        gs.record_extraction(str(fpath), lang, st.rung, st.symbol_count,
                             st.anon_count, int(st.has_error))
        if call_sites:
            calls_by_file[str(fpath)] = call_sites
        if import_specs:
            specs_by_file[str(fpath)] = import_specs
        if facts is not None:
            php_facts[str(fpath)] = facts
        for sym in syms:
            if not sym.name:
                continue
            sid = symbol_id(sym.file, sym.name, sym.start_line)
            seen_sids.add(sid)
            gs.upsert_symbol(sid, sym.name, sym.qualified_name, sym.kind,
                             sym.file, sym.start_line, sym.end_line, sym.language)
    gs.commit()
    # Before the resolution tables are built, not after: they are read straight out of `symbols`
    # below, so a stale row surviving to that point would resolve calls onto a definition this
    # pass did not find. `purge_dangling_edges` then drops the edges those rows anchored; the
    # edges retracted because their *call site* went are handled by `prune_edges_to` at the end.
    if targets is None:
        gs.prune_symbols_to(seen_sids, seen_files)
        gs.purge_dangling_edges()
    gs.dedup_symbols()
    # S8: keyed by (family, name), not name. Keying on the bare name is what bound every
    # javascript `get()` to every PHP `get()` in the same repo — 1.09 M edges fleet-wide,
    # 5.97 % of all of them, measured 2026-07-28 across 151 projects with edges. A call can
    # only reach a definition its own grammar could import, so the family is part of the key.
    name_to_entries: dict[tuple[str, str], list[tuple[str, str]]] = {}
    file_to_sym_spans: dict[str, list[tuple[int, int, str]]] = {}
    for (sid, name, fstr, sl, el) in gs._con.execute(
        "SELECT sid, name, file, start_line, end_line FROM symbols"
    ):
        name_to_entries.setdefault((_fam(fstr), name), []).append((sid, fstr))
        if fstr:
            file_to_sym_spans.setdefault(fstr, []).append((sl, el, sid))
    for spans in file_to_sym_spans.values():
        spans.sort()
    # S14: built once per root, over every PHP file the walk parsed — a receiver's class is very
    # often declared in a different file from the call, so a per-file resolver would resolve
    # almost nothing. `None` when the root holds no PHP, which is the common case.
    php = Resolver(_ImportResolver(root)._read_psr4(), php_facts) if php_facts else None
    for fstr, call_sites in calls_by_file.items():
        sym_spans = file_to_sym_spans.get(fstr)
        if not sym_spans:
            continue
        facts = php_facts.get(fstr)
        hints = facts.hints() if facts is not None else {}
        for callee_name, call_line in call_sites:
            caller_sid = _innermost_symbol(sym_spans, call_line)
            if not caller_sid:
                continue
            pool = _callee_pool(name_to_entries.get((_fam(fstr), callee_name), []),
                                caller_sid, fstr, php, facts,
                                hints.get((callee_name, call_line), ""))
            for callee_sid in pool:
                seen_edges.add((caller_sid, callee_sid))
                gs.upsert_edge(caller_sid, callee_sid)
    # S12: import edges, resolved after the symbol pass for the same ordering reason the call
    # resolution above has — the target has to be checked against the set of files this index
    # actually holds, and a specifier naming a file outside it (a vendored package, the stdlib)
    # is dropped rather than recorded as a dangling edge.
    if specs_by_file:
        indexed = {r[0] for r in gs._con.execute("SELECT file FROM file_extraction")}
        resolver = _ImportResolver(root)
        for fstr, specs in specs_by_file.items():
            for spec in specs:
                dst = resolver.resolve(spec, Path(fstr), _fam(fstr))
                if dst and dst != fstr and dst in indexed:
                    seen_imports.add((fstr, dst))
                    gs.upsert_import(fstr, dst)
    gs.commit()
    if targets is None:
        gs.prune_edges_to(seen_edges)
        gs.prune_imports_to(seen_imports)



def _innermost_symbol(sym_spans: list[tuple[int, int, str]], call_line: int) -> str:
    """The tightest symbol span containing `call_line`, or "" — that is the caller.

    Tightest, not first: spans nest, and the enclosing class would otherwise win over the method
    the call is actually written in."""
    caller_sid, best_span = "", -1
    for sl, el, sid in sym_spans:
        if sl <= call_line <= el:
            span = el - sl
            if caller_sid == "" or span < best_span:
                best_span, caller_sid = span, sid
    return caller_sid


def _callee_pool(cands, caller_sid: str, fstr: str, php, facts, hint: str) -> list[str]:
    """The definitions one call site resolves to — empty when the evidence does not single one out.

    `cands` is every same-family definition of that name, as (sid, file)."""
    import collections

    # Same file is the *preferred scope*, which is what every language's scoping rules
    # do — neither an exclusion (excluding it left `callers`/`callees` unable to answer
    # any relation inside one file) nor a mere inclusion (including the whole family
    # bound one call site to N definitions, 95.7% of fleet edges). Past the preferred
    # scope there is no evidence here to choose with, so emit nothing rather than N-1
    # wrong edges. See docs/decisions/2026-07-31-an-edge-is-a-resolved-call.md.
    same_file = [sid for sid, cfile in cands if cfile == fstr]
    # The self-drop comes after the tier is chosen, never before. Choosing first means a
    # recursive call in the only file defining that name finds an empty tier and falls
    # through to a same-named definition in some other file — a confidently wrong edge.
    # Worth 996 edges fleet-wide.
    pool = [sid for sid in (same_file or [sid for sid, _ in cands]) if sid != caller_sid]
    # S14: narrowing happens *before* the cap, never after, and it does not raise the cap.
    # A pool of five that the receiver's declared type cuts to one is not the cap being
    # relaxed to five — it is a pool of one, qualified by evidence the name-only tier
    # never looked at, and it clears the existing bar on its own terms. Above the cap and
    # unnarrowed, the edge is still dropped; that is why precision does not move.
    if php is not None and facts is not None and len(pool) > _MAX_CALLEE_FANOUT:
        won = php.narrow(facts, hint,
                         collections.Counter(
                             cfile for sid, cfile in cands if sid != caller_sid))
        if won:
            pool = [sid for sid, cfile in cands if cfile == won and sid != caller_sid]
    return [] if len(pool) > _MAX_CALLEE_FANOUT else pool

def _persist_partition_quality(gs) -> None:  # type: ignore[no-untyped-def]
    """Persist partition-quality verdict to the meta table immediately after detect_communities.
    The sig (symbol:edge:community counts) self-invalidates on any content change, so the
    read-side (overview status) can use the cached verdict for O(1) lookup instead of a full scan."""
    import json as _json

    from rag_search.graph.quality import partition_quality
    hq = partition_quality(gs)
    sig = f"{gs.symbol_count()}:{gs.edge_count()}:{gs.community_count()}"
    gs.set_meta("partition_quality", _json.dumps({"sig": sig, "q": hq}))


def _rederive_graph(project_path: str) -> None:
    """Re-extract symbols+edges, re-detect communities, wipe stale L2+, stamp meta."""
    from pathlib import Path

    from rag_search.core.config import project_graph_db
    from rag_search.graph.community import detect_communities
    from rag_search.graph.store import GraphStore
    root = Path(project_path)
    gs = GraphStore(project_graph_db(project_path))
    try:
        _extract_graph(gs, root)
        detect_communities(gs)
        gs._con.execute("DELETE FROM communities WHERE level>=2")
        gs.commit()
        gs.set_meta("algo_version", _pipeline_algo_version())
        gs.set_meta("source_sig", _code_source_fingerprint(project_path))
        gs.set_meta("incremental_since_full", "0")
        _persist_partition_quality(gs)
        gs.commit()
    finally:
        gs.close()
    log.info("_rederive_graph %s: re-extracted and re-detected", project_path)


def _graph_needs_update(project_path: str) -> bool:
    """True if the watcher should re-extract this project's graph for the current change.

    Mirrors reconcile's own conditions so the two paths cannot drift apart, minus the one case
    that belongs to reconcile: a project with no graph.db yet has never been indexed.

    A federation root is not one of those cases, though it was exempted here on two reasons that
    both failed. First HR4 ("0 own communities by design"), which HR4 does not say. Then a cost
    argument from HR40 — refuted by measuring it: `iter_files` takes `federation_mode=True`,
    which prunes the symlinks escaping the root, so the fleet's one root walks **5,966 files in
    1.41 s**, fewer than the 7,697 of the first member the watcher already re-extracts on every
    edit. A root is shard 0 of the query-time union, not its broker — it owns its own store like
    any member, and the only thing federation changes is that *reads* union the shards.
    Reuses the memoised code-only fingerprint on_change has already computed — no second walk.
    """
    from rag_search.core.config import project_graph_db
    from rag_search.graph.store import GraphStore
    gdb = project_graph_db(project_path)
    if not gdb.exists():
        return False
    gs = GraphStore(gdb)
    try:
        return _graph_stale(project_path, gs)
    finally:
        gs.close()


def _graph_reconcile_action(project_path: str) -> str | None:
    """What reconcile owes this project's graph: "index", "rederive", or nothing.

    Reconcile's half of the hand-off `_graph_needs_update` defers on. Deliberately reads
    only *state* — the graph's own stamps — never the project's role. The role predicate
    that stood here (`not entry.federation`) skipped every federation root on the claim
    that a root has "0 own communities by design (HR4)", which HR4 does not say: HR4
    forbids cross-repo *edges*. Measured when it was removed, the sole root in the fleet
    carried 342 own symbols and 20 communities, so the guard was wrong for 100% of the
    population it governed, and had frozen that graph two algorithm generations behind
    the 159 members it fans out into. A root is shard 0 of the union, not its broker.

    The symbol-free case the role predicate was reaching for is already handled, and for
    the true reason: `_graph_needs_full_index` requires `symbol_count() > 0`.
    """
    from rag_search.core.config import project_graph_db
    from rag_search.graph.store import GraphStore
    gdb = project_graph_db(project_path)
    if not gdb.exists():
        return None
    gs = GraphStore(gdb)
    try:
        if _graph_needs_full_index(gs):
            return "index"
        return "rederive" if _graph_stale(project_path, gs) else None
    finally:
        gs.close()


def _update_graph_files(project_path: str, files: list) -> None:
    """Re-extract just `files` into the existing graph, then re-detect communities whole.

    Extraction is single-worker-bound (HR40) and scales with repo size, not edit size — a full
    re-derive costs ~190s on a 17k-file repo, which is not something a 45s debounce window can
    afford. detect_communities is <4s even there, so it is simply re-run.

    Deletion is deliberately not filtered by is_ignored_path: a file that was removed, renamed
    or newly gitignored must still lose its rows, and `upsert_symbol` alone never subtracts.

    An algo bump or an exhausted compaction budget falls through to the full re-derive here
    rather than being left for reconcile — reconcile is startup-once by design, so deferring
    to it would freeze that project's graph until the next daemon restart.
    """
    from pathlib import Path

    from rag_search.core.config import project_graph_db
    from rag_search.graph.community import detect_communities
    from rag_search.graph.store import GraphStore
    from rag_search.index.discover import detect_language, is_code_language, is_ignored_path
    root = Path(project_path)
    changed = [Path(f) for f in files if is_code_language(detect_language(Path(f)))]
    if not changed:
        return
    targets = [p for p in changed if p.exists() and not is_ignored_path(p, root)]
    owed = True  # assume the expensive path until the cheap one is proven safe
    gs = GraphStore(project_graph_db(project_path))
    try:
        owed = (gs.get_meta("algo_version") != _pipeline_algo_version()
                or int(gs.get_meta("incremental_since_full") or 0) >= _INCREMENTAL_COMPACT_AT)
        if not owed:
            for fpath in changed:
                gs.delete_file_symbols(str(fpath))
                gs.delete_file_imports(str(fpath))
            gs.commit()
            if targets:
                _extract_graph(gs, root, only=targets)
            # Only now: a symbol whose start_line did not move keeps its sid, so its incoming
            # edges are live again and must not be swept as dangling.
            gs.purge_dangling_edges()
            detect_communities(gs)
            gs._con.execute("DELETE FROM communities WHERE level>=2")
            passes = int(gs.get_meta("incremental_since_full") or 0) + 1
            gs.set_meta("incremental_since_full", str(passes))
            gs.set_meta("source_sig", _code_source_fingerprint(project_path))
            _persist_partition_quality(gs)
            gs.commit()
            log.info("_update_graph_files %s: %d file(s) re-extracted (pass %d)",
                     project_path, len(targets), passes)
    finally:
        gs.close()
    if owed:
        _rederive_graph(project_path)  # must run with the store closed — it reopens it


def _reindex_vectors(project_path: str) -> None:
    """Rebuild only the vector index, leaving the graph and its summaries intact.

    An embed-signature drift invalidates vectors and nothing else — chunk shape has
    no bearing on the tree-sitter graph derived from it. Routing drift through
    _index_project would gs.clear() the graph and force a full re-extract and
    re-cluster of the whole project to fix a chunking bug.
    """
    from pathlib import Path

    from rag_search.core.config import project_vector_db
    from rag_search.core.registry import get_project, upsert_project
    from rag_search.embed.embedder import get_embedder
    from rag_search.index.indexer import index_project
    from rag_search.index.store import VectorStore

    # Read the watermark before indexing, never after: a file written while this runs must
    # stay stale rather than be marked done by a stamp taken at the end.
    newest = _newest_code_mtime(project_path)
    vs = VectorStore(project_vector_db(project_path))
    try:
        file_count, chunk_count = index_project(Path(project_path), get_embedder(), vs)
        vs.set_meta(_VECTORS_MTIME_KEY, repr(newest))
    finally:
        vs.close()
    # indexed_at is deliberately left alone: it marks graph+vector completeness for
    # _needs_index, and this pass rebuilt only half of that.
    if (entry := get_project(project_path)) is not None:
        entry.file_count = file_count
        entry.chunk_count = chunk_count
        upsert_project(entry)
    log.info("_reindex_vectors %s: %d files -> %d chunks", project_path, file_count, chunk_count)


def _needs_index(path: str) -> bool:
    """True if this project's index is absent or never completed.

    Keys on registry indexed_at (set only at the end of a successful _index_project)
    so a partial/aborted index with stray chunks is still treated as needing re-index.
    """
    import sqlite3

    from rag_search.core.config import project_vector_db
    from rag_search.core.registry import get_project

    e = get_project(path)
    if e is None or e.indexed_at is None:
        return True  # never completed; stray partial chunks do not count
    vdb = project_vector_db(path)
    if not vdb.exists():
        return True
    try:
        with sqlite3.connect(str(vdb)) as con:
            return con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 0
    except Exception:
        return True


def _needs_labels(path: str) -> bool:
    """True if any community in this project's graph is missing a summary.

    Still terminates now that labelling is structural: label_community_structural writes a
    templated summary ("N symbol(s) (kinds) from files. Primary: …"), never NULL or "", so a
    completed pass always clears this gate. If it ever stopped writing one, reconcile would
    spin forever with nothing in the journal — which is why DK4a asserts this directly.
    """
    import sqlite3

    from rag_search.core.config import project_graph_db

    gdb = project_graph_db(path)
    if not gdb.exists():
        return False
    try:
        with sqlite3.connect(str(gdb)) as con:
            n = con.execute(
                "SELECT COUNT(*) FROM communities WHERE summary IS NULL OR summary = ''"
            ).fetchone()[0]
            return n > 0
    except Exception:
        return False


def _has_vectors(path: str) -> bool:
    """Cheap "has this project ever been embedded" test — one stat, no sqlite open."""
    from rag_search.core.config import index_dir
    try:
        return (index_dir(path) / "vectors.db").exists()
    except OSError:
        return False


def _graph_algo_drifted(path: str, current: str) -> bool:
    """Cheap "has the current extractor never run on this graph" test — one read-only sqlite open.

    Deliberately *not* `_graph_stale`: that also compares `source_sig`, which costs a
    `_code_source_fingerprint` tree walk per project, and this runs for every registry row on
    every pass. The stamp alone separates the population this key exists to promote. A store
    that matches the stamp but has drifted on the fingerprint is still repaired — by
    `_graph_reconcile_action` once the walk reaches it — it just does not get promoted.

    `mode=ro` without `immutable=1`: these stores are WAL and the daemon is their writer, and
    `immutable=1` fails outright on a WAL database, which would read as "no drift" for every
    project in the fleet — a silent unsort rather than a loud error.
    """
    import sqlite3

    from rag_search.core.config import project_graph_db
    try:
        gdb = project_graph_db(path)
        if not gdb.exists():
            return False  # nothing to re-derive; `_has_vectors` is the key that covers this row
        con = sqlite3.connect(f"file:{gdb}?mode=ro", uri=True, timeout=1.0)
        try:
            row = con.execute("SELECT value FROM meta WHERE key='algo_version'").fetchone()
        finally:
            con.close()
    except (OSError, sqlite3.Error):
        return False
    return bool(row) and row[0] != current


def reconcile_order(rows: list) -> list:
    """Never-embedded projects first, then most-recently-touched.

    The second key alone was here first, and it fixed a real starvation: an unordered walk ground
    through 198 projects for 7.6 h without reaching either repo edited that day. But it is keyed on
    `last_change_seen`, which a project that has never been indexed does not have — so `or ""` sorts
    all of them to the *end* of a `reverse=True` walk. Measured on this host 07-30, after the
    registry wipe: 157 of 210 enabled rows held zero chunks and **157 of 157** had an empty key.

    That is only latent until something stops the walk early, and `is_paused()` does exactly that
    with no resume cursor — every pass restarts at position 0. So the walk repeatedly re-paid the
    full `_index_set_drift` tree scan for the 53 converged projects at the head and never once
    reached the 157 at the tail. A live suite holds the pause lease for its whole run, which made
    running the tests the thing preventing the rebuild the tests were waiting on.

    Ordering by need rather than by recency breaks that: a project with no vectors returns *nothing*
    for a search, while a stale one returns slightly old results, so "never embedded" outranks "not
    touched lately" whenever the two disagree. In steady state nothing has an empty store and this
    key is constant, which is why the original ordering keeps working — this only changes the walk
    when there is a backlog, which is precisely when the walk gets cut short.

    A third key sits between those two as of 2026-07-31: graphs the current extractor has never
    run on. Recency does not cover *pipeline* drift — for a graph re-derive every store already
    has vectors, so the first key is constant across the whole population and the sort degenerates
    to recency, under which a stale store (by definition one nothing has touched lately) lands at
    the *tail* of a `reverse=True` walk. Measured before this changed: the 16 current stores sat at
    walk positions 59-74 while the 128 stale ones spanned 58-201, median 137, and no pass had
    reached past position 16 since the stamp last moved.

    Never-embedded still outranks drifted, on this function's own argument: no vectors returns
    *nothing* for a search, drifted returns real results from an older extractor. That population
    is small (14 rows here) and drains in a pass or two, so it does not hold the drifted set back.

    Kept out of `reconcile_projects` so the ordering can be asserted without a GPU, a daemon, or the
    six-hour job that calls it.
    """
    current = _pipeline_algo_version()  # hoisted: the key runs once per row, this must not
    return sorted(rows, key=lambda e: (not _has_vectors(e.path),
                                       _graph_algo_drifted(e.path, current),
                                       e.last_change_seen or ""),
                  reverse=True)


def _cursor_file():
    """Beside the registry, so `RSE_REGISTRY_PATH` isolates the cursor along with the projects."""
    from rag_search.core.config import REGISTRY_PATH
    return REGISTRY_PATH.with_name("reconcile_cursor")


def _load_cursor() -> str:
    """Project path the last truncated pass stopped at, or "" for "start at the head"."""
    import contextlib
    with contextlib.suppress(OSError):
        return _cursor_file().read_text(encoding="utf-8").strip()
    return ""


def _save_cursor(path: str) -> None:
    """Persisted rather than held in a global: a restart is exactly when the walk resets, and
    `_PAUSED` already taught this file what an un-restorable module global costs."""
    import contextlib
    with contextlib.suppress(OSError):
        f = _cursor_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(path, encoding="utf-8")


def _resume_at(walk: list, cursor: str) -> list:
    """Rotate `walk` to begin at `cursor`, wrapping once. Unknown/empty cursor: unchanged.

    Rotation rather than a stored integer offset because the walk is re-sorted and re-sized every
    pass (198 -> 203 -> 210 -> 216 across one afternoon here), so an index points at an arbitrary
    different project next time. Rotating preserves the priority ordering for everything after the
    resume point and still reaches every row within one lap.
    """
    if not cursor:
        return walk
    for i, e in enumerate(walk):
        if e.path == cursor:
            return walk[i:] + walk[:i]
    return walk  # cursor's project left the registry — fall back to the priority head


def reconcile_projects() -> None:
    """Idempotent: discover+register members, index any unindexed/stalled project, label any
    project with missing community summaries (any level).  Safe to call repeatedly.

    A truncated pass records where it stopped and the next pass resumes there. Without that, a
    pause returned at position 0 and the next pass restarted at position 0, so the walk re-paid
    the head forever and never reached the tail: measured 2026-07-31, nine consecutive passes
    abandoned at 0-16 of 216 while all 128 stale stores sat at positions 58-201 — **none of them
    reachable by any pass**. A live suite holds the pause lease for its whole run, so truncation
    is the normal case, not the exception: running the tests was the thing preventing the rebuild
    the tests were waiting on.
    """
    if is_paused():
        # Suspension is a state, not a discard (cf. Flux `suspend`) — but _PAUSED is a bare
        # global with no nesting, so a pause landing inside the startup grace silently
        # abandoned the whole pass. It cost days of a fleet migration to find, precisely
        # because both this and the mid-walk return below logged nothing.
        log.info("reconcile: abandoned before start (sweeps paused)")
        return
    from rag_search.core.registry import list_projects
    from rag_search.daemon.federation import register_all_members

    t0 = time.monotonic()
    try:
        register_all_members()
    except Exception as exc:
        log.warning("reconcile member-discovery: %s", exc)

    # Re-arm the watcher against the registry once per pass. `upsert_project` already
    # notifies on a membership change, but `_migrate()` prunes dead entries without one
    # (it is called from `list_projects`, so notifying there would recurse) — this is the
    # path that catches those. A no-change sync returns without touching the watch.
    from rag_search.daemon.server import sync_watcher
    sync_watcher()

    from rag_search.core.config import is_federation_excluded

    # Never-embedded first, then most-recently-touched. See `reconcile_order` — the second key
    # alone sent every unindexed project to the tail of a walk that `is_paused()` truncates.
    walk = _resume_at(reconcile_order(list_projects()), _load_cursor())
    for pos, entry in enumerate(walk):
        if is_paused():
            # Name the position: "reconcile stopped" is not actionable, "stopped at
            # 105/160" tells you how much of the walk never ran.
            log.info("reconcile: abandoned at %d/%d (sweeps paused), resuming there next pass",
                     pos, len(walk))
            _save_cursor(entry.path)
            return
        if not entry.enabled:
            continue
        if is_federation_excluded(entry.path):
            continue
        needs_idx = _needs_index(entry.path)
        needs_rederive = needs_vectors = False
        if not needs_idx and (drifted := _vectors_stale(entry.path)):
            from rag_search.core.config import AUTO_MIGRATE_VECTORS
            from rag_search.index.store import embed_signature
            log.warning(
                "%s: vectors built by %r, config is now %r — %s",
                entry.path, drifted, embed_signature(),
                "re-embedding" if AUTO_MIGRATE_VECTORS
                else "set RSE_AUTO_MIGRATE_VECTORS=1 to migrate",
            )
            needs_vectors = bool(AUTO_MIGRATE_VECTORS)
        # The third trigger. Without it, a project whose watcher stream goes quiet keeps
        # a re-derived graph over vectors from months ago and reports itself healthy;
        # the only symptom is `search` answering from source that no longer exists.
        if not needs_idx and not needs_vectors and (
            missed := _vectors_content_stale(entry.path)
        ):
            log.info(
                "%s: %d file(s) newer than vectors — re-embedding them",
                entry.path, len(missed),
            )
            try:
                _index_files(entry.path, missed)
            except Exception as exc:
                log.warning("%s: content-freshness reindex failed: %s", entry.path, exc)
        # The fourth trigger, and the only one that is a completeness check rather than a change
        # detector. The three above all reduce to "something is newer than the last pass", which
        # is blind by construction to drift caused by discovery *policy* — a size cap, an
        # exclusion, a newly supported language — because no file's mtime moves when the rule
        # about it does. Level-triggered, in the Kubernetes sense: it compares desired against
        # actual and repairs the difference, so it also absorbs whatever the watcher's debounce
        # dropped instead of needing to have observed the event.
        if not needs_idx and not needs_vectors:
            try:
                unindexed, orphaned = _index_set_drift(entry.path)
            except Exception as exc:
                unindexed, orphaned = [], []
                log.warning("%s: index set check failed: %s", entry.path, exc)
            if orphaned:
                log.info("%s: purging %d indexed path(s) discovery no longer yields",
                         entry.path, len(orphaned))
                try:
                    _purge_paths(entry.path, orphaned)
                except Exception as exc:
                    log.warning("%s: orphan purge failed: %s", entry.path, exc)
            if unindexed:
                # Bounded per pass, not because the whole set is too expensive but because it is
                # one indivisible unit: a restart mid-`_index_files` discards everything it has
                # done, and the measured backlog here is 7,693 files for a single project. A cap
                # makes the repair resumable by construction — each pass keeps what it finished,
                # and the next pass re-derives the remainder from the store rather than from any
                # progress state. Convergence is unaffected; only its granularity is.
                batch = unindexed[:_DRIFT_REPAIR_MAX]
                log.info("%s: %d discoverable file(s) never indexed — indexing %d this pass",
                         entry.path, len(unindexed), len(batch))
                try:
                    _index_files(entry.path, batch)
                except Exception as exc:
                    log.warning("%s: set-drift reindex failed: %s", entry.path, exc)
        if not needs_idx:
            action = _graph_reconcile_action(entry.path)
            needs_idx = action == "index"
            needs_rederive = action == "rederive"
        try:
            # Orthogonal to the chain below, not a branch of it: re-embedding touches
            # only vectors.db, so it neither satisfies nor preempts a graph rederive.
            # needs_vectors is only ever set when needs_idx is False, so the two can
            # never both rebuild the same vectors.
            if needs_vectors:
                _reindex_vectors(entry.path)
            if needs_idx:
                _index_project(entry.path)
                _label_project(entry.path)
            elif needs_rederive:
                _rederive_graph(entry.path)
                _label_project(entry.path)
            elif _needs_labels(entry.path):
                _label_project(entry.path)  # label-only; skip expensive re-index
        except Exception as exc:
            log.warning("reconcile %s: %s", entry.path, exc)
    # A pass that repairs nothing used to log nothing at all, so "reconcile is healthy" and
    # "reconcile has not run since lunchtime" produced byte-identical journals — and the two
    # `abandoned` lines above, added for the same reason, only cover the paused case. That is how
    # a 4 h sweep outage stayed invisible on 2026-07-30 while every other signal read green.
    # Success is the common case, so it is the one that most needs a line to point at.
    # Cleared only on a lap that ran to the end, so the next pass starts at the priority head
    # again. Clearing it anywhere else would reintroduce the restart-at-0 bug it exists to fix.
    _save_cursor("")
    log.info("reconcile: pass complete over %d project(s) in %.1fs", len(walk),
             time.monotonic() - t0)


_VACUUM_BLOAT_BYTES: int = 256 * 1024 * 1024  # VACUUM when freelist > 256 MB


def _vacuum_if_bloated(db_path, threshold: int = _VACUUM_BLOAT_BYTES) -> bool:
    """VACUUM db_path when freelist occupies more than threshold bytes. Returns True if vacuumed."""
    import sqlite3
    from pathlib import Path
    p = Path(db_path)
    if not p.exists():
        return False
    try:
        with sqlite3.connect(str(p), timeout=10) as con:
            page_size = con.execute("PRAGMA page_size").fetchone()[0]
            freelist = con.execute("PRAGMA freelist_count").fetchone()[0]
            if page_size * freelist <= threshold:
                return False
            log.info("VACUUM %s (freelist=%d pages, ~%d MB)", p.name,
                     freelist, page_size * freelist // (1024 * 1024))
            con.execute("VACUUM")
            log.info("VACUUM %s done", p.name)
            return True
    except Exception as exc:
        log.warning("VACUUM %s: %s", p.name, exc)
        return False


def maintenance() -> None:
    """Vacuum orphan index dirs; reclaim fragmented SQLite space (bloat-gated)."""
    if is_paused():
        return
    import sqlite3  # noqa: F401 — ensure available before list_projects() import

    from rag_search.core.config import INDEX_ROOT, project_graph_db, project_vector_db
    from rag_search.core.orphans import (
        OrphanSweepRefusedError,
        expire_trash,
        orphan_dirs,
        quarantine,
    )
    from rag_search.core.registry import list_projects

    if not INDEX_ROOT.exists():
        return
    # Before filling the trash, not after: an expiry that ran afterwards would measure the age of
    # dirs this very pass created, and on a clock skew or a zero TTL would collect them immediately.
    expire_trash()
    # Never `allow_bulk`: this runs unattended every 6 h, so there is nobody here to read a refusal
    # and decide. A refused sweep costs disk until an operator looks; an unrefused wrong one costs a
    # full GPU re-index of the fleet. The vacuum pass below is unaffected and still runs.
    try:
        orphans = orphan_dirs()
    except OrphanSweepRefusedError as exc:
        log.error("orphan sweep refused, deleted nothing: %s", exc)
        orphans = []
    for d in orphans:
        dest = quarantine(d)
        log.info("vacuum orphan: %s -> %s", d, dest or "(left in place, quarantine failed)")

    for entry in list_projects():
        if not entry.enabled:
            continue
        _vacuum_if_bloated(project_vector_db(entry.path))
        _vacuum_if_bloated(project_graph_db(entry.path))


def _index_project(project_path: str) -> None:
    import time
    from pathlib import Path

    from rag_search.core.config import project_graph_db, project_vector_db
    from rag_search.embed.embedder import get_embedder
    from rag_search.graph.community import detect_communities
    from rag_search.graph.store import GraphStore
    from rag_search.index.indexer import index_project
    from rag_search.index.store import VectorStore

    root = Path(project_path)
    embedder = get_embedder()

    # 1. Chunk + embed → vectors.db. The span and chunk count are logged because a full unit
    # is the only place indexing throughput can be measured end to end — the incremental path
    # at _reindex_vectors logs its own, and without this one a completed unit left no trace at
    # all, so "new-chunks/min" had to be sampled with py-spy instead of read.
    t0 = time.monotonic()
    vs = VectorStore(project_vector_db(project_path))
    try:
        file_count, chunk_count = index_project(root, embedder, vs)
    finally:
        vs.close()
    span = time.monotonic() - t0
    log.info(
        "_index_project %s: %d files -> %d chunks in %.1fs (%.0f chunks/min)",
        project_path, file_count, chunk_count, span, chunk_count / span * 60 if span > 0 else 0,
    )

    # 2. Tree-sitter extract + community detection → graph.db; stamp pipeline meta.
    gs = GraphStore(project_graph_db(project_path))
    try:
        _extract_graph(gs, root)
        detect_communities(gs)
        gs.set_meta("algo_version", _pipeline_algo_version())
        gs.set_meta("source_sig", _code_source_fingerprint(project_path))
        gs.set_meta("incremental_since_full", "0")  # this IS the authoritative full pass
        _persist_partition_quality(gs)
        gs.commit()
    finally:
        gs.close()

    from datetime import UTC, datetime

    from rag_search.core.registry import get_project, upsert_project
    entry = get_project(project_path)
    if entry is not None:
        entry.indexed_at = datetime.now(UTC).isoformat()
        entry.last_change_seen = entry.indexed_at
        entry.file_count = file_count
        entry.chunk_count = chunk_count
        upsert_project(entry)


def _index_files(project_path: str, files: list) -> None:
    """Incremental reindex: re-embed only the changed files (no full-project rescan)."""
    from pathlib import Path

    from rag_search.core.config import project_vector_db
    from rag_search.core.index_config import effective_config
    from rag_search.embed.embedder import get_embedder
    from rag_search.index.discover import is_ignored_path
    from rag_search.index.indexer import index_files
    from rag_search.index.store import VectorStore

    root = Path(project_path)
    cfg = effective_config(root)
    filtered = [
        Path(str(f)) for f in files
        if not is_ignored_path(Path(str(f)), root, cfg)
    ]
    if not filtered:
        return
    # Stat before indexing, and only over the files this pass actually handled. A file the
    # debounce dropped is not in `filtered`, so it never advances the mark and the tree's
    # newest mtime stays above it — which is exactly how `_vectors_content_stale` still
    # catches a dropped event. Advancing from the whole tree instead would mask it.
    import contextlib
    handled = 0.0
    for f in filtered:
        with contextlib.suppress(OSError):
            handled = max(handled, float(int(f.stat().st_mtime)))
    vs = VectorStore(project_vector_db(project_path))
    try:
        index_files(filtered, get_embedder(), vs, project_root=root)
        prior = vs.get_meta(_VECTORS_MTIME_KEY)
        if handled and handled > float(prior or 0.0):
            vs.set_meta(_VECTORS_MTIME_KEY, repr(handled))
    finally:
        vs.close()

    from datetime import UTC, datetime

    from rag_search.core.registry import get_project, upsert_project
    entry = get_project(project_path)
    if entry is not None:
        entry.last_change_seen = datetime.now(UTC).isoformat()
        upsert_project(entry)


def _label_project(project_path: str) -> None:
    """Give every fastgreedy community a deterministic structural label. No LLM, no network.

    (Not Leiden — `graph/community.py` runs igraph `community_fastgreedy`, ALGO_VERSION "fg1",
    and `test_schema_consistency.py::SC8a` asserts leidenalg is never imported.)

    Only L1 communities are ever generated (WS-B), and the orphan prune below is what keeps
    this terminating: label_community_structural writes nothing for a community with no
    symbols, so an orphan row would hold _needs_labels True forever.
    """
    # Single-flight: at most one CPU-bound graph pass runs at a time across the watcher and
    # reconcile threads — caps daemon CPU at ~one core instead of pinning two concurrently.
    # Never held around index/embed or GPU queries, so search freshness is unaffected.
    with _HEAVY_LOCK:
        from rag_search.core.config import project_graph_db
        from rag_search.graph.community import label_community_structural
        from rag_search.graph.store import GraphStore

        gs = GraphStore(project_graph_db(project_path))
        try:
            gs._con.execute(
                "DELETE FROM communities WHERE level=1 AND id NOT IN "
                "(SELECT DISTINCT community_id FROM symbols WHERE community_id IS NOT NULL)"
            )
            gs.commit()
            cids = [r[0] for r in gs._con.execute(
                "SELECT id FROM communities WHERE (summary IS NULL OR summary = '') AND level = 1"
            ).fetchall()]
            for cid in cids:
                label_community_structural(gs, cid)
            gs.commit()
            gs._con.execute(
                "UPDATE communities SET title='Community-' || CAST(id AS TEXT) "
                "WHERE level=1 AND (title IS NULL OR title='')"
            )
            gs.commit()
            if cids:
                log.info("_label_project %s: labelled=%d", project_path, len(cids))
        finally:
            gs.close()


# ── graph lane ────────────────────────────────────────────────────────────────────────────
# The heavy half of on_change is serialised by _HEAVY_LOCK regardless, so running it on the
# watcher's dispatch workers never bought parallelism — it only meant a worker sat *blocked* on
# that lock. Measured on the live daemon: one worker inside bounded_parse holding the lock, the
# other parked on it for 4+ minutes, and a third project's edit never indexed because both
# workers were consumed. Moving the reader off the work was necessary but not sufficient.
#
# One dedicated lane does the same serialised work without occupying a worker, so the cheap half
# (_index_files — what actually makes an edit searchable) always runs promptly.
_graph_lane_cv = threading.Condition()   # guards _graph_lane_wanted and _pending_graph_files
_graph_lane_wanted: dict[str, str] = {}  # project -> source sig to stamp once its pass lands
_graph_lane_thread: threading.Thread | None = None
_graph_lane_busy: bool = False           # a pass is running right now (queue-empty is not idle)


def _graph_lane_join(timeout: float = 120.0) -> bool:
    """Block until nothing is queued and no pass is in flight. False on timeout.

    on_change used to finish the heavy half before returning, so callers could read graph.db
    straight after it. That guarantee is what the lane moved, not what it removed — this hands
    it back to anyone who needs it (the WG gates read the graph the moment on_change returns).
    """
    import time
    with _graph_lane_cv:
        deadline = time.monotonic() + timeout
        while _graph_lane_wanted or _graph_lane_busy:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            # A deadline on a join, not a poll interval: the lane's own wait() is untimed.
            _graph_lane_cv.wait(remaining)
        return True


def _graph_lane_submit(project_path: str, sig: str) -> None:
    """Queue a project's heavy graph pass, coalescing with anything already queued for it.

    Started lazily rather than at import: a process that never sees a file event (a CLI
    invocation, a test importing this module) should not grow a thread for it.
    """
    global _graph_lane_thread
    with _graph_lane_cv:
        _graph_lane_wanted[project_path] = sig
        if _graph_lane_thread is None or not _graph_lane_thread.is_alive():
            _graph_lane_thread = threading.Thread(
                target=_graph_lane_run, daemon=True, name="rse-graph-lane")
            _graph_lane_thread.start()
        _graph_lane_cv.notify()


def _graph_lane_run() -> None:
    global _graph_lane_busy
    while True:
        with _graph_lane_cv:
            while not _graph_lane_wanted:
                # No timeout: the lane wakes on a submit, never on a clock. Same rule as the
                # watcher's dispatch workers — the only clock in this daemon is the kernel's.
                _graph_lane_cv.wait()
            project_path, sig = next(iter(_graph_lane_wanted.items()))
            del _graph_lane_wanted[project_path]
            # Popped here, not at submit time, so files that arrived while this project waited
            # its turn belong to this pass instead of provoking a second one.
            pending = _pending_graph_files.pop(project_path, set())
            # Set before releasing the lock: an empty queue is not an idle lane, and a join
            # that sampled the gap between pop and pass would return "done" mid-pass.
            _graph_lane_busy = True
        try:
            _graph_lane_pass(project_path, sig, pending)
        finally:
            with _graph_lane_cv:
                _graph_lane_busy = False
                _graph_lane_cv.notify_all()


def _graph_lane_pass(project_path: str, sig: str, pending: set) -> None:
    """One project's heavy pass. `sig` was sampled before the wait, so it can only ever claim
    *less* than this pass covered — a later event re-runs it, which is the safe direction."""
    try:
        if pending and _graph_needs_update(project_path):
            # Must be released before _label_project: _HEAVY_LOCK is a plain threading.Lock
            # and _label_project acquires it itself. Held here so extraction never runs
            # concurrently with another heavy graph pass (the daemon's ~1-core budget).
            with _HEAVY_LOCK:
                _update_graph_files(project_path, sorted(pending))
        _label_project(project_path)
        _last_labelled_sig[project_path] = sig
    except Exception as exc:
        # Put the batch back rather than stamping it done: source_sig is only written by a pass
        # that completed, so a lost batch here would leave the same silent phantom rows.
        with _graph_lane_cv:
            _pending_graph_files.setdefault(project_path, set()).update(pending)
        log.warning("graph lane %s: %s", project_path, exc)


def on_change(project_path: str, files: list) -> None:
    """Watcher callback: incremental reindex; then the graph pass (debounced) if not recent."""
    import os
    import time

    if is_paused():
        return
    if not os.path.isdir(project_path):
        # The root is gone, so this batch *is* the deletion. Indexing it anyway opens a store, and
        # opening one creates it — the daemon answers "this project was deleted" by writing a fresh
        # empty store for it, which nothing can ever reach again: the registry row went with the
        # tree, and the dir name is a hash of a path that no longer exists. Measured 2026-08-05 —
        # the live suite's workspace rmtree left two such stores per run, written 0-3s after the
        # suite's own teardown had already swept, and every earlier fix aimed at the sweep rather
        # than at the write. A missed event costs nothing here: a root that comes back is a change
        # of its own.
        return
    now = time.monotonic()
    if now - _last_index_fail.get(project_path, 0.0) < _INDEX_BACKOFF_S:
        return  # in backoff window after a previous failure; skip this event
    # Invalidate the fingerprint memo so the next reconcile pass re-walks this project.
    _code_fingerprint_cache.pop(project_path, None)
    # The index step is deliberately ABOVE the code-only gate below, and unconditional: it is the
    # only thing that re-embeds a docs/prose edit (HR28 — docs are ordinary source), and the gate
    # is code-only by construction, so a docs write never reaches it. Hoisting the gate above this
    # call would silently stop docs from ever being re-indexed. Guarded by GG4.
    try:
        if files:
            _index_files(project_path, files)
        else:
            _index_project(project_path)
    except Exception as exc:
        log.warning("incremental reindex %s: %s", project_path, exc)
        _last_index_fail[project_path] = now  # back off before retrying
        return
    # HR38: code-only sig — non-code churn (docs/config/images) never wakes the graph lane,
    # only real source drift does.
    sig = _code_source_fingerprint(project_path)
    if sig == _last_labelled_sig.get(project_path):
        return  # source unchanged — graph re-derive and labelling not needed
    if files:
        # Guarded now that the lane reads this map too; the dispatch workers are several
        # threads, not the single watcher thread this was written for.
        with _graph_lane_cv:
            _pending_graph_files.setdefault(project_path, set()).update(str(f) for f in files)
    if now - _last_lane_run.get(project_path, 0.0) < _LANE_DEBOUNCE_S:
        return  # carried in _pending_graph_files above, so this event's files are not lost
    _last_lane_run[project_path] = now
    # Hand off and return. Blocking here on _HEAVY_LOCK would consume a dispatch worker for the
    # length of another project's graph pass, which is the starvation this exists to end.
    _graph_lane_submit(project_path, sig)


def burst_label_federation(root_path: str) -> dict:
    """Label root + all discovered federation members. Return aggregate totals."""
    from rag_search.core.config import project_graph_db
    from rag_search.daemon.federation import discover_members
    from rag_search.graph.store import GraphStore

    paths = [root_path, *discover_members(root_path)]
    results: list[dict] = []
    for path in paths:
        gdb = project_graph_db(path)
        if not gdb.exists():
            log.info("burst_label_federation: skip %s (no graph DB)", path)
            continue
        _label_project(path)
        gs = GraphStore(gdb)
        try:
            total = gs._con.execute("SELECT COUNT(*) FROM communities WHERE level>=1").fetchone()[0]
            pending = gs._con.execute(
                "SELECT COUNT(*) FROM communities WHERE (summary IS NULL OR summary = '') AND level = 1"
            ).fetchone()[0]
        finally:
            gs.close()
        results.append({"path": path, "total": total, "pending": pending})
        log.info("burst_label_federation %s: total=%d pending=%d", path, total, pending)

    total_communities = sum(r["total"] for r in results)
    total_pending = sum(r["pending"] for r in results)
    log.info("burst_label_federation %s: Σ=%d pending=%d", root_path, total_communities, total_pending)
    return {"root": root_path, "members": results,
            "total_communities": total_communities, "total_pending": total_pending}
