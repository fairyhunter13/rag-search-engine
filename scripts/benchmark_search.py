#!/usr/bin/env python3
"""Search quality benchmark — MRR@10 and NDCG@10.

Runs a fixed gold-set of 20 queries against a project and measures retrieval quality.
Uses path-substring matching to decide relevance (no human annotation needed).

Usage:
  python scripts/benchmark_search.py --project /path/to/project
  python scripts/benchmark_search.py --project /path/to/project --top-k 10 --json

Exit code 0 = MRR@10 ≥ 0.5, else 1.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


# Gold set: (query, relevant_path_substrings)
# A result is relevant if its path contains ANY of the substrings (case-insensitive).
_GOLD_SET: list[tuple[str, list[str]]] = [
    ("HTTP handler route endpoint",         ["handler", "route", "server", "api"]),
    ("authentication login verify token",   ["auth", "login", "jwt", "token", "middleware"]),
    ("database connection pool query",      ["db", "database", "storage", "repo", "sql"]),
    ("LLM enrichment community summary",    ["enrich", "llm", "client", "community"]),
    ("graph storage nodes edges",           ["graph", "storage", "node", "edge"]),
    ("file watcher debounce flush",         ["watcher", "watch", "debounce", "flush"]),
    ("embedding vector search rerank",      ["embed", "vector", "rerank", "search"]),
    ("config registry project path",        ["config", "registry", "project"]),
    ("MCP tool search ask overview",        ["mcp", "tool", "handler"]),
    ("Leiden community detection clustering", ["community", "leiden", "detection", "cluster"]),
    ("wiki page generator markdown",        ["wiki", "generator", "markdown"]),
    ("dashboard API endpoint route",        ["dashboard", "route", "api"]),
    ("federation member project index",     ["federation", "federat"]),
    ("incremental index rebuild change",    ["index", "incremental", "rebuild", "change"]),
    ("test fixture conftest setup",         ["test", "conftest", "fixture"]),
    ("tree-sitter parser extract symbol",   ["extractor", "extract", "parser", "tree"]),
    ("background job async pipeline",       ["job", "pipeline", "async", "task"]),
    ("SSE event stream live feed",          ["event", "sse", "stream", "dashboard"]),
    ("metrics history chart sparkline",     ["metric", "chart", "history", "monitor"]),
    ("vacuum storage cleanup disk",         ["vacuum", "storage", "cleanup", "disk"]),
]


def _ndcg(relevances: list[int], k: int) -> float:
    dcg = sum(rel / math.log2(i + 2) for i, rel in enumerate(relevances[:k]))
    ideal = sorted(relevances, reverse=True)[:k]
    idcg = sum(rel / math.log2(i + 2) for i, rel in enumerate(ideal))
    return dcg / idcg if idcg > 0 else 0.0


def _mrr(relevances: list[int]) -> float:
    for i, rel in enumerate(relevances):
        if rel > 0:
            return 1.0 / (i + 1)
    return 0.0


async def _run_query(project_path: str, query: str, top_k: int) -> list[str]:
    from opencode_search.handlers._query import handle_search_code
    result = await handle_search_code(
        query=query,
        project_paths=[project_path],
        top_k=top_k,
        use_rerank=True,
    )
    return [r.get("path", "") for r in result.get("results", [])]


def _is_relevant(path: str, substrings: list[str]) -> bool:
    p = path.lower()
    return any(s in p for s in substrings)


async def _benchmark(project_path: str, top_k: int = 10) -> dict:
    results: list[dict] = []
    for query, relevant_substrings in _GOLD_SET:
        paths = await _run_query(project_path, query, top_k)
        relevances = [1 if _is_relevant(p, relevant_substrings) else 0 for p in paths]
        results.append({
            "query": query,
            "relevant_substrings": relevant_substrings,
            "top_paths": paths[:3],
            "relevances": relevances,
            "mrr": round(_mrr(relevances), 4),
            "ndcg@10": round(_ndcg(relevances, 10), 4),
            "hit": any(r > 0 for r in relevances),
        })

    mrr_avg = sum(r["mrr"] for r in results) / len(results)
    ndcg_avg = sum(r["ndcg@10"] for r in results) / len(results)
    hit_rate = sum(1 for r in results if r["hit"]) / len(results)

    return {
        "project_path": project_path,
        "queries": len(results),
        "top_k": top_k,
        "mrr@10": round(mrr_avg, 4),
        "ndcg@10": round(ndcg_avg, 4),
        "hit_rate@10": round(hit_rate, 4),
        "details": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Search quality benchmark")
    parser.add_argument("--project", required=True, help="Indexed project path")
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--json", action="store_true", help="Output full JSON")
    args = parser.parse_args()

    report = asyncio.run(_benchmark(args.project, top_k=args.top_k))

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"\nSearch Quality Benchmark — {Path(args.project).name}")
        print(f"{'='*50}")
        print(f"  MRR@10:       {report['mrr@10']:.3f}  (target ≥ 0.50)")
        print(f"  NDCG@10:      {report['ndcg@10']:.3f}  (target ≥ 0.40)")
        print(f"  Hit Rate@10:  {report['hit_rate@10']:.3f}  ({int(report['hit_rate@10']*len(report['details']))}/{len(report['details'])} queries)")
        print()
        print(f"  {'Query':<45}  MRR    NDCG   Hit")
        print(f"  {'-'*45}  -----  -----  ---")
        for r in report["details"]:
            hit = "✓" if r["hit"] else "✗"
            print(f"  {r['query'][:44]:<45}  {r['mrr']:.3f}  {r['ndcg@10']:.3f}  {hit}")
        print()

    mrr_pass = report["mrr@10"] >= 0.50
    if not mrr_pass:
        print(f"WARN: MRR@10 {report['mrr@10']:.3f} < 0.50 threshold", file=sys.stderr)
    return 0 if mrr_pass else 1


if __name__ == "__main__":
    sys.exit(main())
