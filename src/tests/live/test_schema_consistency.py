"""Phase 2.0 — schema-consistency static guards (runs at collection time, no GPU needed).

SC4  No unscoped FROM-communities read: known leaks are patched; allowlist enforced
SC6  Producer↔consumer symmetry, at two levels: no write-only symbols *column* (Part A) and no
     Symbol dataclass *field* without a column (Part C), beyond _KNOWN_DEAD. Part C was added
     2026-07-31 after `Symbol.signature`/`.docstring` survived Part A for months by never
     becoming columns at all — the guard could not see what it did not start from.
SC8  community detection is leidenalg-free and deterministic
SC9  every RSE_* env knob in core/config.py has a consumer outside core/config.py
SC10 no served instruction names a JSON payload shape no server code emits
"""
from __future__ import annotations

import importlib
import inspect
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.live


# ---------------------------------------------------------------------------
# SC9 — every RSE_* env knob in core/config.py is actually consumed somewhere
# ---------------------------------------------------------------------------

def test_sc9_every_env_knob_has_a_consumer():
    """SC9: a constant read from RSE_* env must be read by something outside core/config.py.

    core/config.py is the retargeting contract HR34 promises a fresh clone: set the variable,
    change the behaviour. A knob that parses its env var and is then read by nothing breaks
    that promise silently — no error, no effect. Thirteen had accumulated by 2026-07-31
    (RSE_FINAL_TOP_K, RSE_MAX_BYTES, RSE_SCHEMA_VERSION, …), each outliving the call site it
    was added for. This fails the build the moment a fourteenth appears.
    """
    root = Path(__file__).parents[3]
    cfg = root / "src" / "rag_search" / "core" / "config.py"
    src = cfg.read_text()

    knobs = {
        m.group(1)
        for m in re.finditer(r"^([A-Z][A-Z0-9_]{2,})\s*(?::[^=]+)?=.*os\.environ\.get\(",
                             src, re.MULTILINE)
    }
    assert knobs, "no env-backed constants found — the regex above stopped matching config.py"

    searched = [
        p for p in (*root.glob("src/rag_search/**/*.py"), *root.glob("scripts/**/*.py"))
        if p != cfg
    ]
    corpus = "\n".join(p.read_text(errors="replace") for p in searched)

    orphans = sorted(k for k in knobs if not re.search(rf"\b{re.escape(k)}\b", corpus))
    assert not orphans, (
        "env knob(s) in core/config.py with no consumer outside that file: "
        f"{orphans}. Either wire each to the code that reads it, or delete it — a knob that "
        "parses and does nothing is a broken public interface, not dead code."
    )


# ---------------------------------------------------------------------------
# SC4 — known unscoped reads are all patched (spot-check the fixed sites)
# ---------------------------------------------------------------------------

_FIXED_SITES: list[tuple[str, str, str]] = [
    # (module dotted path, function/context description, expected substring in source)
    # The two `suggested_questions` readers were the original leak sites and both are gone —
    # `routes_search` (whole module) and `_overview`'s variant. `_overview` stays on this list
    # because its `communities` branch is now the only community read left on a served surface,
    # and it is the one that must keep the scope.
    ("rag_search.server._overview", "communities", "level>=1"),
]


def test_sc4_fixed_leak_sites_carry_level_scope():
    """SC4: previously unscoped community reads now carry level>=1."""
    for mod_path, fn_hint, expected in _FIXED_SITES:
        mod = importlib.import_module(mod_path)
        src = inspect.getsource(mod)
        # Find the relevant snippet containing fn_hint + the SELECT
        # If expected is absent from the entire module source, the fix regressed.
        assert expected in src, (
            f"{mod_path} ({fn_hint}): expected {expected!r} in source — "
            "the level>=1 scope guard was removed, re-introducing the structural-spine leak."
        )


# Phase-2a cleaned all dead items (F-B/D/G/H/I); new dead items fail CI.
_KNOWN_DEAD: frozenset[str] = frozenset()


