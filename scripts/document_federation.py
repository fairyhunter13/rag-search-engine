"""Resumable driver: fully document all 24 federation members of astro-project.

For each member: index → build graph → enrich all communities → generate wiki →
embed wiki into LanceDB → ingest member docs.

Usage:
  # Full run (all 24 members, enrich root first):
  python scripts/document_federation.py

  # Enrich root until complete, then stop:
  python scripts/document_federation.py --root-only

  # Index + document specific members:
  python scripts/document_federation.py --members astro-golibs astro-cart-be

  # Check status without running:
  python scripts/document_federation.py --status

Checkpointing: skips members that already have wiki pages AND enriched communities
unless --force is passed. Safe to interrupt and re-run.
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
log = logging.getLogger("document_federation")

_ASTRO = "/home/user/git/github.com/fairyhunter13/astro-project"
_MAX_COMMUNITIES_PER_MEMBER = 200  # per member; root uses OPENCODE_ENRICH_MAX_COMMUNITIES
_CHECKPOINT_FILE = Path("~/.local/share/opencode-search/federation_doc_checkpoint.json").expanduser()


def _load_checkpoint() -> dict:
    if _CHECKPOINT_FILE.exists():
        try:
            return json.loads(_CHECKPOINT_FILE.read_text())
        except Exception:
            pass
    return {}


def _save_checkpoint(data: dict) -> None:
    _CHECKPOINT_FILE.parent.mkdir(parents=True, exist_ok=True)
    _CHECKPOINT_FILE.write_text(json.dumps(data, indent=2))


def _member_status(path: str) -> dict:
    """Return quick status of a member without opening LanceDB."""
    from opencode_search.config import (
        get_project_graph_db_path, get_project_wiki_dir, load_registry,
    )
    from opencode_search.graph.storage import GraphStorage
    from pathlib import Path as P

    reg = load_registry()
    entry = reg.get(path)
    indexed = entry is not None and entry.indexed_at is not None

    graph_db = get_project_graph_db_path(path)
    graph_exists = P(graph_db).exists()

    wiki_dir = get_project_wiki_dir(path)
    wiki_count = len(list(wiki_dir.glob("*.md"))) if wiki_dir.exists() else 0

    enriched = 0
    total_communities = 0
    if graph_exists:
        try:
            gs = GraphStorage(graph_db)
            gs.open()
            all_c = gs.get_communities()
            total_communities = len(all_c)
            enriched = sum(1 for c in all_c if c.title)
            gs.close()
        except Exception:
            pass

    return {
        "path": path,
        "indexed": indexed,
        "graph": graph_exists,
        "communities": total_communities,
        "enriched": enriched,
        "wiki_pages": wiki_count,
    }


async def _enrich_until_complete(project_path: str, max_per_batch: int = 200) -> dict:
    """Keep enriching until no unenriched meaningful communities remain."""
    from opencode_search.handlers import handle_enrich_project
    from opencode_search.config import get_project_graph_db_path
    from opencode_search.graph.storage import GraphStorage

    total_enriched = 0
    rounds = 0
    while True:
        rounds += 1
        # Count remaining
        graph_db = get_project_graph_db_path(project_path)
        gs = GraphStorage(graph_db)
        gs.open()
        all_c = gs.get_communities(min_node_count=2)
        unenriched = [c for c in all_c if not c.title]
        gs.close()

        if not unenriched:
            log.info("%s: all communities enriched after %d rounds (%d total)", project_path, rounds, total_enriched)
            break

        log.info("%s: %d unenriched communities remain — round %d", project_path, len(unenriched), rounds)
        result = await handle_enrich_project(
            project_path=project_path,
            scope="communities",
            max_communities=max_per_batch,
            include_federation=False,
        )
        batch = result.get("enriched_communities", 0)
        total_enriched += batch
        log.info("%s: enriched %d communities in round %d", project_path, batch, rounds)

        if batch == 0:
            log.warning("%s: 0 enriched in this round — LLM may be unavailable", project_path)
            break

    return {"enriched_total": total_enriched, "rounds": rounds}


async def document_member(path: str, force: bool = False) -> dict:
    """Index + graph + enrich + wiki + ingest for a single member."""
    from opencode_search.handlers import handle_index_project, handle_pipeline
    from opencode_search.handlers._wiki import handle_wiki_reindex

    t0 = time.perf_counter()
    st = _member_status(path)
    log.info("Member %s: indexed=%s graph=%s communities=%d enriched=%d wiki=%d",
             path, st["indexed"], st["graph"], st["communities"], st["enriched"], st["wiki_pages"])

    # Skip if already complete and not forcing
    if not force and st["enriched"] > 0 and st["wiki_pages"] > 0 and st["indexed"]:
        log.info("Member %s: already documented, skipping (use --force to redo)", path)
        return {"status": "skipped", "path": path, **st}

    results = {}

    # Step 1: index
    if not st["indexed"] or force:
        log.info("Member %s: indexing...", path)
        idx_result = await handle_index_project(path=path, watch=False, force=force, follow_symlinks=True)
        results["index"] = idx_result.get("status", "unknown")
        log.info("Member %s: indexed — %s", path, results["index"])
    else:
        results["index"] = "already_indexed"

    # Step 2: enrich until complete
    log.info("Member %s: enriching all communities...", path)
    enrich_result = await _enrich_until_complete(path, max_per_batch=_MAX_COMMUNITIES_PER_MEMBER)
    results["enrich"] = enrich_result

    # Step 3: generate wiki
    log.info("Member %s: generating wiki...", path)
    from opencode_search.handlers._wiki import handle_wiki_generate
    wiki_result = await handle_wiki_generate(
        project_path=path,
        max_communities=_MAX_COMMUNITIES_PER_MEMBER,
        include_federation=False,
    )
    results["wiki"] = {"pages": wiki_result.get("total", 0)}

    # Step 4: embed wiki pages
    log.info("Member %s: embedding wiki pages...", path)
    reindex_result = await handle_wiki_reindex(project_path=path)
    results["wiki_reindex"] = {"chunks": reindex_result.get("embedded_chunks", 0)}

    # Step 5: ingest docs
    log.info("Member %s: ingesting docs...", path)
    try:
        from opencode_search.handlers._pipeline import _find_doc_files
        from opencode_search.handlers._wiki import handle_wiki_ingest
        from pathlib import Path as P
        doc_files = _find_doc_files(P(path))
        ingested = 0
        for doc in doc_files:
            try:
                r = await handle_wiki_ingest(source_path=str(doc), project_path=path)
                if r.get("status") == "ok":
                    ingested += 1
            except Exception as e:
                log.debug("Doc ingest failed for %s: %s", doc, e)
        results["ingest"] = {"ingested": ingested, "scanned": len(doc_files)}
    except Exception as e:
        results["ingest"] = {"error": str(e)}

    elapsed = round(time.perf_counter() - t0, 1)
    log.info("Member %s: done in %.1fs — %s", path, elapsed, results)
    return {"status": "ok", "path": path, "elapsed_s": elapsed, **results}


async def enrich_root_until_complete() -> dict:
    """Enrich the astro-project root until all meaningful communities are done."""
    log.info("Root %s: enriching until complete...", _ASTRO)
    result = await _enrich_until_complete(_ASTRO, max_per_batch=200)

    # Regenerate wiki for newly enriched communities
    log.info("Root: regenerating wiki for newly enriched communities...")
    from opencode_search.handlers._wiki import handle_wiki_generate, handle_wiki_reindex
    wiki_result = await handle_wiki_generate(
        project_path=_ASTRO, max_communities=1000, include_federation=False,
    )
    reindex_result = await handle_wiki_reindex(project_path=_ASTRO)
    log.info("Root: %d wiki pages, %d chunks embedded", wiki_result.get("total", 0), reindex_result.get("embedded_chunks", 0))

    return {**result, "wiki_pages": wiki_result.get("total", 0), "wiki_chunks": reindex_result.get("embedded_chunks", 0)}


async def run_status() -> None:
    """Print status of all members."""
    from opencode_search.handlers import handle_list_federation
    result = await handle_list_federation(project_path=_ASTRO)
    raw = result.get("members", [])
    members = [m["path"] if isinstance(m, dict) else m for m in raw]
    print(f"\nastro-project federation: {len(members)} members\n")
    print(f"{'Path':<60} {'Idx':>4} {'Comms':>6} {'Enrich':>7} {'Wiki':>5}")
    print("-" * 85)
    for m in members:
        st = _member_status(m)
        print(f"{m:<60} {str(st['indexed']):>4} {st['communities']:>6} {st['enriched']:>7} {st['wiki_pages']:>5}")
    root_st = _member_status(_ASTRO)
    print("-" * 85)
    print(f"{'ROOT (astro-project)':<60} {str(root_st['indexed']):>4} {root_st['communities']:>6} {root_st['enriched']:>7} {root_st['wiki_pages']:>5}")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root-only", action="store_true", help="Only enrich/wiki the root, skip members")
    parser.add_argument("--members", nargs="*", help="Only process these member basenames")
    parser.add_argument("--force", action="store_true", help="Re-process even if already documented")
    parser.add_argument("--status", action="store_true", help="Print status and exit")
    parser.add_argument("--skip-root", action="store_true", help="Skip root enrichment, go straight to members")
    args = parser.parse_args()

    if args.status:
        await run_status()
        return

    # Phase D: enrich root until complete
    if not args.skip_root:
        log.info("=== Phase D: Enrich root until complete ===")
        root_result = await enrich_root_until_complete()
        log.info("Root complete: %s", root_result)
    else:
        log.info("Skipping root enrichment (--skip-root)")

    if args.root_only:
        log.info("--root-only: stopping after root")
        return

    # Phase C: document all 24 members
    log.info("=== Phase C: Document all federation members ===")
    from opencode_search.handlers import handle_list_federation
    result = await handle_list_federation(project_path=_ASTRO)
    raw_members = result.get("members", [])
    # handle_list_federation returns dicts {"path": ..., "indexed": ..., "file_count": ...}
    all_members = [m["path"] if isinstance(m, dict) else m for m in raw_members]

    if args.members:
        members = [m for m in all_members if Path(m).name in args.members or m in args.members]
        log.info("Filtered to %d members: %s", len(members), members)
    else:
        members = all_members

    checkpoint = _load_checkpoint()
    completed = checkpoint.get("completed", [])
    remaining = [m for m in members if m not in completed or args.force]
    log.info("%d members to process (%d already done)", len(remaining), len(completed))

    for i, member in enumerate(remaining, 1):
        log.info("=== Member %d/%d: %s ===", i, len(remaining), member)
        try:
            member_result = await document_member(member, force=args.force)
            if member_result.get("status") in ("ok", "skipped"):
                completed.append(member)
                checkpoint["completed"] = list(set(completed))
                _save_checkpoint(checkpoint)
        except Exception as e:
            log.error("Member %s failed: %s", member, e)

    log.info("All members processed. %d completed.", len(completed))
    await run_status()


if __name__ == "__main__":
    asyncio.run(main())
