"""Retrieval quality, measured mechanically, one arm per subprocess.

The query set is derived rather than written: one leading docstring or comment
block per file is the query, that file is the positive. This is the CodeSearchNet
protocol, and it is deterministic -- which the previous engine's "40-query golden
set" was not, having been quoted to four decimals in a config file and then never
committed, so none of its numbers can be checked today.

`--lane dense` is the default on purpose. The docstring sits inside the chunk it
identifies, so BM25 matches it verbatim and the full pipeline saturates at 1.000
on every metric; a fused-and-reranked score launders the reranker's work into the
embedder's column and every arm ties.

Each arm runs in its own subprocess with its own STATE_DIR, because every knob
this compares is a module-level constant read from the environment at import.
That is also the safe shape: an arm that builds a store in-process is one
mistake away from building it in the real one.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from coderag import config, discover, filters, projcfg

MIN_WORDS = 8
MAX_WORDS = 40
HEAD_LINES = 40
K = 10

_PY_DOC = re.compile(r'^\s*(?:r|u|b)?("""|\'\'\')(.*?)\1', re.DOTALL)
_LINE_COMMENT = re.compile(r"^\s*(?://+|#+|--)\s?(.*)$")
_BLOCK_COMMENT = re.compile(r"/\*+(.*?)\*/", re.DOTALL)
_NOISE = re.compile(r"[*/#\-=_]{2,}|<[^>]+>|https?://\S+")


@dataclass(frozen=True, slots=True)
class Query:
    text: str
    positive: str
    lang: str


def _clean(raw: str) -> str:
    text = _NOISE.sub(" ", raw)
    return " ".join(text.split())


def _docstring(head: str) -> str:
    if match := _PY_DOC.search(head):
        return _clean(match.group(2))
    if match := _BLOCK_COMMENT.search(head):
        return _clean(match.group(1))
    lines = []
    for line in head.splitlines():
        if match := _LINE_COMMENT.match(line):
            lines.append(match.group(1))
        elif lines or line.strip():
            break
    return _clean(" ".join(lines))


def build_queries(project: Path, limit: int = 0) -> list[Query]:
    """One query per file, sorted by path so two runs get the same set."""
    cfg = projcfg.effective(project, ())
    queries: list[Query] = []
    for rel in sorted(discover.candidates(project, cfg)):
        meta = discover.read(project, rel)
        if meta is None or meta.lang in filters.DOC_LANGS:
            # A markdown H1 matches the line-comment pattern and would supply
            # most of the query set on a doc-heavy repo, measuring prose recall
            # under a name that says code.
            continue
        head = "\n".join(meta.text.splitlines()[:HEAD_LINES])
        text = _docstring(head)
        words = text.split()
        if len(words) < MIN_WORDS:
            continue
        # Truncated rather than rejected: a long docstring is the common shape,
        # and rejecting it selects for files whose author wrote one line.
        queries.append(Query(" ".join(words[:MAX_WORDS]), meta.rel, meta.lang))
    return _stratify(queries, limit) if limit else queries


def _stratify(queries: list[Query], limit: int) -> list[Query]:
    """Round-robin across languages rather than sampling.

    A random subset of a corpus that is 70% one language measures that language;
    the deltas this harness exists to read are widest on the multi-language repo.
    """
    by_lang: dict[str, list[Query]] = {}
    for query in queries:
        by_lang.setdefault(query.lang, []).append(query)
    picked: list[Query] = []
    while len(picked) < limit and any(by_lang.values()):
        for bucket in sorted(by_lang):
            if by_lang[bucket] and len(picked) < limit:
                picked.append(by_lang[bucket].pop(0))
    return picked


ARMS = {
    "nomic": {"EMBED_MODEL": "nomic-ai/nomic-embed-text-v1.5"},
    "nomic-noprefix": {
        "EMBED_MODEL": "nomic-ai/nomic-embed-text-v1.5",
        "DOCUMENT_PREFIX": " ",
        "QUERY_PREFIX": " ",
    },
    "gte-modernbert": {"EMBED_MODEL": "Alibaba-NLP/gte-modernbert-base"},
    "bge-base": {"EMBED_MODEL": "BAAI/bge-base-en-v1.5"},
    "overlap-300": {"CHUNK_OVERLAP": "300"},
    "no-header": {"CHUNK_HEADER": "0"},
}


def run_arm(name: str, project: Path, scratch: Path, lane: str, limit: int) -> dict:
    """One arm, one subprocess, one throwaway STATE_DIR.

    An arm that fails to load is a result, not a bug to fix -- `gte-base` was
    ruled out by exactly this, ragged tokenizer output on the first batch.
    """
    env = os.environ | {f"CODERAG_{k}": v for k, v in ARMS[name].items()}
    env["CODERAG_STATE_DIR"] = str(scratch / name)
    out = subprocess.run(
        [sys.executable, __file__, "--worker", str(project), "--lane", lane, "--limit", str(limit)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if out.returncode != 0:
        return {"arm": name, "error": out.stderr.strip().splitlines()[-1:] or ["no output"]}
    return {"arm": name} | json.loads(out.stdout)


LANES = {"dense": "semantic", "lexical": "lexical", "hybrid": "hybrid"}


def _worker(project: Path, lane: str, limit: int) -> int:
    from coderag import index, registry, search

    lane = LANES[lane]
    registry.claim(project, direct=True)
    index.index_project(project)
    queries = build_queries(project, limit)
    ranks: list[int | None] = []
    for query in queries:
        hits = search.search(query.text, project, k=K, mode=lane, rerank=False, max_per_file=K)[
            "results"
        ]
        paths = [hit["rel_path"] for hit in hits]
        ranks.append(paths.index(query.positive) + 1 if query.positive in paths else None)
    print(json.dumps(score(ranks) | {"lane": lane, "model": config.EMBED_MODEL}))
    return 0


def score(ranks: list[int | None]) -> dict:
    """recall@10 and MRR@10 over one arm's per-query ranks (1-based, None = miss)."""
    found = [r for r in ranks if r is not None]
    return {
        "queries": len(ranks),
        "recall@1": round(sum(1 for r in found if r == 1) / max(len(ranks), 1), 4),
        f"recall@{K}": round(len(found) / max(len(ranks), 1), 4),
        "mrr": round(sum(1 / r for r in found) / max(len(ranks), 1), 4),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", type=Path)
    parser.add_argument("--lane", default="dense", choices=("dense", "lexical", "hybrid"))
    parser.add_argument("--arms", default="", help="comma-separated; default all")
    parser.add_argument("--limit", type=int, default=300)
    parser.add_argument("--scratch", type=Path, default=Path("/tmp/coderag-eval"))
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.worker:
        return _worker(args.project.resolve(), args.lane, args.limit)

    names = [a for a in (args.arms.split(",") if args.arms else ARMS) if a]
    rows = [run_arm(n, args.project.resolve(), args.scratch, args.lane, args.limit) for n in names]
    print(json.dumps(rows, indent=2))
    # Read the deltas, never the levels: the same model scored 0.61 and 0.19 on
    # two stores of the same corpus, purely from distractor count.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
