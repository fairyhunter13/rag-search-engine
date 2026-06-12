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

# KB-level refresh cooldown — prevents a burst of file changes from triggering N refreshes.
# The index update is always immediate; only the heavier KB refresh is coalesced.
_KB_REFRESH_COOLDOWN_S: float = float(os.environ.get("OPENCODE_KB_REFRESH_COOLDOWN_S", "120"))
_KB_REFRESH_LAST: dict[str, float] = {}   # project_path → monotonic time of last KB refresh
_KB_REFRESH_IN_FLIGHT: set[str] = set()   # projects whose KB refresh task is currently running


def _should_schedule_kb_refresh(project_path: str) -> bool:
    """Return True if the cooldown has passed and no refresh is already running."""
    import time as _time
    if project_path in _KB_REFRESH_IN_FLIGHT:
        return False
    last = _KB_REFRESH_LAST.get(project_path, 0.0)
    return _time.monotonic() - last >= _KB_REFRESH_COOLDOWN_S


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


def _project_kb_incomplete(project_path: str) -> bool:
    """Return True if the project has a KB that is present but incomplete.

    A KB is incomplete when any of the following hold:
    - Graph has edges but zero communities (Leiden never ran / communities wiped — Gap A).
      Definitions-only graphs (edges==0) are NOT incomplete — they correctly verdict DONE.
    - Any non-empty community level is < 99% enriched (enrichment plateau).
    - Wiki content pages (community_*.md) are < 80% of eligible communities with
      node_count≥2 (Gap B: wiki generation was never triggered or was interrupted).

    Returns False on any error (safe-fail: prefer not to over-trigger recovery loops).
    """
    try:
        from opencode_search.config import get_project_graph_db_path, get_project_wiki_dir
        from opencode_search.graph.storage import GraphStorage

        graph_db = get_project_graph_db_path(project_path)
        if not Path(graph_db).exists():
            return False

        gs = GraphStorage(graph_db)
        gs.open()
        try:
            all_comms = gs.get_communities()
            n_communities = len(all_comms)

            # Gap A: graph has resolved edges but Leiden never produced communities (or
            # they were wiped). Use a LIMIT-1 SQL probe — avoids loading all edges just
            # to check existence; same pattern as has_cross_community_edges().
            if n_communities == 0:
                row = gs._db().execute("SELECT 1 FROM edges LIMIT 1").fetchone()
                # has edges → communities wiped/missing → incomplete
                # no edges → definitions-only → NOT incomplete (correctly DONE)
                return row is not None

            # Enrichment plateau: group communities by level; any non-empty level whose
            # enriched fraction < 99% means the KB build stalled mid-way.
            by_level: dict[int, list] = {}
            for c in all_comms:
                lvl = getattr(c, "level", 1) or 1
                by_level.setdefault(lvl, []).append(c)
            for comms in by_level.values():
                eligible = [c for c in comms if (c.node_count or 0) >= 2]
                if not eligible:
                    continue
                enriched = sum(1 for c in eligible if c.title)
                if enriched / len(eligible) < 0.99:
                    return True

            # Gap B: wiki content pages lag eligible communities.
            # The 0.8 ratio + the 6h maintenance cadence guarantee convergence without
            # churn: a regenerated wiki hits ~1.0 and stops re-firing next sweep.
            eligible_total = sum(1 for c in all_comms if (c.node_count or 0) >= 2)
            if eligible_total > 0:
                wiki_dir = get_project_wiki_dir(project_path)
                content_pages = len(list(wiki_dir.glob("community_*.md"))) if wiki_dir.exists() else 0
                if content_pages < 0.8 * eligible_total:
                    return True

            return False
        finally:
            gs.close()
    except Exception:
        return False  # safe-fail: don't over-trigger recovery loops


