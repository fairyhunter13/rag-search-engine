"""Pipelined driver: fully document all 24 federation members.

Architecture — two-stage async pipeline:
  Stage 1 (GPU embedding): serialized — only 1 member indexed at a time.
                            ONNX embedder uses ~2-3 GB VRAM.
  Stage 2 (LLM enrich/wiki): up to 2 members concurrently.
                            Ollama phi4-mini uses ~2.5 GB VRAM (separate budget).

While member A is enriching (Ollama), member B can index (ONNX embedder).
These use different GPU resources, giving ~1.7-2x speedup with no OOM risk.

VRAM budget (RTX 5080 16 GB):
  ONNX embedder: ~2.5 GB
  Ollama phi4-mini: ~2.5 GB
  Overlapped (safe ceiling): ~6 GB  (well within 16 GB)
  Batch size reduced to 8 during overlap via OPENCODE_ONNX_BATCH_SIZE=8.

Usage:
  python scripts/document_federation.py             # run all remaining
  python scripts/document_federation.py --status    # print status table
  python scripts/document_federation.py --force     # re-process completed
  python scripts/document_federation.py --workers 3 # 3 parallel pipelines
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("doc_fed")

_ASTRO = "/home/user/git/github.com/fairyhunter13/redacted-name-3"
_CHECKPOINT = Path("~/.local/share/opencode-search/federation_doc_checkpoint.json").expanduser()

# Resource semaphores (set in main())
_INDEX_SEM: asyncio.Semaphore | None = None   # 1 ONNX session at a time
_LLM_SEM: asyncio.Semaphore | None = None     # concurrent Ollama calls

# GPU/resource settings:
# - ONNX_BATCH_SIZE=32: full throughput per forward pass (RTX 5080 default)
# - EMBED_BATCH_CHUNKS=64: cap chunk accumulation to prevent VRAM spikes when
#   Ollama (2.5GB) + ONNX (2.5GB) run simultaneously. 64 chunks * ~500 chars
#   ≈ 400MB peak activation — safe. 128 caused OOM during overlap.
# - LLM_CONCURRENCY=3: more concurrent Ollama threads for faster enrichment
# - GPU usage target: ~100% (both ONNX embedder + Ollama phi4-mini active)
# - CPU/RAM target: minimal (GPU does all heavy work)
_GPU_ENV = {
    "OPENCODE_ONNX_BATCH_SIZE": "32",      # full ONNX throughput
    "OPENCODE_EMBED_BATCH_CHUNKS": "64",   # prevent VRAM spikes during overlap
    "OPENCODE_LLM_CONCURRENCY": "3",       # faster Ollama enrichment
}
_MAX_COMMUNITIES = 200


def _checkpoint_load() -> dict:
    if _CHECKPOINT.exists():
        try:
            return json.loads(_CHECKPOINT.read_text())
        except Exception:
            pass
    return {}


def _checkpoint_save(data: dict) -> None:
    _CHECKPOINT.parent.mkdir(parents=True, exist_ok=True)
    _CHECKPOINT.write_text(json.dumps(data, indent=2))


def _quick_status(path: str) -> dict:
    """Lightweight status — no LanceDB open needed."""
    from opencode_search.config import get_project_graph_db_path, get_project_wiki_dir, load_registry
    from opencode_search.graph.storage import GraphStorage

    reg = load_registry()
    entry = reg.get(path)
    indexed = entry is not None and entry.indexed_at is not None

    graph_db = get_project_graph_db_path(path)
    graph_exists = Path(graph_db).exists()
    wiki_dir = get_project_wiki_dir(path)
    wiki_count = len(list(wiki_dir.glob("*.md"))) if wiki_dir.exists() else 0

    enriched = total = 0
    if graph_exists:
        try:
            gs = GraphStorage(graph_db)
            gs.open()
            all_c = gs.get_communities()
            total = len(all_c)
            enriched = sum(1 for c in all_c if c.title)
            gs.close()
        except Exception:
            pass

    return {
        "path": path,
        "indexed": indexed,
        "graph": graph_exists,
        "communities": total,
        "enriched": enriched,
        "wiki_pages": wiki_count,
    }


async def _enrich_until_complete(path: str, max_per_batch: int = _MAX_COMMUNITIES) -> dict:
    """Loop enrichment until no unenriched meaningful communities remain."""
    from opencode_search.handlers import handle_enrich_project
    from opencode_search.config import get_project_graph_db_path
    from opencode_search.graph.storage import GraphStorage

    total_enriched = rounds = 0
    while True:
        rounds += 1
        graph_db = get_project_graph_db_path(path)
        gs = GraphStorage(graph_db)
        gs.open()
        all_c = gs.get_communities(min_node_count=2)
        unenriched = [c for c in all_c if not c.title]
        gs.close()

        if not unenriched:
            break
        log.info("[%s] enrich round %d — %d remaining", Path(path).name, rounds, len(unenriched))
        result = await handle_enrich_project(
            project_path=path, scope="communities",
            max_communities=max_per_batch, include_federation=False,
        )
        batch = result.get("enriched_communities", 0)
        total_enriched += batch
        if batch == 0:
            log.warning("[%s] 0 enriched — LLM may be unavailable", Path(path).name)
            break

    return {"enriched": total_enriched, "rounds": rounds}


async def _stage1_index(path: str, force: bool) -> dict:
    """Stage 1: index + build graph. GPU embedding serialized via _INDEX_SEM.

    Calls _run_index_project() directly (not handle_index_project) so the
    semaphore wraps the ACTUAL GPU work, not just the background task spawn.
    handle_index_project returns immediately with status='indexing' — the real
    work would run outside the semaphore if we used it.

    GPU policy: batch=32 (full ONNX throughput), chunk_cap=64 (prevents VRAM
    spikes when Ollama holds 2.5GB simultaneously). ONNX session released after
    each member — prevents cross-member VRAM fragmentation.
    """
    from opencode_search.handlers._index import _run_index_project, _build_graph_sync
    from opencode_search.embeddings import cleanup_models
    from opencode_search.config import get_project_graph_db_path

    name = Path(path).name
    project_path_obj = Path(path).expanduser().resolve()

    # Apply GPU-optimal env vars before opening ONNX session
    for k, v in _GPU_ENV.items():
        os.environ[k] = v

    log.info("[%s] ▶ stage1: waiting for GPU slot...", name)
    t0 = time.perf_counter()

    async with _INDEX_SEM:
        log.info("[%s] ▶ stage1: GPU slot acquired — indexing...", name)
        # Run the full indexing pipeline (GPU embedding + graph build) synchronously
        await _run_index_project(
            path_str=str(project_path_obj),
            project_path=project_path_obj,
            watch=False,
            force=force,
            follow_symlinks=True,
            on_complete=None,
        )
        # Release ONNX VRAM before handing GPU slot to next member
        released = await asyncio.to_thread(cleanup_models)
        if released:
            log.info("[%s] ONNX session released — VRAM freed", name)

    elapsed = time.perf_counter() - t0
    log.info("[%s] ✓ stage1: indexed + graph in %.1fs", name, elapsed)
    return {"status": "ok", "elapsed_s": round(elapsed, 1)}


async def _stage2_llm(path: str) -> dict:
    """Stage 2: enrich + wiki + ingest. Uses Ollama (can overlap with stage1 of others)."""
    from opencode_search.handlers._wiki import handle_wiki_generate, handle_wiki_reindex
    from opencode_search.handlers._pipeline import _find_doc_files
    from opencode_search.handlers._wiki import handle_wiki_ingest

    name = Path(path).name

    async with _LLM_SEM:
        # Enrich
        log.info("[%s] ▶ stage2: enriching communities...", name)
        t0 = time.perf_counter()
        enrich = await _enrich_until_complete(path)
        log.info("[%s] ✓ enriched %d communities", name, enrich["enriched"])

        # Wiki generate
        log.info("[%s] ▶ stage2: generating wiki...", name)
        wiki_result = await handle_wiki_generate(
            project_path=path, max_communities=_MAX_COMMUNITIES, include_federation=False,
        )
        wiki_pages = wiki_result.get("total", 0)
        log.info("[%s] ✓ %d wiki pages", name, wiki_pages)

        # Embed wiki pages into LanceDB
        reindex = await handle_wiki_reindex(project_path=path)
        log.info("[%s] ✓ %d wiki chunks embedded", name, reindex.get("embedded_chunks", 0))

        # Ingest docs
        ingested = 0
        try:
            doc_files = _find_doc_files(Path(path))
            for doc in doc_files:
                try:
                    r = await handle_wiki_ingest(source_path=str(doc), project_path=path)
                    if r.get("status") == "ok":
                        ingested += 1
                except Exception as e:
                    log.debug("[%s] doc ingest skip %s: %s", name, doc.name, e)
        except Exception as e:
            log.warning("[%s] doc ingest error: %s", name, e)

        elapsed = time.perf_counter() - t0
        log.info("[%s] ✓ stage2: done in %.1fs (enriched=%d wiki=%d docs=%d)",
                 name, elapsed, enrich["enriched"], wiki_pages, ingested)

    return {
        "enriched": enrich["enriched"],
        "wiki_pages": wiki_pages,
        "wiki_chunks": reindex.get("embedded_chunks", 0),
        "docs_ingested": ingested,
    }


async def document_member(path: str, force: bool = False) -> dict:
    """Full pipeline for one member: stage1 (index) → stage2 (enrich+wiki)."""
    name = Path(path).name
    st = _quick_status(path)
    log.info("[%s] start — idx=%s comms=%d enriched=%d wiki=%d",
             name, st["indexed"], st["communities"], st["enriched"], st["wiki_pages"])

    if not force and st["enriched"] > 0 and st["wiki_pages"] > 0 and st["indexed"]:
        log.info("[%s] already complete — skipping", name)
        return {"status": "skipped", **st}

    t_total = time.perf_counter()
    results: dict = {}

    # Stage 1: index (serialized GPU embedding)
    if not st["indexed"] or force:
        try:
            results["index"] = await _stage1_index(path, force)
        except Exception as e:
            log.error("[%s] stage1 FAILED: %s", name, e)
            return {"status": "error", "error": str(e), "stage": "index"}
    else:
        results["index"] = {"status": "already_indexed"}
        log.info("[%s] already indexed — skip stage1", name)

    # Stage 2: enrich + wiki (can run concurrently with other members' stage1)
    try:
        results["llm"] = await _stage2_llm(path)
    except Exception as e:
        log.error("[%s] stage2 FAILED: %s", name, e)
        return {"status": "error", "error": str(e), "stage": "llm", **results}

    elapsed = round(time.perf_counter() - t_total, 1)
    log.info("[%s] ✅ DONE in %.1fs", name, elapsed)
    return {"status": "ok", "elapsed_s": elapsed, **results}


async def run_status() -> None:
    from opencode_search.handlers import handle_list_federation
    result = await handle_list_federation(project_path=_ASTRO)
    raw = result.get("members", [])
    members = [m["path"] if isinstance(m, dict) else m for m in raw]

    print(f"\n{'Member':<35} {'Idx':>4} {'Comms':>6} {'Enr':>5} {'Wiki':>5}")
    print("-" * 60)
    for m in members:
        st = _quick_status(m)
        done = "✅" if (st["enriched"] > 0 and st["wiki_pages"] > 0) else "⏳"
        print(f"{done} {Path(m).name:<33} {str(st['indexed']):>4} "
              f"{st['communities']:>6} {st['enriched']:>5} {st['wiki_pages']:>5}")

    root = _quick_status(_ASTRO)
    print("-" * 60)
    print(f"✅ ROOT (redacted-name-3)              {str(root['indexed']):>4} "
          f"{root['communities']:>6} {root['enriched']:>5} {root['wiki_pages']:>5}")


async def main() -> None:
    global _INDEX_SEM, _LLM_SEM

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--workers", type=int, default=2,
                        help="Number of concurrent pipeline workers (default 2)")
    parser.add_argument("--members", nargs="*",
                        help="Process only these member basenames")
    args = parser.parse_args()

    if args.status:
        await run_status()
        return

    # Workers: index semaphore always 1 (GPU OOM prevention)
    # LLM semaphore = workers (each worker can enrich independently)
    _INDEX_SEM = asyncio.Semaphore(1)
    _LLM_SEM = asyncio.Semaphore(args.workers)

    from opencode_search.handlers import handle_list_federation
    result = await handle_list_federation(project_path=_ASTRO)
    raw = result.get("members", [])
    all_members = [m["path"] if isinstance(m, dict) else m for m in raw]

    if args.members:
        all_members = [m for m in all_members if Path(m).name in args.members or m in args.members]

    checkpoint = _checkpoint_load()
    completed = set(checkpoint.get("completed", []))
    remaining = [m for m in all_members if m not in completed or args.force]

    log.info("Federation docs: %d members remaining (%d already done), workers=%d",
             len(remaining), len(completed), args.workers)

    async def _run_member(path: str) -> None:
        try:
            result = await document_member(path, force=args.force)
            if result.get("status") in ("ok", "skipped"):
                completed.add(path)
                checkpoint["completed"] = list(completed)
                _checkpoint_save(checkpoint)
                log.info("[%s] checkpoint saved (%d/%d done)",
                         Path(path).name, len(completed), len(all_members))
        except Exception as e:
            log.error("[%s] unhandled error: %s", Path(path).name, e)

    # Run all remaining members concurrently — the semaphores enforce resource limits
    await asyncio.gather(*[_run_member(m) for m in remaining])

    log.info("All members done. Final status:")
    await run_status()


if __name__ == "__main__":
    asyncio.run(main())
