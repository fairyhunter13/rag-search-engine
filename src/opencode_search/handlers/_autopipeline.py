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
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


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
        if load_patterns_cache(project_path) is not None:
            return False

        return True
    except Exception:
        return True  # Assume fresh if we can't check


async def handle_auto_pipeline(project_path: str, force: bool = False) -> dict[str, Any]:
    """Run the full knowledge-base pipeline automatically after indexing.

    Checks if the project is fresh (no enrichment yet) before running.
    Set force=True to re-run even if already enriched.

    This function is designed to be called via asyncio.create_task() so the
    index operation returns immediately while the pipeline runs in background.
    """
    root = Path(project_path).expanduser().resolve()
    pp = str(root)

    if not force and not _project_is_fresh(pp):
        log.info("auto_pipeline[%s]: already enriched — skipping", root.name)
        return {"status": "skipped", "reason": "already_enriched", "project_path": pp}

    log.info("auto_pipeline[%s]: starting full KB build", root.name)
    steps: list[dict[str, Any]] = []

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

    log.info("auto_pipeline[%s]: all steps complete", root.name)
    return {"status": "ok", "project_path": pp, "steps": steps}


def schedule_auto_pipeline(project_path: str) -> None:
    """Schedule handle_auto_pipeline as a background task if enabled.

    Safe to call from any async context — creates an asyncio task without
    awaiting it, so the caller (post_index callback) returns immediately.
    """
    if not auto_pipeline_enabled():
        log.debug("auto_pipeline disabled via OPENCODE_AUTO_PIPELINE=0")
        return
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(
                handle_auto_pipeline(project_path),
                name=f"auto_pipeline:{Path(project_path).name}",
            )
            log.info("auto_pipeline[%s]: scheduled as background task", Path(project_path).name)
    except Exception as exc:
        log.warning("auto_pipeline: failed to schedule task: %s", exc)
