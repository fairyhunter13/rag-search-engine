"""Project pattern detection: languages, frameworks, dependencies."""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from rag_search.index.discover import detect_language, iter_files

_FRAMEWORK_CACHE: dict[tuple, list[str]] = {}


def _llm_frameworks(deps: list[str]) -> list[str]:
    """Return framework labels via DeepSeek LLM; falls back to raw dep names when no key."""
    key = tuple(sorted(set(deps)))
    if key in _FRAMEWORK_CACHE:
        return _FRAMEWORK_CACHE[key]
    try:
        from rag_search.graph.llm import deepseek_chat, deepseek_key
        if not deepseek_key():
            raise ValueError("no key")
        sample = sorted({d.split("/")[-1].split("-")[0] for d in deps if d})[:40]
        raw = deepseek_chat(
            "List the software frameworks (React, Django, Gin, etc.) used by a project with "
            "these dependencies. Return ONLY a JSON array of short framework names.\n\n"
            "Dependencies:\n" + "\n".join(sample),
            max_tokens=200,
        ).strip().replace("```json", "").replace("```", "").strip()
        result = sorted({s.strip() for s in json.loads(raw) if isinstance(s, str) and s.strip()})
    except Exception:
        result = sorted({d.split("/")[-1].split("-")[0] for d in deps if d})
    _FRAMEWORK_CACHE[key] = result
    return result


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
    """Return {languages, frameworks, dependencies, source_file_count} from a project tree."""
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
        "source_file_count": sum(lang_counts.values()),
    }
