"""Full knowledge-base pipeline: discover → enrich → wiki → ingest docs."""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

from opencode_search.enricher.client import create_llm_client

log = logging.getLogger(__name__)

# Documentation file patterns to auto-ingest
_DOC_GLOBS = [
    "*.md", "*.rst", "*.txt",
    "docs/**/*.md", "docs/**/*.rst",
    "documentation/**/*.md",
    "wiki/**/*.md",
]
_MAX_DOCS_TO_INGEST = 20  # safety cap — avoid flooding wiki with raw content
_DOC_MAX_BYTES = 200 * 1024  # skip files > 200 KB


def _find_doc_files(root: Path) -> list[Path]:
    """Return up to _MAX_DOCS_TO_INGEST doc files from the project root."""
    found: list[Path] = []
    seen: set[str] = set()

    def _add(p: Path) -> None:
        r = str(p.resolve())
        if r not in seen and p.is_file() and p.stat().st_size <= _DOC_MAX_BYTES:
            seen.add(r)
            found.append(p)

    # Top-level patterns (non-recursive)
    for pattern in ["*.md", "*.rst"]:
        for p in sorted(root.glob(pattern)):
            _add(p)
            if len(found) >= _MAX_DOCS_TO_INGEST:
                return found

    # Recursive inside docs/, documentation/, wiki/
    for sub in ("docs", "documentation", "wiki"):
        sub_dir = root / sub
        if sub_dir.is_dir() and not sub_dir.is_symlink():
            for p in sorted(sub_dir.rglob("*.md")):
                _add(p)
                if len(found) >= _MAX_DOCS_TO_INGEST:
                    return found
            for p in sorted(sub_dir.rglob("*.rst")):
                _add(p)
                if len(found) >= _MAX_DOCS_TO_INGEST:
                    return found

    return found


