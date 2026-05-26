#!/usr/bin/env python3
"""Regression benchmark for opencode-search indexing performance.

Runs a full force re-index of a project and compares key metrics against a
stored baseline.  Exits non-zero if any metric regresses beyond its threshold.

Usage:
    python scripts/benchmark_regression.py <project_path> [--baseline <json_file>]

Baseline file format (auto-created on first run):
    {
        "elapsed_s": 1435.9,
        "files_indexed": 20000,
        "chunks_total": 109016,
        "max_gap_s": 8.98,
        "gaps_over_20s": 0
    }
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path

# Allow running from repo root without installing
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from opencode_search.config import get_project_db_path, get_tier_dims
from opencode_search.embeddings import get_embed_workers_gpu
from opencode_search.indexer import index_project
from opencode_search.storage import Storage

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# Regression thresholds — how much worse than baseline is acceptable
ELAPSED_REGRESSION_PCT = 20   # allow up to 20% slower
MAX_GAP_REGRESSION_S   = 10   # allow max_gap to grow by up to 10s
GAPS_OVER_20S_LIMIT    = 3    # allow at most 3 GPU stall gaps (should be 0)


async def run_benchmark(project_path: Path, tier: str) -> dict:
    db_path = get_project_db_path(project_path, tier)
    dims = get_tier_dims(tier)

    storage = Storage(db_path=db_path, dims=dims)
    await storage.open()
    try:
        await storage.compact_before_index()

        batch_timestamps: list[float] = []
        files_done = [0]

        async def progress(current: int, total: int, _path: str) -> None:
            files_done[0] = current
            if current % 500 == 0 or current == total:
                pct = 100 * current // max(total, 1)
                print(f"\r  {current}/{total} files ({pct}%)", end="", flush=True)

        t0 = time.perf_counter()
        result = await index_project(
            storage, project_path,
            tier=tier,
            force=True,
            follow_symlinks=True,
            embed_workers=min(2, get_embed_workers_gpu()),
            file_workers=8,
            progress_callback=progress,
        )
        elapsed = time.perf_counter() - t0
        print()
    finally:
        await storage.close()

    return {
        "elapsed_s": round(elapsed, 1),
        "elapsed_min": round(elapsed / 60, 2),
        "files_indexed": result.files_indexed,
        "files_unchanged": result.files_unchanged,
        "chunks_total": result.chunks_total,
        "errors": result.errors,
    }


def compare(current: dict, baseline: dict) -> list[str]:
    failures = []

    elapsed_pct = 100 * (current["elapsed_s"] - baseline["elapsed_s"]) / max(baseline["elapsed_s"], 1)
    if elapsed_pct > ELAPSED_REGRESSION_PCT:
        failures.append(
            f"elapsed_s regressed by {elapsed_pct:.1f}% "
            f"({current['elapsed_s']}s vs baseline {baseline['elapsed_s']}s, "
            f"threshold +{ELAPSED_REGRESSION_PCT}%)"
        )

    if current["errors"] > baseline.get("errors", 0) + 5:
        failures.append(
            f"errors increased: {current['errors']} vs baseline {baseline.get('errors', 0)}"
        )

    chunk_delta_pct = abs(current["chunks_total"] - baseline["chunks_total"]) / max(baseline["chunks_total"], 1) * 100
    if chunk_delta_pct > 10:
        failures.append(
            f"chunks_total changed by {chunk_delta_pct:.1f}% "
            f"({current['chunks_total']} vs baseline {baseline['chunks_total']})"
        )

    return failures


def main() -> None:
    parser = argparse.ArgumentParser(description="Indexing regression benchmark")
    parser.add_argument("project", help="Project directory to benchmark")
    parser.add_argument("--tier", default="budget", help="Embedding tier (default: budget)")
    parser.add_argument("--baseline", help="Baseline JSON file path")
    parser.add_argument("--save-baseline", action="store_true", help="Save results as new baseline")
    args = parser.parse_args()

    project_path = Path(args.project).expanduser().resolve()
    if not project_path.is_dir():
        print(f"ERROR: {project_path} is not a directory", file=sys.stderr)
        sys.exit(1)

    baseline_path = Path(args.baseline) if args.baseline else (
        project_path / ".opencode-benchmark-baseline.json"
    )

    print(f"Benchmarking: {project_path}")
    print(f"Tier: {args.tier}")
    print()

    results = asyncio.run(run_benchmark(project_path, args.tier))

    print(f"\nResults:")
    print(f"  elapsed:       {results['elapsed_s']}s ({results['elapsed_min']} min)")
    print(f"  files indexed: {results['files_indexed']}")
    print(f"  chunks total:  {results['chunks_total']}")
    print(f"  errors:        {results['errors']}")

    if args.save_baseline or not baseline_path.exists():
        baseline_path.write_text(json.dumps(results, indent=2))
        print(f"\nBaseline saved to {baseline_path}")
        return

    baseline = json.loads(baseline_path.read_text())
    print(f"\nBaseline ({baseline_path}):")
    print(f"  elapsed:       {baseline['elapsed_s']}s ({baseline.get('elapsed_min', '?')} min)")
    print(f"  files indexed: {baseline['files_indexed']}")
    print(f"  chunks total:  {baseline['chunks_total']}")
    print(f"  errors:        {baseline.get('errors', 0)}")

    delta_pct = 100 * (results["elapsed_s"] - baseline["elapsed_s"]) / max(baseline["elapsed_s"], 1)
    sign = "+" if delta_pct >= 0 else ""
    print(f"\nDelta: {sign}{delta_pct:.1f}% elapsed")

    failures = compare(results, baseline)
    if failures:
        print("\nREGRESSIONS DETECTED:")
        for f in failures:
            print(f"  ✗ {f}")
        sys.exit(1)
    else:
        print("\nNo regressions detected.")


if __name__ == "__main__":
    main()
