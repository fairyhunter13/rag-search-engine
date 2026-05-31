"""LLM-powered pattern analysis handler.

Complements the fast heuristic detector in _graph.py with deep LLM analysis:
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


def _build_analysis_prompt(
    heuristic: dict[str, Any],
    samples: list[tuple[str, str]],
) -> str:
    lang = heuristic.get("conventions", {}).get("language", "unknown")
    frameworks = ", ".join(heuristic.get("key_frameworks", [])) or "none detected"
    arch_hint = heuristic.get("architecture", "unknown")
    manifest_manager = heuristic.get("dependencies", {}).get("manager", "unknown")

    files_text = "\n\n".join(
        f"--- {rel} ---\n{content}" for rel, content in samples
    )

    return (
        f"You are an expert software architect analysing a {lang} project.\n"
        f"Heuristic scan: frameworks=[{frameworks}], architecture={arch_hint}, "
        f"package_manager={manifest_manager}.\n\n"
        "Sample source files:\n\n"
        f"{files_text}\n\n"
        "Analyse these files and respond with a JSON object (no markdown fences) with EXACTLY these keys:\n"
        "{\n"
        '  "architecture_description": "one paragraph describing the overall architecture",\n'
        '  "coding_patterns": ["list of detected patterns, e.g. repository pattern, functional options, builder"],\n'
        '  "naming_conventions": "description of actual naming style observed in code",\n'
        '  "error_handling_style": "description of error handling approach observed",\n'
        '  "test_approach": "description of how tests are structured",\n'
        '  "code_quality_signals": ["positive signals", "potential concerns"],\n'
        '  "primary_language": "the single dominant language",\n'
        '  "key_abstractions": ["top 3-5 key abstractions/modules visible in code"],\n'
        '  "confidence": "high|medium|low based on sample quality"\n'
        "}\n"
        "Base your response ONLY on what you can actually observe in the code. "
        "If you cannot determine something, use null for that field."
    )


def _parse_llm_json(raw: str) -> dict[str, Any]:
    """Extract JSON from LLM response, handling prose wrapping."""
    raw = raw.strip()
    # Strip markdown fences if present
    if raw.startswith("```"):
        lines = raw.splitlines()
        raw = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    # Find first { ... } block
    start = raw.find("{")
    end = raw.rfind("}") + 1
    if start >= 0 and end > start:
        try:
            return json.loads(raw[start:end])
        except json.JSONDecodeError:
            pass
    return {"raw_response": raw, "confidence": "low"}


async def handle_analyze_patterns_llm(project_path: str, force: bool = False) -> dict[str, Any]:
    """Run LLM-based pattern analysis and cache the result.

    Samples real source files, sends them to the configured local LLM
    (OPENCODE_LLM_PROVIDER), and stores the structured result in the
    project's index directory. Future calls to handle_detect_patterns()
    automatically merge this cached analysis.

    Args:
        project_path: Absolute path to the project root.
        force: Re-run even if a cached result exists.

    Returns a dict with 'status', 'llm_analysis', and 'cached_at'.
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

    # Get heuristic baseline first (fast)
    from opencode_search.handlers._graph import handle_detect_patterns as _heuristic
    heuristic = await _heuristic(project_path=project_path)

    # Sample source files
    samples = await asyncio.to_thread(_sample_source_files, root)
    if not samples:
        return {
            "status": "error",
            "error": "No source files found to analyse",
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

    # Call LLM
    prompt = _build_analysis_prompt(heuristic, samples)
    try:
        raw = await asyncio.to_thread(
            llm.chat,
            [{"role": "user", "content": prompt}],
            max_tokens=1024,
        )
    except Exception as exc:
        return {"status": "error", "error": f"LLM call failed: {exc}", "project_path": str(root)}

    llm_result = _parse_llm_json(raw)

    # Cache
    from datetime import datetime, timezone
    cached_at = datetime.now(timezone.utc).isoformat()
    cache_data = {
        "project_path": str(root),
        "cached_at": cached_at,
        "files_sampled": len(samples),
        "llm_analysis": llm_result,
    }
    await asyncio.to_thread(_save_patterns_cache, project_path, cache_data)
    log.info("patterns_llm[%s]: cached LLM analysis (%d files sampled)", root.name, len(samples))

    return {
        "status": "ok",
        "project_path": str(root),
        "files_sampled": len(samples),
        "llm_analysis": llm_result,
        "cached_at": cached_at,
    }
