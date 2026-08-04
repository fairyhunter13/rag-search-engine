#!/usr/bin/env python3
"""Reproducible retrieval-quality A/B between embedding models.

    python scripts/eval_retrieval.py --project /repo
    python scripts/eval_retrieval.py --project /repo --model X --build --out /tmp/rse-eval/x
    python scripts/eval_retrieval.py --compare a.json b.json               # paired, McNemar

**Compare two arms with `--compare`, never by eyeballing their means.** Arms run the same queries
against the same corpus, so the results are paired; two means thrown at each other discard that and
need a far larger margin to resolve. Each run now carries per-query ranks in `records` for exactly
this. `nDCG@10` is no longer reported — with one gold document and binary relevance it was a
deterministic transform of MRR at the same rank, never a second signal (see `_rank_of`).

Exists because `core/config.py` cites a "40-query golden set" as the evidence for `EMBED_MODEL`,
`RERANK_MODEL` and `EMBED_MAX_TOKENS` — the last a four-point sweep quoted to four decimals — and
**that query set is nowhere in the repo**. Those numbers can be quoted but not checked, and a
challenger cannot be compared against them at all. The failure `measure_preconditions.py` was landed
to end, one level up: a measurement whose *inputs* did not outlive the session that took them.

Ground truth is derived, never hand-labelled, which is what makes it survive: queries are tree-sitter
symbols — qualified name plus the definition's own first line — and positives the file defining them,
selection is deterministic so two models are always asked the same questions, and nothing
project-specific is committed — the set is regenerated from whatever `--project` names, so this file
carries no real path and P18/HR34 holds.

Symbols, not Python docstrings, since 2026-07-31. The docstring selector read
`WHERE language = 'python'` and matched `\"\"\"…\"\"\"`, so it could only ever ask about one language —
and a single-language verdict cannot license an embedder switch across a fleet whose mass is
javascript, php and html. It was also this script's only regex, against P6/HR15. **Levels moved
when it changed**: compare arms measured by the same selector, never across this boundary.

**The default lane is `dense`, and that is not a detail.** The docstring is still inside the chunk
it identifies, so BM25 matches it verbatim: measured on a 1,278-chunk store, the lexical lane alone
scores recall@1 0.925 and the full fused-and-reranked pipeline scores a flat 1.000/1.000/1.000/1.000
— no headroom, so every arm ties and the A/B answers nothing. The dense lane on the same set and the
same store reads 0.725 / 0.8102 / 0.8503 / 0.975. It is also the only lane an embedding model
controls, which makes it both the discriminating measurement and the honest one. Use
`--lane hybrid` to measure the pipeline (a reranker or fusion change), never to compare embedders.

Absolute levels are not comparable to the historical 0.875/1.000/0.933/0.934 figures, which came
from a hand-written set this cannot reconstruct. Read the delta between two arms, never the level.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# Too short is keyword soup, too long is prose about a module; neither is a query anyone types.
# Excluded at both ends rather than truncated — truncating changes what is being asked.
_MIN_QUERY_CHARS, _MAX_QUERY_CHARS = 40, 300
_DEFAULT_QUERIES, _TOP_K = 40, 10
# Kinds worth asking about. `data` is excluded: 28.1% of symbol rows fleet-wide (62,449 of 222,483,
# counted 2026-07-31) and they are config keys — "project.version" is not a question anyone puts to
# a code search. The 53.7% this comment carried at first was wrong; the exclusion does not turn on
# the size, so the decision is unchanged.
_QUERY_KINDS = ("function", "method", "class")


def _signature_at(file: str, start_line: int) -> str | None:
    """The definition's own first line, collapsed. Located by tree-sitter, so no pattern here.

    Reading the line rather than storing it: `symbols` has no signature column, and adding one
    would move `EXTRACTOR_REV` and re-derive 152 graphs to serve a script that runs by hand.
    """
    try:
        with open(file, errors="ignore") as fh:
            for i, line in enumerate(fh, start=1):
                if i == start_line:
                    return " ".join(line.split())
    except OSError:
        return None
    return None


def build_query_set(graph_db: Path, store_paths: set[str], limit: int) -> list[tuple[str, str]]:
    """(query, gold path) pairs from the graph store — deterministic, language-stratified.

    Drawn from tree-sitter symbols rather than Python docstrings, because the docstring selector
    could only ever ask about one language and a single-language verdict cannot license an
    embedder switch across a 152-store fleet where javascript, php and css carry most of the mass.

    A second property falls out of the source: this set is built from symbols and files, never
    from chunks, so it is **identical across chunking arms**. The `RSE_EMBED_MAX_TOKENS=512` arm
    re-chunks the store, which under the old selector silently changed the questions along with
    the answers; the confound is now confined to the index, where it belongs.
    """
    con = sqlite3.connect(f"file:{graph_db}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT qualified_name, file, start_line, language FROM symbols "
            f"WHERE kind IN ({','.join('?' * len(_QUERY_KINDS))}) ORDER BY sid", _QUERY_KINDS,
        ).fetchall()
    finally:
        con.close()
    by_lang: dict[str, list[tuple[str, str]]] = {}
    seen: set[str] = set()
    for qname, file, start, lang in rows:
        # One query per file (a fat module cannot dominate), and only files the store actually
        # holds — a gold path that was never indexed scores 0 for every arm and measures nothing.
        # Discovery is model-independent, so this filter is the same set in every arm.
        if file in seen or file not in store_paths:
            continue
        sig = _signature_at(file, start)
        if sig is None:
            continue
        q = f"{qname} {sig}"
        if not _MIN_QUERY_CHARS <= len(q) <= _MAX_QUERY_CHARS:
            continue
        seen.add(file)
        by_lang.setdefault(lang, []).append((q, file))
    out: list[tuple[str, str]] = []
    # Round-robin over languages in name order: with 1,542 python functions against a handful of
    # css rules, taking the first N by id would rebuild the single-language set this replaces.
    while len(out) < limit and any(by_lang.values()):
        for lang in sorted(by_lang):
            if by_lang[lang] and len(out) < limit:
                out.append(by_lang[lang].pop(0))
    return out


_MAX_COMMIT_FILES = 10
_MIN_SUBJECT_CHARS = 20


def build_commit_query_set(
    project: Path, store_paths: set[str], limit: int,
) -> list[tuple[str, tuple[str, ...]]]:
    """(commit message, files it changed) — a query population the corpus cannot leak the answer to.

    `build_query_set` writes every query as `qualified_name signature`, so the identifier is
    verbatim in both the query and the chunk it is supposed to find. That is not a hard retrieval
    task, and it is why arms that read a real effect under their own probes read null here. A
    commit message is written by a human describing intent, in the vocabulary of the problem rather
    than of the code, and the files the commit touched are the answer — the join is recorded rather
    than reconstructed, so nothing about it can be leaked by the embedder.

    Three filters, each removing commits whose file list is not a statement about relevance:
    merges (no content of their own), commits touching more than `_MAX_COMMIT_FILES` (bulk renames
    and reformats, where the message describes none of the files individually), and subjects too
    short to be a query. Files not in the store are dropped, and a commit that keeps none is
    skipped — a gold path that was never indexed scores 0 in every arm and measures nothing.

    **Not comparable to `build_query_set`'s numbers.** Different questions against the same corpus
    are two instruments; a commit-mode MRR beside a symbol-mode MRR is a category error, and the
    two must never be merged into one table. Neither of the papers that use this construction
    publishes a label-quality validation, so **hand-read ~20 pairs before trusting an arm**.
    """
    # Two distinct markers, not one: `%b` routinely contains blank lines, so splitting the message
    # from the file list on a blank line puts half a commit body into the gold set. `\x1e` starts a
    # commit, `\x1f` ends its message — neither can occur in a git log.
    rs, us = "\x1e", "\x1f"
    proc = subprocess.run(
        ["git", "-C", str(project), "log", "--no-merges", "--name-only",
         f"--format={rs}%s %b{us}", "-n", str(limit * 20)],
        capture_output=True, text=True, errors="replace", timeout=120, check=False)
    if proc.returncode != 0:
        raise SystemExit(f"git log failed in {project}: {proc.stderr.strip()[:200]}")

    out: list[tuple[str, tuple[str, ...]]] = []
    for block in proc.stdout.split(rs)[1:]:
        if len(out) >= limit:
            break
        msg, _, files = block.partition(us)
        msg = " ".join(msg.split())
        if not _MIN_SUBJECT_CHARS <= len(msg) <= _MAX_QUERY_CHARS:
            continue
        named = [ln for ln in files.splitlines() if ln.strip()]
        if not named or len(named) > _MAX_COMMIT_FILES:
            continue
        gold = tuple(dict.fromkeys(
            p for ln in named if (p := str(project / ln)) in store_paths))
        # A commit keeps its label only if the store holds most of what it touched. Found by
        # hand-reading the first ten pairs: a commit about gitignore-aware *discovery* survived
        # with one gold file, a test, because the module it actually changed was not indexed. The
        # message then describes a file that is not in the answer, which is a mislabel rather than
        # a hard query, and it is invisible in the aggregate. Half is a judgement, not a result.
        if gold and len(gold) * 2 >= len(named):
            out.append((msg, gold))
    return out


def _rank_of(paths: list[str], gold: tuple[str, ...]) -> int:
    """1-based rank of the best-placed member of `gold` in `paths`, or 0 if none is present.

    Symbol-mode queries carry exactly one gold path, so this is the single-gold behaviour it has
    always had. Commit mode carries several, and the *first* one retrieved is the right summary:
    the question a commit message asks is "where does this change live", and a searcher who lands
    on any of the touched files has been answered. Requiring all of them would score a correct
    top-1 as a failure whenever a commit happened to touch two files.

    Every metric this script reports is a function of this single number, which is why it is
    what the per-query records carry: recall@1 is `rank == 1`, RR is `1/rank` inside the cutoff,
    recall@k is `0 < rank <= k`. Keeping the rank rather than the derived floats is what makes a
    *paired* comparison possible at all — see `mcnemar`.

    **`nDCG@10` was reported here until 2026-08-01 and was never a second signal.** With one gold
    document and binary relevance it is `1/log2(rank+1)` against RR's `1/rank` at the same rank —
    a deterministic transform of a column already in the table, so per-query it carries no
    information RR does not. The old docstring conceded the arithmetic (*"Ideal DCG is 1.0 … so
    DCG is already normalised"*) while every table in the lineage still presented the two as
    independent columns. Dropped rather than recomputed; the tables already on the record keep
    theirs, and this is a retirement, not a restatement of them.
    """
    for i, p in enumerate(paths, start=1):
        if p in gold:
            return i
    return 0


def mcnemar(a: list[dict], b: list[dict]) -> dict:
    """McNemar's exact test on recall@1 over the queries two arms share.

    Two arms run **the same queries against the same corpus**, so their scores are paired and
    comparing two independent means throws away the pairing — the textbook case for McNemar on
    the discordant pairs, which is materially more powerful. The harness used to accumulate
    straight into a 4-element total and divide, discarding exactly the records this needs.

    Exact two-sided binomial rather than the chi-square approximation: the discordant count is
    routinely under 25 here, which is where the approximation is least trustworthy, and
    `math.comb` costs nothing. There is no scipy in this venv and a hand-run script is not a
    reason to add one.
    """
    ra = {r["qid"]: r["rank"] for r in a}
    rb = {r["qid"]: r["rank"] for r in b}
    shared = sorted(set(ra) & set(rb))
    only_a = sum(1 for q in shared if ra[q] == 1 and rb[q] != 1)
    only_b = sum(1 for q in shared if ra[q] != 1 and rb[q] == 1)
    n = only_a + only_b
    lo = min(only_a, only_b)
    p = 1.0 if n == 0 else min(1.0, 2.0 * sum(math.comb(n, k) for k in range(lo + 1)) / 2**n)
    return {
        "shared_queries": len(shared), "a_only_hit1": only_a, "b_only_hit1": only_b,
        "discordant": n, "p_value_exact": round(p, 6),
        # Stated rather than left to the reader: a and b are the file order on the command line.
        "reading": ("no discordant pairs" if n == 0 else
                    f"{'A' if only_a > only_b else 'B'} wins {max(only_a, only_b)}-{lo}"),
    }


def compare(path_a: Path, path_b: Path) -> dict:
    """Paired comparison of two result files written by `evaluate`."""
    a, b = (json.loads(p.read_text()) for p in (path_a, path_b))
    for name, arm in (("A", a), ("B", b)):
        if not arm.get("records"):
            raise SystemExit(f"{name} carries no per-query records — re-run that arm")
    if a["lane"] != b["lane"]:
        raise SystemExit(f"lanes differ ({a['lane']} vs {b['lane']}): not the same measurement")
    return {
        "A": {"model": a["model"], "recall@1": a["recall@1"], "queries": a["queries"]},
        "B": {"model": b["model"], "recall@1": b["recall@1"], "queries": b["queries"]},
        "mcnemar_recall@1": mcnemar(a["records"], b["records"]),
    }


def _embedder(model: str | None):
    """The production embedder, unwrapped.

    A `_Prefixed` shim stood here while the task prefixes were a proposal: the harness had to
    supply `search_document: ` / `search_query: ` because production did not, and putting them in
    production before the switch was decided would have silently rewritten every vector in the
    fleet under an unchanged `EMBED_MODEL`. The switch is decided — `Embedder.embed` is
    side-aware and `EMBED_PREFIX_REV` records it — so the shim is now a way to prefix *twice*.
    Deleted rather than guarded: the harness measures what production does, and a knob that can
    only make the measurement wrong is not a knob.
    """
    from rag_search.embed.embedder import Embedder
    e = Embedder(model=model) if model else Embedder()
    e.warmup()
    return e


def evaluate(project: Path, store_dir: Path, model: str | None, n: int, lane: str,
             queries_from: str = "symbol") -> dict:
    from rag_search.core.config import project_graph_db
    from rag_search.index.store import VectorStore
    from rag_search.query.search import search

    db = store_dir / "vectors.db"
    if not db.exists():
        raise SystemExit(f"no vector store at {db}")
    graph_db = Path(project_graph_db(str(project)))
    if not graph_db.exists():
        raise SystemExit(f"no graph store at {graph_db} — index {project} before evaluating it")
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        path_lang = dict(con.execute("SELECT DISTINCT path, language FROM chunks"))
        store_paths = set(path_lang)
    finally:
        con.close()
    if queries_from == "commit":
        queries = build_commit_query_set(project, store_paths, n)
        if not queries:
            raise SystemExit(f"no commit in {project} survived the filters with an indexed file")
    else:
        queries = [(q, (gold,)) for q, gold in build_query_set(graph_db, store_paths, n)]
        if not queries:
            raise SystemExit(f"no query-shaped symbols in {graph_db} whose file is indexed in {db}")

    # The pool depth the cross-encoder is actually given, imported rather than hard-coded so this
    # cannot drift from production. `recall@` it is the reranker's ceiling: a gold chunk outside
    # the pool is one no reranker can ever recover, which is the question `_MIN_POOL` answers and
    # the aggregate metrics never could.
    from rag_search.query.search import _MIN_POOL

    embedder, store = _embedder(model), VectorStore(db, migrate=False)
    depth = max(_TOP_K, _MIN_POOL) if lane == "dense" else _TOP_K
    records: list[dict] = []
    for q, gold in queries:
        if lane == "dense":
            q_vec = embedder.embed([q], batch_size=1, side="query")[0].astype("float32")
            hits = store.search(q_vec, top_k=depth)
        else:
            hits = search(q, embedder, store, top_k=_TOP_K)
        records.append({
            # A short digest of the query, never the gold path: the records only have to *align*
            # two arms, and the set is deterministic so the same query hashes the same in both.
            "qid": hashlib.sha1(q.encode()).hexdigest()[:12],
            "rank": _rank_of([h["path"] for h in hits], gold),
        })
    k = len(queries)
    ranks = [r["rank"] for r in records]
    # RR keeps the @10 cutoff it always had, even though the dense lane now retrieves deeper —
    # changing it would silently rebase every MRR already on the record.
    mrr = sum(1.0 / r for r in ranks if 0 < r <= _TOP_K) / k
    spread: dict[str, int] = {}
    for _q, gold in queries:
        # A commit-mode query has several gold files; the first names the language of the
        # change well enough for a spread that exists to catch collapse to one language.
        lang = path_lang.get(gold[0], "?")
        spread[lang] = spread.get(lang, 0) + 1
    return {
        "project": str(project), "store": str(db), "model": model or "<configured>",
        "lane": lane, "queries_from": queries_from, "queries": k,
        # Reported, not just relied on: a set that has quietly collapsed to one language is the
        # exact failure this selector replaced, and it would otherwise look like a normal result.
        "query_languages": dict(sorted(spread.items(), key=lambda kv: -kv[1])),
        "recall@1": round(sum(r == 1 for r in ranks) / k, 4), "MRR": round(mrr, 4),
        "recall@10": round(sum(0 < r <= _TOP_K for r in ranks) / k, 4),
        # Only the dense lane retrieves deep enough to answer this: `search()` returns its
        # results already reranked, so a hybrid figure would mean duplicating production fusion
        # here to reconstruct a pool this script never sees.
        f"recall@{_MIN_POOL}": (round(sum(0 < r <= _MIN_POOL for r in ranks) / k, 4)
                                if lane == "dense" else None),
        # Per-query ranks, so two arms can be compared with `--compare` as the paired
        # measurements they are rather than as two independent means.
        "records": records,
    }


def build(project: Path, out: Path, model: str | None) -> None:
    """Index `project` into a scratch store. Never touches the fleet's own index."""
    from rag_search.index.indexer import index_project
    from rag_search.index.store import VectorStore

    out.mkdir(parents=True, exist_ok=True)
    files, chunks = index_project(
        str(project), _embedder(model), VectorStore(out / "vectors.db"),
        federation_mode=False)
    print(f"indexed {files} files / {chunks} chunks into {out/'vectors.db'}", file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", type=Path, help="required unless --compare is given")
    ap.add_argument("--compare", nargs=2, type=Path, metavar=("A", "B"),
                    help="two result files: McNemar exact test on recall@1 over shared queries")
    ap.add_argument("--model", default=None, help="embedding model id (default: the configured one)")
    ap.add_argument("--build", action="store_true", help="index into --out first, using --model")
    ap.add_argument("--out", type=Path, default=None, help="scratch store (default: the fleet's)")
    ap.add_argument("--queries", type=int, default=_DEFAULT_QUERIES)
    ap.add_argument("--lane", choices=("dense", "hybrid"), default="dense",
                    help="dense isolates the embedder; hybrid measures the whole pipeline")
    ap.add_argument("--queries-from", choices=("symbol", "commit"), default="symbol",
                    help="symbol: qualified_name + signature (the identifier is verbatim in "
                         "the chunk); commit: message as query, files changed as gold. The "
                         "two are different instruments — never merge their tables")
    args = ap.parse_args()

    if args.compare:
        print(json.dumps(compare(*args.compare), indent=2))
        return 0
    if args.project is None:
        raise SystemExit("--project is required unless --compare is given")
    project = args.project.resolve()
    if args.out is None:
        from rag_search.core.config import index_dir
        store_dir = Path(index_dir(str(project)))
    else:
        store_dir = args.out
    if args.build:
        if args.out is None:
            raise SystemExit("--build requires --out: refusing to rebuild the fleet's own store")
        build(project, store_dir, args.model)
    print(json.dumps(evaluate(project, store_dir, args.model, args.queries, args.lane,
                                 args.queries_from), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