async def handle_pipeline(
    project_path: str,
    enrich_max_communities: int = 100,
    wiki_max_communities: int = 100,
    index_members: bool = False,
    ingest_docs: bool = True,
    watch: bool = False,
) -> dict[str, Any]:
    """Run the full knowledge-base pipeline for a project.

    Executes the following steps in order, skipping steps that are not
    applicable (e.g. enrich/wiki are skipped if LLM is unavailable):

    1. **discover** — auto-discover federation members from symlinks/workspace files
       and register them in the project registry.
    2. **index_members** — (optional, default off) index each federation member
       as a separate project. Skip if the root was indexed with follow_symlinks=True
       and already covers all member content.
    3. **enrich** — LLM-generate titles and summaries for the top N communities
       (largest-first). Includes federation members with separate graphs.
    4. **wiki** — generate markdown wiki pages for the top N communities.
       Includes federation members.
    5. **ingest_docs** — scan the project root for markdown/rst documentation files
       and ingest them into the wiki knowledge base.

    Args:
        enrich_max_communities: Communities to enrich per project (default 100).
        wiki_max_communities: Communities to wiki per project (default 100).
        index_members: If True, index each federation member separately.
            Default False — safe when root was indexed with follow_symlinks=True.
        ingest_docs: If True, automatically ingest documentation files found
            in the project root and docs/ directory.
        watch: Pass watch=True to start file-watchers on indexed members.
    """
    root = str(Path(project_path).expanduser().resolve())
    t0 = time.perf_counter()
    steps: list[dict[str, Any]] = []

    # ── Step 1: Discover + register federation members ──────────────────────
    from opencode_search.handlers._federation import (
        handle_add_federation_member,
        handle_discover_federation,
    )

    discover_result = await handle_discover_federation(project_path=root)
    discovered = discover_result.get("discovered", [])
    registered_new = 0
    for member in discovered:
        r = await handle_add_federation_member(root_path=root, member_path=member)
        if r.get("status") == "ok" and r.get("total_members", 0) > 0:
            registered_new += 1

    steps.append({
        "step": "discover",
        "status": "ok",
        "discovered": len(discovered),
        "registered": registered_new,
    })
    log.info("pipeline[%s]: discovered %d federation members", root, len(discovered))

    # ── Step 2: Index members (optional) ────────────────────────────────────
    if index_members and discovered:
        from opencode_search.handlers._federation import handle_index_federation

        idx_result = await handle_index_federation(root_path=root, watch=watch)
        steps.append({"step": "index_members", **idx_result})
        log.info("pipeline[%s]: indexed %d members", root, len(idx_result.get("indexed", [])))
    else:
        steps.append({
            "step": "index_members",
            "status": "skipped",
            "reason": "index_members=False (root index covers members via follow_symlinks)",
        })

    # ── Step 3: Enrich communities ───────────────────────────────────────────
    from opencode_search.handlers._enrichment import handle_enrich_project

    llm = create_llm_client()
    if llm is None or not llm.is_available():
        steps.append({
            "step": "enrich",
            "status": "skipped",
            "reason": "LLM not available (set OPENCODE_LLM_PROVIDER)",
        })
    else:
        enrich_result = await handle_enrich_project(
            project_path=root,
            scope="communities",
            max_communities=enrich_max_communities,
            include_federation=True,
        )
        steps.append({"step": "enrich", **enrich_result})
        log.info(
            "pipeline[%s]: enriched %d communities",
            root, enrich_result.get("enriched_communities", 0),
        )

        # Submit background symbol enrichment — non-blocking; pipeline returns immediately.
        from opencode_search.handlers._enrichment import handle_enrich_symbols_background
        from opencode_search.jobs import submit_job
        sym_job = submit_job(
            handle_enrich_symbols_background(root),
            action="enrich_symbols",
            project_path=root,
            dedup=True,
        )
        steps.append({
            "step": "enrich_symbols",
            "status": "running_background",
            "job_id": sym_job.id,
        })
        log.info("pipeline[%s]: background symbol enrichment submitted as job[%s]", root, sym_job.id)

    # ── Step 4: Generate wiki ────────────────────────────────────────────────
    from opencode_search.handlers._wiki import handle_wiki_generate

    if llm is None or not llm.is_available():
        steps.append({
            "step": "wiki",
            "status": "skipped",
            "reason": "LLM not available",
        })
    else:
        wiki_result = await handle_wiki_generate(
            project_path=root,
            max_communities=wiki_max_communities,
            include_federation=True,
        )
        steps.append({"step": "wiki", **wiki_result})
        log.info(
            "pipeline[%s]: created %d wiki pages",
            root, wiki_result.get("total", 0),
        )

    # ── Step 5: Ingest documentation files ──────────────────────────────────
    if ingest_docs and llm is not None and llm.is_available():
        from opencode_search.handlers._wiki import handle_wiki_ingest

        root_path = Path(root)
        doc_files = _find_doc_files(root_path)
        ingested: list[str] = []
        failed_docs: list[str] = []

        for doc in doc_files:
            try:
                r = await handle_wiki_ingest(
                    source_path=str(doc),
                    project_path=root,
                )
                if r.get("status") == "ok":
                    ingested.append(str(doc.relative_to(root_path)))
                else:
                    failed_docs.append(str(doc.name))
            except Exception as exc:
                log.debug("doc ingest failed for %s: %s", doc, exc)
                failed_docs.append(str(doc.name))

        steps.append({
            "step": "ingest_docs",
            "status": "ok",
            "ingested": ingested,
            "failed": failed_docs,
            "total_scanned": len(doc_files),
        })
        log.info("pipeline[%s]: ingested %d doc files", root, len(ingested))
    else:
        reason = "LLM not available" if (llm is None or not llm.is_available()) else "ingest_docs=False"
        steps.append({"step": "ingest_docs", "status": "skipped", "reason": reason})

    # ── Step 6: Build recursive community hierarchy ──────────────────────────
    # Requires ≥5 level-1 communities to be meaningful.
    # Skipped silently for small test projects.
    try:
        from opencode_search.graph.community import CommunityDetector
        from opencode_search.handlers._graph import _open_graph

        gs = _open_graph(root)
        if gs is None:
            steps.append({"step": "hierarchy", "status": "skipped", "reason": "graph not built"})
        else:
            try:
                n_level1 = len(gs.get_communities(level=1, min_node_count=2))
                if n_level1 >= 5:
                    levels_built = await asyncio.to_thread(
                        CommunityDetector().build_hierarchy, gs
                    )
                    steps.append({
                        "step": "hierarchy",
                        "status": "ok",
                        "levels_built": levels_built,
                        "max_level": gs.get_max_community_level(),
                    })
                    log.info("pipeline[%s]: hierarchy built (%d levels)", root, levels_built)
                else:
                    steps.append({
                        "step": "hierarchy",
                        "status": "skipped",
                        "reason": f"too few communities ({n_level1} < 5)",
                    })
            finally:
                import contextlib
                with contextlib.suppress(Exception):
                    gs.close()
    except Exception as exc:
        log.warning("pipeline[%s]: hierarchy build failed: %s", root, exc)
        steps.append({"step": "hierarchy", "status": "error", "error": str(exc)})

    # ── Step 7: Enrich hierarchy macro-communities ───────────────────────────
    # Generates LLM titles/summaries for level-2+ communities (architecture domains).
    if llm is not None and llm.is_available():
        try:
            from opencode_search.handlers._enrichment import handle_enrich_hierarchy
            h_result = await handle_enrich_hierarchy(project_path=root)
            steps.append({"step": "enrich_hierarchy", **h_result})
            log.info(
                "pipeline[%s]: enriched %d hierarchy communities",
                root, h_result.get("enriched", 0),
            )
        except Exception as exc:
            log.warning("pipeline[%s]: hierarchy enrichment failed: %s", root, exc)
            steps.append({"step": "enrich_hierarchy", "status": "skipped", "reason": str(exc)})
    else:
        steps.append({"step": "enrich_hierarchy", "status": "skipped", "reason": "LLM not available"})

    # ── Step 8: Vacuum storage (LanceDB version pruning + SQLite VACUUM) ────
    # Runs at end of every full pipeline build to reclaim disk space.
    # LanceDB accumulates old dataset versions on every write transaction;
    # vacuum() compacts fragmented files and prunes old versions — major
    # storage win (up to 75%), especially after incremental writes accumulate.
    vacuum_results: dict[str, Any] = {}
    try:
        from opencode_search.config import get_project_db_path, get_project_graph_db_path
        from opencode_search.graph.storage import GraphStorage
        from opencode_search.storage import Storage

        storage = Storage(db_path=get_project_db_path(root), dims=768)
        await storage.open()
        try:
            ldb_vac = await storage.vacuum()
            vacuum_results["lancedb"] = ldb_vac
        finally:
            await storage.close()

        gs = GraphStorage(get_project_graph_db_path(root))
        gs.open()
        try:
            sqlite_vac = gs.vacuum()
            vacuum_results["graph_db"] = sqlite_vac
        finally:
            gs.close()

        steps.append({"step": "vacuum", "status": "ok", **vacuum_results})
        log.info("pipeline[%s]: vacuum complete — LanceDB saved %.1f MB, graph.db saved %.1f MB",
                 root,
                 ldb_vac.get("saved_mb", 0),
                 sqlite_vac.get("saved_mb", 0))
    except Exception as exc:
        log.info("pipeline[%s]: vacuum skipped: %s", root, exc)
        steps.append({"step": "vacuum", "status": "skipped", "reason": str(exc)})

    return {
        "status": "ok",
        "project_path": root,
        "steps": steps,
        "elapsed_s": round(time.perf_counter() - t0, 2),
    }
