"""Project pattern detection: languages, frameworks, dependencies."""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from opencode_search.index.discover import detect_language, iter_files

_FW: dict[str, str] = {
    "django": "Django", "flask": "Flask", "fastapi": "FastAPI",
    "react": "React", "vue": "Vue", "astro": "Astro",
    "express": "Express", "next": "Next.js", "nextjs": "Next.js",
    "gin": "Gin", "echo": "Echo", "spring": "Spring",
    "rails": "Rails", "sinatra": "Sinatra",
}


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

    frameworks = sorted({_FW[d.split("[")[0].strip().lower()]
                         for d in deps if d.split("[")[0].strip().lower() in _FW})
    return {
        "languages": dict(lang_counts.most_common(10)),
        "frameworks": frameworks,
        "dependencies": sorted(set(deps[:60])),
        "file_count": sum(lang_counts.values()),
    }