def test_sc6_no_dead_data_beyond_allowlist():
    """SC6: no write-only symbols column outside _KNOWN_DEAD — write-amplification tripwire.

    Part B applied the same tripwire to process.db, reading every `CREATE TABLE IF NOT
    EXISTS` out of kb/bpre.py and requiring a payload SELECT for each somewhere in the
    package. There is no process.db and no second store any more; graph.db's symbols table
    is the only producer↔consumer pair left, and that is Part A.
    """
    from rag_search.graph.store import GraphStore

    _r = Path(__file__).parents[2] / "rag_search"
    ss = (_r / "graph/store.py").read_text()

    # symbols columns — parse INSERT col list; read cols from list_symbols source
    # (regex-only across multi-line SQL string literals is fragile; inspect is reliable)
    im = re.search(r"INSERT\s+INTO\s+symbols\s*\(([^)]+)\)", ss, re.IGNORECASE)
    assert im, "SC6: upsert_symbol INSERT INTO symbols not found"
    written = {c.strip() for c in im.group(1).split(",")}
    ls_src = inspect.getsource(GraphStore.list_symbols)
    read_sym = set(re.findall(r'"(\w+)"', ls_src))  # column names in SELECT + keys tuple
    for col in written:
        if f"symbols.{col}" not in _KNOWN_DEAD:
            assert col in read_sym, (
                f"SC6: symbols.{col} written by upsert_symbol but absent from list_symbols — "
                f"add a consumer or add 'symbols.{col}' to _KNOWN_DEAD"
            )

    # Part C, added 2026-07-31. The column check above could not see `Symbol.signature` and
    # `.docstring`, which were populated on every symbol the extractor emitted and read by
    # nothing: they never became columns, so they were invisible to a guard that starts from the
    # INSERT list. Same defect, one level up — a field written and never read is a claim the
    # schema does not honour, and it reads as available data to the next person extending the
    # extractor. The dataclass is the producer, so it is the right place to start from.
    from rag_search.graph.extractor import Symbol
    for field in Symbol.__dataclass_fields__:
        if f"Symbol.{field}" in _KNOWN_DEAD:
            continue
        assert field in written, (
            f"SC6: Symbol.{field} is populated by the extractor but is not among the columns "
            f"upsert_symbol writes ({sorted(written)}) — give it a column and a consumer, or "
            f"add 'Symbol.{field}' to _KNOWN_DEAD"
        )


_R2_DROPPED_COLS = ("semantic_type", "narrated", "kind", "path")

_PRE_R2_SCHEMA = """
    CREATE TABLE communities (
        id INTEGER PRIMARY KEY,
        level INTEGER NOT NULL DEFAULT 1,
        title TEXT,
        summary TEXT,
        member_count INTEGER DEFAULT 0,
        semantic_type TEXT,
        narrated INTEGER DEFAULT 0,
        kind TEXT DEFAULT 'community',
        path TEXT
    );
    INSERT INTO communities (id,level,title,summary,member_count,semantic_type,narrated,kind)
    VALUES (7,1,'Auth','3 symbol(s) (function) from auth.py.',3,'security',1,'community');
"""


def test_sc9_r2_purge_migrates_a_pre_purge_db(safe_tmp_path):
    """SC9: opening a pre-R2 graph.db drops the four dead community columns and keeps the rest.

    Every graph in the fleet was created with these columns, so the purge is a migration, not a
    schema edit — and nothing else exercises that path. The keep half is not decoration: a
    migration that dropped `summary` or `member_count` would satisfy "the dead columns are gone"
    just as well, and both halves read the same reopened DB.
    """
    import sqlite3

    from rag_search.graph.store import GraphStore

    db = safe_tmp_path / "pre_r2.db"
    con = sqlite3.connect(str(db))
    con.executescript(_PRE_R2_SCHEMA)
    con.commit()
    con.close()

    gs = GraphStore(db)
    try:
        cols = {r[1] for r in gs._con.execute("PRAGMA table_info(communities)")}
        row = gs._con.execute(
            "SELECT title, summary, member_count FROM communities WHERE id=7"
        ).fetchone()
    finally:
        gs.close()

    assert not cols.intersection(_R2_DROPPED_COLS), (
        f"SC9: pre-R2 columns survived: {sorted(cols.intersection(_R2_DROPPED_COLS))}"
    )
    assert {"id", "level", "title", "summary", "member_count"} <= cols, (
        f"SC9: the migration took a live column with it — surviving columns are {sorted(cols)}"
    )
    assert row is not None and row[0] == "Auth" and row[2] == 3, (
        f"SC9: DROP COLUMN lost the row's live data — read back {row!r}"
    )


def test_sc8_no_leidenalg_in_community():
    """SC8a: community.py must not import leidenalg (k-core replaced Leiden)."""
    import rag_search.graph.community as mod
    src = inspect.getsource(mod)
    assert "leidenalg" not in src, (
        "SC8: graph.community still imports leidenalg — Phase 2.0c k-core swap not complete"
    )


