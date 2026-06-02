"""Auto-pipeline: trigger the full knowledge-base build automatically after indexing.

Fires after handle_index_project() completes (first-time indexing only).
Controlled by OPENCODE_AUTO_PIPELINE env var (default "1" = enabled; "0" = disabled).

Flow:
    INDEX (embedding done)
        ↓  auto-triggers
    LLM OVERVIEW  ── (Step 1 of LLM-first pattern analysis)
        ↓  in parallel
    EXACT PARSING  ── tree-sitter graph already built during index
        ↓  combines
    ENRICH  ── LLM generates community titles/summaries
    WIKI    ── LLM generates wiki pages
    INGEST  ── auto-discovers docs in project root
    ANALYZE PATTERNS  ── 3-step LLM-first pattern analysis, cached

All steps run as a background task — the index call returns immediately.
Errors in any step are logged but do not affect the index result.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# In-process event log — survives daemon restarts only within the same process.
# Entries: {"project": str, "scheduled_at": str, "status": "scheduled"|"ok"|"error"}
_PIPELINE_EVENTS: list[dict[str, Any]] = []
_MAX_EVENTS = 100  # cap to avoid unbounded growth
_BG_TASKS: set[asyncio.Task] = set()  # strong refs to prevent GC of fire-and-forget tasks


def get_pipeline_events() -> list[dict[str, Any]]:
    """Return the last N auto-pipeline events for this daemon process."""
    return list(_PIPELINE_EVENTS)


def auto_pipeline_enabled() -> bool:
    """Return True unless OPENCODE_AUTO_PIPELINE is explicitly set to 0/false/no."""
    val = os.environ.get("OPENCODE_AUTO_PIPELINE", "1").strip().lower()
    return val not in {"0", "false", "no", "off"}


def _project_is_fresh(project_path: str) -> bool:
    """Return True if the project has never been enriched (no LLM knowledge yet)."""
    try:
        from opencode_search.config import get_project_graph_db_path, get_project_wiki_dir
        from opencode_search.graph.storage import GraphStorage

        graph_db = get_project_graph_db_path(project_path)
        if Path(graph_db).exists():
            gs = GraphStorage(graph_db)
            gs.open()
            try:
                all_comms = gs.get_communities()
                enriched = sum(1 for c in all_comms if c.title)
                if enriched > 0:
                    return False
            finally:
                gs.close()

        wiki_dir = get_project_wiki_dir(project_path)
        if wiki_dir.exists() and any(wiki_dir.glob("*.md")):
            return False

        from opencode_search.handlers._patterns import load_patterns_cache
        return load_patterns_cache(project_path) is None
    except Exception:
        return True  # Assume fresh if we can't check


def _project_needs_hierarchy(project_path: str) -> bool:
    """Return True if project has enriched communities but no hierarchy (max_level == 1).

    Used to trigger hierarchy build on already-enriched projects that were
    indexed before hierarchy support was added.
    """
    try:
        from opencode_search.config import get_project_graph_db_path
        from opencode_search.graph.storage import GraphStorage

        graph_db = get_project_graph_db_path(project_path)
        if not Path(graph_db).exists():
            return False
        gs = GraphStorage(graph_db)
        gs.open()
        try:
            max_level = gs.get_max_community_level()
            n_level1 = len(gs.get_communities(level=1, min_node_count=2))
            return max_level == 1 and n_level1 >= 5
        finally:
            gs.close()
    except Exception:
        return False


async def handle_auto_pipeline(project_path: str, force: bool = False) -> dict[str, Any]:
    """Run the full knowledge-base pipeline automatically after indexing.

    Checks if the project is fresh (no enrichment yet) before running.
    Set force=True to re-run even if already enriched.

    This function is designed to be called via asyncio.create_task() so the
    index operation returns immediately while the pipeline runs in background.
    """
    root = Path(project_path).expanduser().resolve()
    pp = str(root)

    if not force and not _project_is_fresh(pp) and not _project_needs_hierarchy(pp):
        log.info("auto_pipeline[%s]: already enriched — skipping", root.name)
        return {"status": "skipped", "reason": "already_enriched", "project_path": pp}

    log.info("auto_pipeline[%s]: starting full KB build", root.name)
    steps: list[dict[str, Any]] = []
    _record_event(pp, "running")

    # ── Step 1: Enrich + Wiki + Ingest ──────────────────────────────────────
    try:
        from opencode_search.handlers._pipeline import handle_pipeline
        pipeline_result = await handle_pipeline(
            project_path=pp,
            enrich_max_communities=200,
            wiki_max_communities=200,
            ingest_docs=True,
        )
        steps.append({"step": "pipeline", "status": pipeline_result.get("status", "ok")})
        log.info("auto_pipeline[%s]: pipeline complete", root.name)
    except Exception as exc:
        log.warning("auto_pipeline[%s]: pipeline failed: %s", root.name, exc)
        steps.append({"step": "pipeline", "status": "error", "error": str(exc)})

    # ── Step 2: LLM Pattern Analysis (3-step: overview → exact → synthesis) ─
    try:
        from opencode_search.handlers._patterns import handle_analyze_patterns_llm
        pattern_result = await handle_analyze_patterns_llm(project_path=pp)
        steps.append({"step": "analyze_patterns", "status": pattern_result.get("status", "ok")})
        log.info("auto_pipeline[%s]: pattern analysis complete", root.name)
    except Exception as exc:
        log.info("auto_pipeline[%s]: pattern analysis skipped/failed: %s", root.name, exc)
        steps.append({"step": "analyze_patterns", "status": "skipped", "reason": str(exc)})

    # ── Step 3: Build + enrich community hierarchy ───────────────────────────
    # Runs on fresh projects AND on already-enriched projects that lack hierarchy.
    try:
        if _project_needs_hierarchy(pp):
            from opencode_search.config import get_project_graph_db_path
            from opencode_search.graph.community import CommunityDetector
            from opencode_search.graph.storage import GraphStorage
            db_path = get_project_graph_db_path(pp)
            gs = GraphStorage(db_path)
            gs.open()
            try:
                levels_built = await asyncio.to_thread(CommunityDetector().build_hierarchy, gs)
            finally:
                gs.close()
            from opencode_search.handlers._enrichment import handle_enrich_hierarchy
            h_result = await handle_enrich_hierarchy(project_path=pp)
            steps.append({
                "step": "hierarchy",
                "status": "ok",
                "levels_built": levels_built,
                "enriched": h_result.get("enriched", 0),
            })
            log.info("auto_pipeline[%s]: hierarchy built (%d levels)", root.name, levels_built)
        else:
            steps.append({
                "step": "hierarchy",
                "status": "skipped",
                "reason": "hierarchy already built or too few communities",
            })
    except Exception as exc:
        log.info("auto_pipeline[%s]: hierarchy skipped: %s", root.name, exc)
        steps.append({"step": "hierarchy", "status": "skipped", "reason": str(exc)})

    log.info("auto_pipeline[%s]: all steps complete", root.name)
    _record_event(pp, "ok", steps=steps)
    return {"status": "ok", "project_path": pp, "steps": steps}


async def _run_incremental_enrichment(project_path: str, modified_files: list[str]) -> None:
    """Re-enrich communities affected by file changes and refresh patterns cache.

    Called as a background task from _build_incremental_on_change after the
    graph incremental update completes — independent of any MCP request.
    """
    if not auto_pipeline_enabled():
        return

    root = Path(project_path).expanduser().resolve()
    pp = str(root)
    log.info("incremental_enrich[%s]: checking %d changed files", root.name, len(modified_files))

    try:
        from opencode_search.config import get_project_graph_db_path
        from opencode_search.graph.storage import GraphStorage

        db_path = get_project_graph_db_path(pp)
        if not Path(db_path).exists():
            log.debug("incremental_enrich[%s]: no graph DB yet, skipping", root.name)
            return

        gs = GraphStorage(db_path)
        gs.open()
        try:
            affected_ids = gs.get_communities_for_files(modified_files)
        finally:
            gs.close()

        if not affected_ids:
            log.debug("incremental_enrich[%s]: no communities affected", root.name)
        else:
            log.info(
                "incremental_enrich[%s]: re-enriching %d communities: %s",
                root.name, len(affected_ids), affected_ids[:10],
            )
            from opencode_search.handlers._enrichment import handle_enrich_project
            await handle_enrich_project(
                project_path=pp,
                scope="communities",
                community_ids=affected_ids,
            )
    except Exception as exc:
        log.debug("incremental_enrich[%s]: enrichment failed: %s", root.name, exc)

    # Always refresh patterns cache after any file change
    try:
        from opencode_search.handlers._patterns import handle_analyze_patterns_llm
        await handle_analyze_patterns_llm(project_path=pp, force=True)
        log.info("incremental_enrich[%s]: patterns cache refreshed", root.name)
    except Exception as exc:
        log.debug("incremental_enrich[%s]: patterns refresh failed: %s", root.name, exc)


def schedule_incremental_enrichment(project_path: str, modified_files: list[str]) -> None:
    """Schedule incremental re-enrichment as a background asyncio task.

    Called from _build_incremental_on_change after graph update completes.
    Returns immediately; enrichment runs in the background without blocking
    file-change processing.
    """
    if not auto_pipeline_enabled():
        return
    if not modified_files:
        return
    try:
        loop = asyncio.get_running_loop()
        task = loop.create_task(
            _run_incremental_enrichment(project_path, modified_files),
            name=f"incremental_enrich:{Path(project_path).name}",
        )
        _BG_TASKS.add(task)
        task.add_done_callback(_BG_TASKS.discard)
        log.info(
            "incremental_enrich[%s]: scheduled for %d files",
            Path(project_path).name, len(modified_files),
        )
    except RuntimeError:
        log.debug("incremental_enrich: no running event loop, skipped")
    except Exception as exc:
        log.warning("incremental_enrich: failed to schedule: %s", exc)


def _record_event(project_path: str, status: str, **extra: Any) -> None:
    entry: dict[str, Any] = {
        "project": Path(project_path).name,
        "project_path": project_path,
        "at": datetime.now(UTC).isoformat(),
        "status": status,
        **extra,
    }
    _PIPELINE_EVENTS.append(entry)
    if len(_PIPELINE_EVENTS) > _MAX_EVENTS:
        del _PIPELINE_EVENTS[0]


def schedule_auto_pipeline(project_path: str) -> None:
    """Schedule handle_auto_pipeline as a background asyncio task.

    Called from _run_index_project (in _index.py) after embedding and graph
    build complete — fires automatically from the indexer, not from any MCP
    request handler.  Returns immediately; the pipeline runs in the background.
    """
    if not auto_pipeline_enabled():
        log.debug("auto_pipeline disabled via OPENCODE_AUTO_PIPELINE=0")
        return
    try:
        loop = asyncio.get_running_loop()
        task = loop.create_task(
            handle_auto_pipeline(project_path),
            name=f"auto_pipeline:{Path(project_path).name}",
        )
        _BG_TASKS.add(task)
        task.add_done_callback(_BG_TASKS.discard)
        _record_event(project_path, "scheduled")
        log.info("auto_pipeline[%s]: scheduled as background task", Path(project_path).name)
    except RuntimeError:
        # No running event loop — skip (e.g. called from sync context or tests)
        log.debug("auto_pipeline: no running event loop, skipped scheduling")
    except Exception as exc:
        log.warning("auto_pipeline: failed to schedule task: %s", exc)
