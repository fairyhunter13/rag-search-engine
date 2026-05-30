"""
Enrichment pipeline for ~/git/github.com/fairyhunter13/redacted-name-3.

Runs in order:
  1. Wait for indexing to complete (graph.db + community detection must be done)
  2. Enrich communities with LLM titles/summaries (phi4-mini:3.8b via Ollama)
  3. Generate wiki pages for all communities
  4. Ingest all project-level markdown docs into the wiki
  5. Print a summary report

Run:
    OPENCODE_LLM_PROVIDER=ollama .venv/bin/python scripts/enrich_astro.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

# project root on sys.path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

ACME_PROJECT = Path.home() / "git/github.com/fairyhunter13/redacted-name-3"

# Docs to ingest into the wiki (highest signal first)
DOCS_TO_INGEST = [
    "ARCHITECTURE-SUMMARY.md",
    "context.md",
    "docs/project-overview.md",
    "docs/technology-stack.md",
    "docs/integration-architecture.md",
    "docs/api-authentication.md",
    "docs/api-contracts.md",
    "docs/grpc-endpoints.md",
    "docs/deployment-guide.md",
    "docs/development-guide.md",
    "docs/protobuf-versioning-guide.md",
    "docs/data-models.md",
    "docs/source-tree-analysis.md",
    "docs/cli-tools-guide.md",
    "docs/repositories/acme-promo-be.md",
    "docs/repositories/acme-campaign-be.md",
    "docs/repositories/acme-api-customer-spring.md",
    "docs/repositories/acme-api-admin-spring.md",
    "docs/repositories/acme-kong.md",
]


def _print(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


async def wait_for_index_complete(project_path: str, poll_interval: int = 30) -> None:
    """Poll project_status until indexed_at is set (full pipeline done)."""
    from opencode_search.handlers import handle_project_status
    _print("Waiting for indexing + graph + community detection to complete...")
    while True:
        status = await handle_project_status(path=project_path)
        if not status.get("indexing_running") and status.get("indexed_at"):
            _print(f"Indexing complete. chunks={status.get('chunks')} files={status.get('file_count')}")
            return
        running = status.get("indexing_running", "?")
        indexed_at = status.get("indexed_at")
        _print(f"  still running={running}, indexed_at={indexed_at} — retrying in {poll_interval}s")
        await asyncio.sleep(poll_interval)


async def enrich_communities(project_path: str) -> dict:
    """Run LLM community enrichment."""
    from opencode_search.handlers._enrichment import handle_enrich_project
    _print("Enriching communities with LLM (phi4-mini:3.8b)...")
    t0 = time.monotonic()
    result = await handle_enrich_project(project_path=project_path, scope="communities")
    elapsed = time.monotonic() - t0
    if result.get("status") == "ok":
        _print(f"  Community enrichment done: {result.get('enriched_communities', 0)} communities in {elapsed:.1f}s")
    else:
        _print(f"  Community enrichment result: {result}")
    return result


async def generate_wiki(project_path: str) -> dict:
    """Generate wiki pages for all communities."""
    from opencode_search.handlers._wiki import handle_wiki_generate
    _print("Generating wiki pages for all communities...")
    t0 = time.monotonic()
    result = await handle_wiki_generate(project_path=project_path)
    elapsed = time.monotonic() - t0
    if result.get("status") == "ok":
        pages = result.get("pages_created", [])
        _print(f"  Wiki generated: {len(pages) if isinstance(pages, list) else pages} pages in {elapsed:.1f}s")
    else:
        _print(f"  Wiki generate result: {result}")
    return result


async def ingest_docs(project_path: str) -> list[dict]:
    """Ingest all project markdown docs into the wiki."""
    from opencode_search.handlers._wiki import handle_wiki_ingest
    results = []
    existing = [d for d in DOCS_TO_INGEST if (ACME_PROJECT / d).exists()]
    _print(f"Ingesting {len(existing)}/{len(DOCS_TO_INGEST)} docs into wiki...")
    for doc_rel in existing:
        doc_path = str(ACME_PROJECT / doc_rel)
        _print(f"  Ingesting: {doc_rel}")
        t0 = time.monotonic()
        result = await handle_wiki_ingest(source_path=doc_path, project_path=project_path)
        elapsed = time.monotonic() - t0
        status = result.get("status", "?")
        pages = result.get("pages_created", [])
        _print(f"    → {status}, {len(pages) if isinstance(pages, list) else pages} pages ({elapsed:.1f}s)")
        results.append({"doc": doc_rel, "result": result})
    return results


async def show_communities(project_path: str) -> None:
    """Print detected communities with their titles."""
    from opencode_search.handlers._graph import handle_get_communities
    result = await handle_get_communities(project_path=project_path)
    communities = result.get("communities", [])
    _print(f"\nDetected {len(communities)} communities:")
    for c in sorted(communities, key=lambda x: x.get("node_count", 0), reverse=True)[:20]:
        title = c.get("title") or "(no title yet)"
        cid = c.get("id")
        n = c.get("node_count", 0)
        eps = ", ".join(c.get("key_entry_points", [])[:3])
        _print(f"  [{cid}] {title!r} — {n} nodes  entry_points: {eps or 'none'}")


async def show_sample_searches(project_path: str) -> None:
    """Run a few global_search queries to verify the knowledge base."""
    from opencode_search.handlers._graph import handle_global_search
    queries = [
        "authentication JWT token middleware",
        "cart checkout order management",
        "notification push email SMS",
        "loyalty points rewards program",
        "gRPC protobuf inter-service communication",
    ]
    _print("\nSample global_search queries:")
    for q in queries:
        result = await handle_global_search(query=q, project_path=project_path)
        comm = result.get("community_matches", 0)
        wiki = result.get("wiki_matches", 0)
        total = result.get("total", 0)
        top = result.get("results", [{}])[0] if result.get("results") else {}
        top_type = top.get("type", "")
        top_title = top.get("title") or top.get("name") or ""
        _print(f"  {q!r}")
        _print(f"    → total={total} (comm={comm}, wiki={wiki})  top={top_type}:{top_title!r}")


async def run_wiki_lint(project_path: str) -> None:
    """Run wiki health check."""
    from opencode_search.handlers._wiki import handle_wiki_lint
    result = await handle_wiki_lint(project_path=project_path)
    _print(f"\nWiki lint: healthy={result.get('healthy')} "
           f"orphans={len(result.get('orphan_pages', []))} "
           f"empty={len(result.get('empty_pages', []))}")


async def main() -> None:
    project_path = str(ACME_PROJECT)

    # Check LLM provider
    provider = os.environ.get("OPENCODE_LLM_PROVIDER", "ollama")
    model = os.environ.get("OPENCODE_LLM_MODEL", "phi4-mini:3.8b")
    _print(f"LLM provider: {provider}, model: {model}")
    _print(f"Project: {project_path}")
    _print("=" * 60)

    # 1. Wait for full indexing (graph + community detection done)
    await wait_for_index_complete(project_path, poll_interval=30)

    # 2. Show raw community structure before enrichment
    await show_communities(project_path)

    # 3. Enrich communities with LLM titles/summaries
    await enrich_communities(project_path)

    # 4. Generate wiki pages from communities
    await generate_wiki(project_path)

    # 5. Ingest project docs
    await ingest_docs(project_path)

    # 6. Re-show communities with enriched titles
    _print("\nCommunities after enrichment:")
    await show_communities(project_path)

    # 7. Verify with global_search
    await show_sample_searches(project_path)

    # 8. Wiki health check
    await run_wiki_lint(project_path)

    _print("\n✓ Enrichment pipeline complete.")
    _print(f"  Wiki at: ~/.local/share/opencode-search/indexes/redacted-name-3-*/wiki/")


if __name__ == "__main__":
    asyncio.run(main())
