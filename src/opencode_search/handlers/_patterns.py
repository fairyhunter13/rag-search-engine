"""LLM-powered deep pattern analysis handler.

Complements handle_detect_patterns in _graph.py with file-level LLM analysis:
- Reads representative source files
- Sends them to the project's configured local LLM
- Returns/caches a structured analysis covering architecture, idioms,
  naming conventions, error handling, test approach, and code quality signals

Cache: stored as <index_dir>/patterns_cache.json next to the wiki directory.
Invalidated when re-run. Merged into handle_detect_patterns output when present.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_CACHE_FILENAME = "patterns_cache.json"
_MAX_SAMPLE_FILES = 15
_MAX_BYTES_PER_FILE = 3_000
_MAX_TOTAL_BYTES = 30_000


def _get_cache_path(project_path: str) -> Path:
    from opencode_search.config import get_project_index_dir
    return get_project_index_dir(project_path) / _CACHE_FILENAME


def load_patterns_cache(project_path: str) -> dict[str, Any] | None:
    """Return the cached LLM pattern analysis, or None if not present."""
    cache_path = _get_cache_path(project_path)
    if not cache_path.exists():
        return None
    try:
        return json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_patterns_cache(project_path: str, data: dict[str, Any]) -> None:
    cache_path = _get_cache_path(project_path)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _sample_source_files(root: Path) -> list[tuple[str, str]]:
    """Return up to _MAX_SAMPLE_FILES (relative_path, content_snippet) pairs.

    Takes one source file per directory (stratified across the tree) so that
    federation/monorepo setups with many symlinked repos are represented.
    Prioritises primary language source files, skips generated and test files.
    """
    import os

    _SKIP_DIRS = {"vendor", "node_modules", ".git", ".venv", "venv",
                  "target", "dist", "build", "__pycache__"}
    _SKIP_NAME_PARTS = {"generated", "pb", "mock", "mocks"}
    _PRIMARY_EXTS = {".go", ".py", ".java", ".kt", ".ts", ".tsx", ".rs", ".rb",
                     ".cs", ".swift", ".cpp", ".c", ".scala"}

    def _is_ok(path: Path) -> bool:
        for part in path.parts:
            if part in _SKIP_NAME_PARTS:
                return False
        stem = path.stem.lower()
        return not ("test" in stem or "_test" in stem or "spec" in stem)

    # One file per directory walk — pick the first suitable source file in each dir
    selected: list[Path] = []

    for dirpath, dirnames, filenames in os.walk(root, followlinks=True, topdown=True):
        dirnames[:] = [
            d for d in sorted(dirnames)
            if d not in _SKIP_DIRS and not d.startswith(".")
        ]
        dp = Path(dirpath)
        # Pick the first suitable source file from this directory
        for fname in sorted(filenames):
            path = dp / fname
            if path.suffix.lower() not in _PRIMARY_EXTS:
                continue
            if not _is_ok(path):
                continue
            selected.append(path)
            break  # one per directory
        if len(selected) >= _MAX_SAMPLE_FILES:
            break

    samples: list[tuple[str, str]] = []
    total_bytes = 0
    for path in selected:
        if total_bytes >= _MAX_TOTAL_BYTES:
            break
        try:
            content = path.read_text(errors="replace")[: _MAX_BYTES_PER_FILE]
        except Exception:
            continue
        try:
            rel = str(path.relative_to(root))
        except ValueError:
            rel = str(path)
        samples.append((rel, content))
        total_bytes += len(content)

    return samples


def _parse_llm_json(raw: str) -> dict[str, Any]:
    """Extract JSON from LLM response, handling prose wrapping."""
    raw = raw.strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        raw = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    start = raw.find("{")
    end = raw.rfind("}") + 1
    if start >= 0 and end > start:
        try:
            return json.loads(raw[start:end])
        except json.JSONDecodeError:
            pass
    return {"raw_response": raw[:500], "confidence": "low"}


def _gather_exact_facts(root: Path, project_path: str) -> dict[str, Any]:
    """Step 2: Exact extraction using tree-sitter graph DB + manifest parsing.

    Returns precise, verifiable facts to ground the LLM synthesis:
    - Language file counts (from tree-sitter-aware discover.iter_files)
    - Dependency versions (from manifest parsing)
    - Graph statistics (node/edge/community counts)
    - Top symbols by call frequency
    - Module structure type
    """
    facts: dict[str, Any] = {}

    # Language counts (reuse heuristic detector — already tree-sitter-aware)
    try:
        from opencode_search.handlers._graph import (
            _count_languages_accurate,
            _detect_dependencies,
            _detect_module_structure,
        )
        facts["language_counts"] = _count_languages_accurate(root, project_path)
        deps = _detect_dependencies(root)
        facts["package_manager"] = deps.get("manager", "unknown")
        # Top pinned direct dependencies with versions
        direct = [p for p in deps.get("packages", []) if p.get("direct")][:20]
        facts["pinned_dependencies"] = {p["name"]: p["version"] for p in direct if p.get("version")}
        facts["manifest_files"] = deps.get("manifest_files", [])[:10]
        # Raw directory facts (no heuristic type label — LLM infers structure from these)
        mod_struct = _detect_module_structure(root)
        facts["top_packages"] = mod_struct.get("top_packages", [])
    except Exception:
        pass

    # Real parsed imports (tree-sitter facts, mapping-free)
    try:
        from opencode_search.handlers._graph import _load_external_imports
        facts["imports_in_use"] = [item["module"] for item in _load_external_imports(project_path)]
    except Exception:
        pass

    # Graph statistics from the code graph DB
    try:
        from pathlib import Path as _Path

        from opencode_search.config import get_project_graph_db_path
        from opencode_search.graph.storage import GraphStorage
        db_path = get_project_graph_db_path(project_path)
        if _Path(db_path).exists():
            gs = GraphStorage(db_path)
            gs.open()
            try:
                all_nodes = gs.all_nodes()
                all_edges = gs.all_edges()
                communities = gs.get_communities()
                facts["graph"] = {
                    "node_count": len(all_nodes),
                    "edge_count": len(all_edges),
                    "community_count": len(communities),
                    "enriched_communities": sum(1 for c in communities if c.title),
                    "languages_in_graph": list({n.language for n in all_nodes if n.language}),
                }
                # Top 10 most-called symbols (highest in-degree)
                from collections import Counter
                call_counts: Counter = Counter()
                for e in all_edges:
                    if e.kind == "CALLS":
                        call_counts[e.to_id] += 1
                top_ids = [nid for nid, _ in call_counts.most_common(10)]
                id_to_name = {n.id: n.qualified_name for n in all_nodes}
                facts["top_symbols"] = [id_to_name[nid] for nid in top_ids if nid in id_to_name]
            finally:
                gs.close()
    except Exception:
        pass

    return facts


async def handle_analyze_patterns_llm(project_path: str, force: bool = False) -> dict[str, Any]:
    """Run 3-step LLM-first pattern analysis and cache the result.

    Architecture:
        Step 1 — LLM Overview: sample real source files → LLM understands the
                 project at high level (architecture, tech stack, key patterns)
        Step 2 — Exact Extraction: tree-sitter graph stats + manifest parsing
                 → precise verifiable facts (versions, node counts, top symbols)
        Step 3 — LLM Synthesis: combine overview + exact facts → rich semantic
                 knowledge (architecture description, patterns, conventions, quality)

    Caches the result as patterns_cache.json next to the wiki directory.
    Future calls to handle_detect_patterns() auto-merge the cached analysis.

    Args:
        project_path: Absolute path to the project root.
        force: Re-run even if a cached result exists.
    """
    root = Path(project_path).expanduser().resolve()
    if not root.is_dir():
        return {"error": f"Not a directory: {project_path}"}

    if not force:
        cached = load_patterns_cache(project_path)
        if cached:
            return {
                "status": "cached",
                "llm_analysis": cached.get("llm_analysis"),
                "cached_at": cached.get("cached_at"),
                "project_path": str(root),
            }

    # Create LLM client
    try:
        from opencode_search.enricher.client import create_llm_client
        llm = create_llm_client()
    except Exception as exc:
        return {"status": "error", "error": f"LLM client init failed: {exc}", "project_path": str(root)}

    if llm is None:
        return {
            "status": "error",
            "error": (
                "No LLM provider configured. Set OPENCODE_LLM_PROVIDER "
                "(e.g. ollama, claude-code, anthropic) to enable LLM analysis."
            ),
            "project_path": str(root),
        }

    # ── Step 1: LLM Overview ────────────────────────────────────────────────
    samples = await asyncio.to_thread(_sample_source_files, root)
    if not samples:
        return {
            "status": "error",
            "error": "No source files found to analyse",
            "project_path": str(root),
        }

    log.info("patterns_llm[%s]: Step 1 — LLM overview (%d files)", root.name, len(samples))
    try:
        overview_raw = await asyncio.to_thread(llm.project_overview, samples)
    except Exception as exc:
        return {"status": "error", "error": f"LLM overview failed: {exc}", "project_path": str(root)}
    overview_result = _parse_llm_json(overview_raw)

    # ── Step 2: Exact Extraction ────────────────────────────────────────────
    log.info("patterns_llm[%s]: Step 2 — exact extraction (tree-sitter + manifests)", root.name)
    exact_facts = await asyncio.to_thread(_gather_exact_facts, root, project_path)

    # ── Step 3: LLM Synthesis ────────────────────────────────────────────────
    log.info("patterns_llm[%s]: Step 3 — LLM synthesis", root.name)
    try:
        synthesis_raw = await asyncio.to_thread(llm.project_synthesis, overview_raw, exact_facts)
    except Exception as exc:
        # Fall back to just the overview if synthesis fails
        log.warning("patterns_llm[%s]: synthesis failed, using overview only: %s", root.name, exc)
        synthesis_raw = overview_raw
    llm_result = _parse_llm_json(synthesis_raw)
    # Merge overview confidence if synthesis didn't set it
    if "confidence" not in llm_result:
        llm_result["confidence"] = overview_result.get("confidence", "medium")

    # Cache
    from datetime import datetime
    cached_at = datetime.now(UTC).isoformat()
    cache_data = {
        "project_path": str(root),
        "cached_at": cached_at,
        "files_sampled": len(samples),
        "steps": ["llm_overview", "exact_extraction", "llm_synthesis"],
        "llm_overview": overview_result,
        "exact_facts": exact_facts,
        "llm_analysis": llm_result,
    }
    await asyncio.to_thread(_save_patterns_cache, project_path, cache_data)
    log.info("patterns_llm[%s]: 3-step analysis cached (%d files)", root.name, len(samples))

    return {
        "status": "ok",
        "project_path": str(root),
        "files_sampled": len(samples),
        "steps_completed": ["llm_overview", "exact_extraction", "llm_synthesis"],
        "llm_analysis": llm_result,
        "cached_at": cached_at,
    }