def test_sc8_detect_communities_deterministic(safe_tmp_path):
    """SC8b: detect_communities is byte-identical on two runs with the same graph."""
    from rag_search.graph.community import detect_communities
    from rag_search.graph.extractor import extract_symbols, symbol_id
    from rag_search.graph.store import GraphStore

    fpath = safe_tmp_path / "svc.py"
    fpath.write_text(
        "def alpha(): pass\ndef beta(): return alpha()\n"
        "def gamma(): return beta()\ndef delta(): return gamma()\n"
        "class Engine:\n    def run(self): return delta()\n"
    )
    content = fpath.read_text()

    def _build(db_path) -> dict[str, int]:
        gs = GraphStore(db_path)
        for s in extract_symbols(fpath, content, "python"):
            gs.upsert_symbol(symbol_id(str(fpath), s.name, s.start_line),
                             s.name, s.qualified_name, s.kind,
                             str(fpath), s.start_line, s.end_line, s.language)
        gs.commit()
        m = detect_communities(gs)
        gs.close()
        return m

    m1 = _build(safe_tmp_path / "g1.db")
    m2 = _build(safe_tmp_path / "g2.db")
    assert m1 == m2, (
        f"SC8b: detect_communities non-deterministic — "
        f"diff: {set(m1.items()) ^ set(m2.items())}"
    )


# ---------------------------------------------------------------------------
# SC10 — the served doctrine may not describe a payload no server code emits
# ---------------------------------------------------------------------------

_JSON_LITERAL = re.compile(r'\{[^{}]*"[^"]+"\s*:[^{}]*\}')
_JSON_KEY = re.compile(r'"([A-Za-z_][A-Za-z0-9_]*)"\s*:')


_WITHDRAWN_CLAUSE = (
    'RESILIENCE: if an MCP call returns {"status":"timeout","fallback":true} or hangs,'
)


def _claimed_keys(text: str) -> set[str]:
    return {k for lit in _JSON_LITERAL.findall(text) for k in _JSON_KEY.findall(lit)}


def _emitter_corpus(root: Path, prompt_files: tuple[Path, ...]) -> str:
    return "\n".join(
        p.read_text(errors="replace")
        for p in root.glob("src/rag_search/**/*.py") if p not in prompt_files
    )


def _prompt_files(root: Path) -> tuple[Path, ...]:
    return (root / "src" / "rag_search" / "daemon" / "global_prompt.py",
            root / "scripts" / "integrations" / "canonical.py")


def test_sc10_no_prompt_names_a_payload_no_code_emits():
    """SC10a: every JSON key the served instructions tell a caller to recognise is emitted.

    The instructions are a contract read by an agent that cannot check it. Until 2026-08-17 the
    RESILIENCE rule told callers to watch for `{"status":"timeout","fallback":true}` — a shape no
    code has ever produced, so the branch it armed could not fire and the fallback it promised
    was unreachable. That is worse than an absent rule: it reads as coverage.

    Keys rather than the literal string, because a shape can be assembled from a dict the source
    never writes on one line.
    """
    root = Path(__file__).parents[3]
    pf = _prompt_files(root)
    corpus = _emitter_corpus(root, pf)
    claimed = {k for f in pf for k in _claimed_keys(f.read_text())}

    missing = sorted(k for k in claimed if f'"{k}"' not in corpus)
    assert not missing, (
        f"the served instructions name JSON key(s) no server code emits: {missing}. "
        "Delete the clause or implement the payload — a rule keyed on a shape that cannot "
        "appear arms a branch that can never fire."
    )


def test_sc10_the_withdrawn_clause_is_still_caught():
    """SC10b: SC10a is vacuous on a prompt carrying no JSON literal, which is the state it
    landed in — so the predicate is re-run against the clause it was written to catch.

    Without this, deleting the clause and deleting the guard look identical from CI.
    """
    root = Path(__file__).parents[3]
    corpus = _emitter_corpus(root, _prompt_files(root))
    claimed = _claimed_keys(_WITHDRAWN_CLAUSE)
    assert claimed, "SC10b: the literal parser stopped matching the clause it was built from"
    assert sorted(k for k in claimed if f'"{k}"' not in corpus) == ["fallback"], (
        "SC10b: `fallback` now appears in src/ — either the timeout payload was implemented, in "
        "which case restore the clause, or an unrelated key took the name and SC10b needs a new "
        "discriminator. Until one is chosen SC10a is running blind."
    )
