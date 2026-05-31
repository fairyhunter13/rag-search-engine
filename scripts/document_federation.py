"""GPU-maximizing pipelined driver: document all 24 federation members.

Resource model (RTX 5080 16 GB):
  _EMBED_SEM(1) — ONE ONNX session active at any time.
                  Covers indexing, wiki_reindex, and doc ingest (all embed).
  _LLM_SEM(N)  — N concurrent Ollama workers (enrich + wiki_generate).
                  Ollama runs alongside ONNX safely (different VRAM pools).

GPU policy:
  ONNX batch size: 32       — full per-pass throughput (RTX 5080 default)
  ONNX chunk cap:  128      — max accumulation; safe with single session
  LLM concurrency: 3        — max concurrent Ollama enrichment threads
  Target GPU util: ~100%    — ONNX embedder or Ollama active at all times
  Target VRAM:     ~10 GB   — ONNX(2.5) + Ollama(2.5) + chonkie(1) + workspace(3)

Pipeline per member:
  stage1 [_EMBED_SEM]: index → graph → cleanup_models (releases ONNX VRAM)
  stage2 [_LLM_SEM]:  enrich → wiki_generate
         [_EMBED_SEM]: wiki_reindex + ingest_docs (embed wiki/doc pages)

While member A is in stage2-LLM (Ollama, no _EMBED_SEM), member B's
stage1 can run (ONNX, holds _EMBED_SEM). This is the key overlap.

Usage:
  python scripts/document_federation.py              # resume from checkpoint
  python scripts/document_federation.py --workers 3  # 3 LLM workers (default 2)
  python scripts/document_federation.py --status     # show status table
  python scripts/document_federation.py --force      # re-do completed members
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

_ASTRO = "/home/user/git/github.com/fairyhunter13/astro-project"
_CHECKPOINT = Path(
    "~/.local/share/opencode-search/federation_doc_checkpoint.json"
).expanduser()

# Set once in main(); all GPU embedding paths acquire this before opening ONNX.
_EMBED_SEM: asyncio.Semaphore | None = None
_LLM_SEM:   asyncio.Semaphore | None = None

# GPU environment: single ONNX session → can use full batch sizes.
_GPU_ENV = {
    "OPENCODE_ONNX_BATCH_SIZE":        "16",   # halved from 32 — cuts activation VRAM ~50% for
                                                # coexistence with concurrent Ollama enrichment
    "OPENCODE_EMBED_BATCH_CHUNKS":     "64",   # smaller accumulation → fewer slow LanceDB writes
    "OPENCODE_LLM_CONCURRENCY":        "3",    # concurrent Ollama threads per member
    "OPENCODE_BLACKWELL_RESET_EVERY":  "50",   # fewer session resets for large repos (was 25)
}
_MAX_COMMUNITIES = 200


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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
    from opencode_search.config import (
        get_project_graph_db_path, get_project_wiki_dir, load_registry,
    )
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

    return dict(
        path=path, indexed=indexed, graph=graph_exists,
        communities=total, enriched=enriched, wiki_pages=wiki_count,
    )


async def _enrich_until_complete(path: str) -> int:
    from opencode_search.handlers import handle_enrich_project
    from opencode_search.config import get_project_graph_db_path
    from opencode_search.graph.storage import GraphStorage

    total = rounds = 0
    while True:
        rounds += 1
        gs = GraphStorage(get_project_graph_db_path(path))
        gs.open()
        remaining = [c for c in gs.get_communities(min_node_count=2) if not c.title]
        gs.close()
        if not remaining:
            break
        log.info("[%s] enrich round %d — %d remaining", Path(path).name, rounds, len(remaining))
        r = await handle_enrich_project(
            project_path=path, scope="communities",
            max_communities=_MAX_COMMUNITIES, include_federation=False,
        )
        batch = r.get("enriched_communities", 0)
        total += batch
        if batch == 0:
            log.warning("[%s] 0 enriched — LLM unavailable?", Path(path).name)
            break

    return total


# ---------------------------------------------------------------------------
# Stage 1: GPU embedding (serialized via _EMBED_SEM)
# ---------------------------------------------------------------------------

async def _stage1_index(path: str, force: bool) -> None:
    """Index + build graph. Holds _EMBED_SEM for the entire GPU phase."""
    from opencode_search.handlers._index import _run_index_project
    from opencode_search.embeddings import cleanup_models

    name = Path(path).name
    project_path_obj = Path(path).expanduser().resolve()

    for k, v in _GPU_ENV.items():
        os.environ[k] = v

    log.info("[%s] stage1: waiting for GPU slot…", name)
    async with _EMBED_SEM:
        log.info("[%s] stage1: GPU slot acquired — indexing…", name)
        t0 = time.perf_counter()
        await _run_index_project(
            path_str=str(project_path_obj),
            project_path=project_path_obj,
            watch=False, force=force, follow_symlinks=True,
            on_complete=None,
        )
        # Release ONNX VRAM before handing slot to next member.
        released = await asyncio.to_thread(cleanup_models)
        if released:
            log.info("[%s] ONNX session released — VRAM freed", name)
        log.info("[%s] ✓ stage1: %.1fs", name, time.perf_counter() - t0)


# ---------------------------------------------------------------------------
# Stage 2-LLM: Ollama enrichment + wiki generation (parallel, no embedding)
# ---------------------------------------------------------------------------

async def _stage2_llm(path: str) -> tuple[int, int]:
    """Enrich communities + generate wiki pages via Ollama. No ONNX needed."""
    from opencode_search.handlers._wiki import handle_wiki_generate

    name = Path(path).name
    log.info("[%s] stage2-llm: enriching…", name)
    enriched = await _enrich_until_complete(path)
    log.info("[%s] ✓ enriched %d communities", name, enriched)

    log.info("[%s] stage2-llm: generating wiki pages…", name)
    wiki_result = await handle_wiki_generate(
        project_path=path,
        max_communities=_MAX_COMMUNITIES,
        include_federation=False,
        embed=False,  # stage2_embed will embed under _EMBED_SEM — don't embed here
    )
    wiki_pages = wiki_result.get("total", 0)
    log.info("[%s] ✓ %d wiki pages generated", name, wiki_pages)
    return enriched, wiki_pages


# ---------------------------------------------------------------------------
# Stage 2-Embed: wiki_reindex + ingest_docs (re-acquires _EMBED_SEM)
# ---------------------------------------------------------------------------

async def _stage2_embed(path: str, wiki_pages: int) -> tuple[int, int]:
    """Embed wiki pages and ingest docs. Re-acquires _EMBED_SEM for ONNX work."""
    from opencode_search.handlers._wiki import handle_wiki_reindex, handle_wiki_ingest
    from opencode_search.handlers._pipeline import _find_doc_files
    from opencode_search.embeddings import cleanup_models

    name = Path(path).name

    for k, v in _GPU_ENV.items():
        os.environ[k] = v

    log.info("[%s] stage2-embed: waiting for GPU slot to embed %d wiki pages…",
             name, wiki_pages)
    chunks = ingested = 0
    async with _EMBED_SEM:
        log.info("[%s] stage2-embed: GPU slot acquired — embedding wiki…", name)
        t0 = time.perf_counter()

        r = await handle_wiki_reindex(project_path=path)
        chunks = r.get("embedded_chunks", 0)
        log.info("[%s] ✓ %d wiki chunks embedded", name, chunks)

        # Ingest doc files (each call also embeds into LanceDB)
        doc_files = _find_doc_files(Path(path))
        for doc in doc_files:
            try:
                result = await handle_wiki_ingest(
                    source_path=str(doc), project_path=path
                )
                if result.get("status") == "ok":
                    ingested += 1
            except Exception as exc:
                log.debug("[%s] doc ingest skip %s: %s", name, doc.name, exc)

        # Release ONNX VRAM after all embedding for this member
        released = await asyncio.to_thread(cleanup_models)
        if released:
            log.info("[%s] ONNX released after wiki embed (%.1fs)",
                     name, time.perf_counter() - t0)

    return chunks, ingested


# ---------------------------------------------------------------------------
# Full member pipeline
# ---------------------------------------------------------------------------

async def document_member(path: str, force: bool = False) -> dict:
    name = Path(path).name
    st = _quick_status(path)
    log.info("[%s] start — idx=%s comms=%d enriched=%d wiki=%d",
             name, st["indexed"], st["communities"], st["enriched"], st["wiki_pages"])

    if not force and st["enriched"] > 0 and st["wiki_pages"] > 0 and st["indexed"]:
        log.info("[%s] already complete — skipping", name)
        return {"status": "skipped"}

    t_total = time.perf_counter()

    # Stage 1: index (serialized GPU embedding)
    if not st["indexed"] or force:
        try:
            await _stage1_index(path, force)
        except Exception as exc:
            log.error("[%s] stage1 FAILED: %s", name, exc)
            return {"status": "error", "stage": "index", "error": str(exc)}
    else:
        log.info("[%s] already indexed — skipping stage1", name)

    # Stage 2-LLM: enrich + wiki_generate (can overlap with other members' stage1)
    try:
        async with _LLM_SEM:
            enriched, wiki_pages = await _stage2_llm(path)
            # Stage 2-Embed: re-acquire GPU for wiki embedding + doc ingest
            chunks, ingested = await _stage2_embed(path, wiki_pages)
    except Exception as exc:
        log.error("[%s] stage2 FAILED: %s", name, exc)
        return {"status": "error", "stage": "llm", "error": str(exc)}

    elapsed = round(time.perf_counter() - t_total, 1)
    log.info("[%s] ✅ DONE in %.1fs (enriched=%d wiki=%d chunks=%d docs=%d)",
             name, elapsed, enriched, wiki_pages, chunks, ingested)
    return {"status": "ok", "elapsed_s": elapsed}


# ---------------------------------------------------------------------------
# Status table
# ---------------------------------------------------------------------------

async def run_status() -> None:
    from opencode_search.handlers import handle_list_federation
    result = await handle_list_federation(project_path=_ASTRO)
    raw = result.get("members", [])
    members = [m["path"] if isinstance(m, dict) else m for m in raw]
    chk = set(_checkpoint_load().get("completed", []))

    print(f"\n{'':2} {'Member':<38} {'Idx':>4} {'Comms':>6} {'Enr':>5} {'Wiki':>5}")
    print("─" * 62)
    total_enr = total_wiki = 0
    for m in members:
        st = _quick_status(m)
        done = "✅" if m in chk else "⏳"
        total_enr += st["enriched"]
        total_wiki += st["wiki_pages"]
        print(f"{done} {Path(m).name:<38} {str(st['indexed']):>4} "
              f"{st['communities']:>6} {st['enriched']:>5} {st['wiki_pages']:>5}")
    print("─" * 62)
    root = _quick_status(_ASTRO)
    print(f"✅ {'ROOT (astro-project)':<38} {str(root['indexed']):>4} "
          f"{root['communities']:>6} {root['enriched']:>5} {root['wiki_pages']:>5}")
    print(f"\n{'Members done':>20}: {len(chk)}/24")
    print(f"{'Total enriched':>20}: {total_enr}")
    print(f"{'Total wiki pages':>20}: {total_wiki}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    global _EMBED_SEM, _LLM_SEM

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=2,
                        help="Concurrent LLM (Ollama) workers (default 2)")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--members", nargs="*")
    parser.add_argument("--exclude", nargs="*",
                        help="Member names to skip (for crash-isolating large repos)")
    args = parser.parse_args()

    if args.status:
        await run_status()
        return

    # Single ONNX semaphore — one session at a time prevents VRAM OOM.
    # LLM semaphore — N concurrent Ollama workers (enrich/wiki_generate).
    _EMBED_SEM = asyncio.Semaphore(1)
    _LLM_SEM   = asyncio.Semaphore(args.workers)

    from opencode_search.handlers import handle_list_federation
    result = await handle_list_federation(project_path=_ASTRO)
    raw = result.get("members", [])
    all_members = [m["path"] if isinstance(m, dict) else m for m in raw]

    if args.members:
        all_members = [m for m in all_members
                       if Path(m).name in args.members or m in args.members]

    if args.exclude:
        all_members = [m for m in all_members
                       if Path(m).name not in args.exclude and m not in args.exclude]

    chk = _checkpoint_load()
    completed: set = set(chk.get("completed", []))
    remaining = [m for m in all_members if m not in completed or args.force]

    log.info("Phase C: %d remaining, %d already done, workers=%d",
             len(remaining), len(completed), args.workers)

    async def _run(path: str) -> None:
        try:
            r = await document_member(path, force=args.force)
            if r.get("status") in ("ok", "skipped"):
                completed.add(path)
                chk["completed"] = list(completed)
                _checkpoint_save(chk)
                log.info("checkpoint: %d/24 done", len(completed))
        except Exception as exc:
            log.error("[%s] unhandled: %s", Path(path).name, exc)

    await asyncio.gather(*[_run(m) for m in remaining])

    log.info("All members processed.")
    await run_status()


if __name__ == "__main__":
    asyncio.run(main())
