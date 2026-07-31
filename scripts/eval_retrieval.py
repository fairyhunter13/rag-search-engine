#!/usr/bin/env python3
"""Reproducible retrieval-quality A/B between embedding models.

    python scripts/eval_retrieval.py --project /repo
    python scripts/eval_retrieval.py --project /repo --model X --build --out /tmp/rse-eval/x

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
import json
import math
import sqlite3
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


def _metrics(paths: list[str], gold: str) -> tuple[float, float, float, float]:
    """(recall@1, reciprocal rank, nDCG@10, recall@10) for one query with one relevant document."""
    hit1 = 1.0 if paths[:1] == [gold] else 0.0
    for i, p in enumerate(paths[:_TOP_K], start=1):
        if p == gold:
            # Ideal DCG is 1.0 with a single relevant doc, so DCG is already normalised.
            return hit1, 1.0 / i, 1.0 / math.log2(i + 1), 1.0
    return hit1, 0.0, 0.0, 0.0


def _embedder(model: str | None):
    from rag_search.embed.embedder import Embedder
    e = Embedder(model=model) if model else Embedder()
    e.warmup()
    return e


def evaluate(project: Path, store_dir: Path, model: str | None, n: int, lane: str) -> dict:
    from rag_search.index.store import VectorStore
    from rag_search.query.search import search

    from rag_search.core.config import project_graph_db

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
    queries = build_query_set(graph_db, store_paths, n)
    if not queries:
        raise SystemExit(f"no query-shaped symbols in {graph_db} whose file is indexed in {db}")

    embedder, store = _embedder(model), VectorStore(db, migrate=False)
    agg = [0.0, 0.0, 0.0, 0.0]
    for q, gold in queries:
        if lane == "dense":
            hits = store.search(embedder.embed([q], batch_size=1)[0].astype("float32"), top_k=_TOP_K)
        else:
            hits = search(q, embedder, store, top_k=_TOP_K)
        for i, v in enumerate(_metrics([h["path"] for h in hits], gold)):
            agg[i] += v
    k = len(queries)
    spread: dict[str, int] = {}
    for _q, gold in queries:
        lang = path_lang.get(gold, "?")
        spread[lang] = spread.get(lang, 0) + 1
    return {
        "project": str(project), "store": str(db), "model": model or "<configured>",
        "lane": lane, "queries": k,
        # Reported, not just relied on: a set that has quietly collapsed to one language is the
        # exact failure this selector replaced, and it would otherwise look like a normal result.
        "query_languages": dict(sorted(spread.items(), key=lambda kv: -kv[1])),
        "recall@1": round(agg[0] / k, 4), "MRR": round(agg[1] / k, 4),
        "nDCG@10": round(agg[2] / k, 4), "recall@10": round(agg[3] / k, 4),
    }


def build(project: Path, out: Path, model: str | None) -> None:
    """Index `project` into a scratch store. Never touches the fleet's own index."""
    from rag_search.index.indexer import index_project
    from rag_search.index.store import VectorStore

    out.mkdir(parents=True, exist_ok=True)
    files, chunks = index_project(
        str(project), _embedder(model), VectorStore(out / "vectors.db"), federation_mode=False)
    print(f"indexed {files} files / {chunks} chunks into {out/'vectors.db'}", file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", required=True, type=Path)
    ap.add_argument("--model", default=None, help="embedding model id (default: the configured one)")
    ap.add_argument("--build", action="store_true", help="index into --out first, using --model")
    ap.add_argument("--out", type=Path, default=None, help="scratch store (default: the fleet's)")
    ap.add_argument("--queries", type=int, default=_DEFAULT_QUERIES)
    ap.add_argument("--lane", choices=("dense", "hybrid"), default="dense",
                    help="dense isolates the embedder; hybrid measures the whole pipeline")
    args = ap.parse_args()

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
    print(json.dumps(
        evaluate(project, store_dir, args.model, args.queries, args.lane), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
