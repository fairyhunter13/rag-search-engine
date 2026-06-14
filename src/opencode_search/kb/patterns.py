"""Project pattern detection: languages, frameworks, dependencies."""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from opencode_search.graph.llm import chat
from opencode_search.index.discover import detect_language, iter_files


def _llm_frameworks(deps: list[str]) -> list[str]:
    """Name frameworks implied by the dependency list via LLM (no static map)."""
    if not deps:
        return []
    try:
        raw = chat(
            f"Dependencies: {', '.join(deps[:30])}\n"
            "List only the major frameworks these packages represent. One per line. No explanation.",
        )
        return sorted({
            ln.strip().lstrip("-* •0123456789.").strip()
            for ln in raw.replace(",", "\n").splitlines()
            if ln.strip().lstrip("-* •0123456789.").strip()
        })
    except Exception:
        return []


def _parse_deps(path: Path) -> list[str]:
    if path.name == "pyproject.toml":
        try:
            import tomllib
            data = tomllib.loads(path.read_text())
            return list(data.get("project", {}).get("dependencies", []))
        except Exception:
            return []
    if path.name == "package.json":
        try:
            data = json.loads(path.read_text())
            return list(data.get("dependencies", {}).keys()) + \
                   list(data.get("devDependencies", {}).keys())
        except Exception:
            return []
    if path.name in ("go.mod", "Cargo.toml", "requirements.txt"):
        try:
            return [ln.split()[0] for ln in path.read_text().splitlines() if ln.strip()]
        except Exception:
            return []
    return []


def detect_patterns(project_root: Path) -> dict:
    """Return {languages, frameworks, dependencies, file_count} from a project tree."""
    lang_counts: Counter[str] = Counter()
    deps: list[str] = []

    for p in iter_files(project_root, federation_mode=True):
        lang = detect_language(p)
        if lang != "unknown":
            lang_counts[lang] += 1
        if p.name in ("pyproject.toml", "package.json", "go.mod", "Cargo.toml", "requirements.txt"):
            deps.extend(_parse_deps(p))

    frameworks = _llm_frameworks(deps)
    return {
        "languages": dict(lang_counts.most_common(10)),
        "frameworks": frameworks,
        "dependencies": sorted(set(deps[:60])),
        "file_count": sum(lang_counts.values()),
    }