def _project_needs_hierarchy(project_path: str) -> bool:
    """Return True if project has enriched communities but no hierarchy (max_level == 1)
    AND a level-2 is actually buildable (≥1 cross-community edge exists).

    Used to trigger hierarchy build on already-enriched projects that were
    indexed before hierarchy support was added.

    The cross-community-edge gate is essential: build_hierarchy forms level-2 from
    cross-community CALLS/IMPORTS edges, so a graph with none can NEVER produce a
    level. Without this gate such projects (mutually-disconnected communities —
    e.g. a thin federation root, or service repos with no resolved inter-community
    calls) would be flagged every sweep, re-attempting a futile build + re-enrich
    indefinitely — perpetual GPU/CPU churn that keeps the GPU warm.
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
            if not (max_level == 1 and n_level1 >= 5):
                return False
            # Only attempt a build that can actually yield a level.
            return gs.has_cross_community_edges()
        finally:
            gs.close()
    except Exception:
        return False


def _project_needs_hierarchy_enrich(project_path: str) -> bool:
    """Return True if any level ≥2 community exists with an unenriched (NULL) title.

    Scans ALL hierarchy levels (not just level-2) so a project like astro-project
    where level-3 is 0% enriched still triggers the self-heal path even after
    level-2 is partially enriched.
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
            if max_level <= 1:
                return False
            for lvl in range(2, max_level + 1):
                comms = gs.get_communities(level=lvl)
                if any(not c.title and (c.node_count or 0) >= 2 for c in comms):
                    return True
            return False
        finally:
            gs.close()
    except Exception:
        return False


def _project_needs_community_enrich(project_path: str) -> bool:
    """Return True if any level-1 community (node_count ≥ 2) has no title yet.

    Used by the KB sweep to detect when L1 enrichment has not converged —
    a prerequisite for L2/L3 parents to synthesise accurate summaries.
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
            comms = gs.get_communities(level=1, min_node_count=2)
            return any(not c.title for c in comms)
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

    needs_h = _project_needs_hierarchy(pp)
    needs_h_enrich = _project_needs_hierarchy_enrich(pp)
    kb_incomplete = _project_kb_incomplete(pp)
    if not force and not _project_is_fresh(pp) and not needs_h and not needs_h_enrich and not kb_incomplete:
        log.info("auto_pipeline[%s]: already enriched — skipping", root.name)
        return {"status": "skipped", "reason": "already_enriched", "project_path": pp}

    log.info("auto_pipeline[%s]: starting full KB build", root.name)
    steps: list[dict[str, Any]] = []
    _record_event(pp, "running")

    # ── Step 0: Re-detect communities when graph has edges but 0 communities ─
    # Gap A: payment-gateway had 370 edges and 0 communities because Leiden
    # never ran (or communities were wiped) while the graph was non-empty.
    # Without this step, handle_pipeline's enrich pass finds nothing to enrich
    # and the project stays permanently stuck at 0% enrichment.
    # Definitions-only graphs (edges==0) are exempt — they correctly have 0 communities.
    try:
        from opencode_search.config import get_project_graph_db_path
        from opencode_search.graph.community import CommunityDetector
        from opencode_search.graph.storage import GraphStorage as _GS0
        _db0 = get_project_graph_db_path(pp)
        if Path(_db0).exists():
            _gs0 = _GS0(_db0)
            _gs0.open()
            try:
                _n_comms0 = len(_gs0.get_communities())
                if _n_comms0 == 0:
                    _has_edges = _gs0._db().execute("SELECT 1 FROM edges LIMIT 1").fetchone()
                    if _has_edges:
                        log.info(
                            "auto_pipeline[%s]: 0 communities with edges present — re-running Leiden",
                            root.name,
                        )
                        _n_detected = await asyncio.to_thread(
                            lambda gs=_gs0: len(CommunityDetector().detect_communities(gs))
                        )
                        steps.append({
                            "step": "community_redetect",
                            "status": "ok",
                            "communities_detected": _n_detected,
                        })
                        log.info(
                            "auto_pipeline[%s]: Leiden re-run complete (%d communities)",
                            root.name, _n_detected,
                        )
                    else:
                        steps.append({"step": "community_redetect", "status": "skipped", "reason": "no_edges"})
                else:
                    steps.append({"step": "community_redetect", "status": "skipped", "reason": "already_detected"})
            finally:
                _gs0.close()
    except Exception as exc:
        log.warning("auto_pipeline[%s]: community_redetect failed: %s", root.name, exc)
        steps.append({"step": "community_redetect", "status": "error", "error": str(exc)})

    # ── Step 1: Enrich + Wiki + Ingest ──────────────────────────────────────
    # Use a high cap so every level-1 community is enriched on the first run.
    # 10_000 is effectively unbounded for any real project (astro has 1761 L1).
    # Previously 200, which left most communities unenriched and relied on the
    # incremental file-watcher to backfill — making KB quality non-deterministic.
    try:
        from opencode_search.handlers._pipeline import handle_pipeline
        pipeline_result = await handle_pipeline(
            project_path=pp,
            enrich_max_communities=10_000,
            wiki_max_communities=10_000,
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
    # Runs when: hierarchy not yet built (max_level==1) OR hierarchy exists but
    # level-2+ communities are unenriched (title=NULL) — catches interrupted
    # enrichment runs and hierarchy rebuilds that didn't re-enrich.
    try:
        _needs_h = _project_needs_hierarchy(pp)
        _needs_h_enrich = _project_needs_hierarchy_enrich(pp)
        if _needs_h or _needs_h_enrich:
            levels_built = 0
            if _needs_h:
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
                log.info("auto_pipeline[%s]: hierarchy built (%d levels)", root.name, levels_built)
            from opencode_search.handlers._enrichment import handle_enrich_hierarchy
            h_result = await handle_enrich_hierarchy(project_path=pp)
            steps.append({
                "step": "hierarchy",
                "status": "ok",
                "levels_built": levels_built,
                "enriched": h_result.get("enriched", 0),
            })
            log.info(
                "auto_pipeline[%s]: hierarchy enrich complete (enriched=%d)",
                root.name, h_result.get("enriched", 0),
            )
        else:
            steps.append({
                "step": "hierarchy",
                "status": "skipped",
                "reason": "hierarchy already built and enriched",
            })
    except Exception as exc:
        log.info("auto_pipeline[%s]: hierarchy skipped: %s", root.name, exc)
        steps.append({"step": "hierarchy", "status": "skipped", "reason": str(exc)})

    # ── Step 4: Warm service_mesh cache (post-build, so reads are instant) ───
    try:
        from opencode_search.handlers._service_mesh import handle_detect_service_mesh
        await handle_detect_service_mesh(project_path=pp, force=True)
        steps.append({"step": "service_mesh_warm", "status": "ok"})
        log.info("auto_pipeline[%s]: service_mesh cache warmed", root.name)
    except Exception as exc:
        log.debug("auto_pipeline[%s]: service_mesh warm skipped: %s", root.name, exc)
        steps.append({"step": "service_mesh_warm", "status": "skipped", "reason": str(exc)})

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

    # ── KB-level refresh (debounced by _KB_REFRESH_COOLDOWN_S) ──────────────
    # service_mesh cache: invalidate immediately so the next read recomputes the
    # now-fast bounded scan; re-warm eagerly if cooldown has passed.
    try:
        from opencode_search.handlers._service_mesh import (
            handle_detect_service_mesh,
            invalidate_service_mesh_cache,
        )
        invalidate_service_mesh_cache(pp)
        log.info("incremental_enrich[%s]: service_mesh cache invalidated", root.name)
        try:
            from opencode_search.handlers._answer_cache import invalidate_answers
            invalidate_answers(pp)
            log.debug("incremental_enrich[%s]: answer cache invalidated", root.name)
        except Exception as _ac_exc:
            log.debug("incremental_enrich[%s]: answer cache invalidation failed: %s", root.name, _ac_exc)

        if _should_schedule_kb_refresh(pp):
            _KB_REFRESH_IN_FLIGHT.add(pp)
            try:
                import time as _t
                await handle_detect_service_mesh(project_path=pp, force=True)
                _KB_REFRESH_LAST[pp] = _t.monotonic()
                log.info("incremental_enrich[%s]: service_mesh re-warmed", root.name)
            finally:
                _KB_REFRESH_IN_FLIGHT.discard(pp)
        else:
            log.debug(
                "incremental_enrich[%s]: service_mesh re-warm skipped "
                "(cooldown or in-flight)",
                root.name,
            )
    except Exception as exc:
        log.debug("incremental_enrich[%s]: service_mesh refresh failed: %s", root.name, exc)


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
