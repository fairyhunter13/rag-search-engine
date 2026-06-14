"""Project pattern detection: languages, frameworks, dependencies."""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from opencode_search.index.discover import detect_language, iter_files

_KNOWN: dict[str, str] = {
    "fastapi": "FastAPI", "flask": "Flask", "django": "Django", "starlette": "Starlette",
    "react": "React", "vue": "Vue", "angular": "Angular", "svelte": "Svelte",
    "next": "Next.js", "nuxt": "Nuxt", "express": "Express",
    "torch": "PyTorch", "tensorflow": "TensorFlow", "keras": "Keras",
    "sqlalchemy": "SQLAlchemy", "prisma": "Prisma", "mongoose": "Mongoose",
    "pytest": "pytest", "jest": "Jest", "spring": "Spring Boot",
    "gin": "Gin", "echo": "Echo", "axum": "Axum",
}


def _llm_frameworks(deps: list[str]) -> list[str]:
    """Map well-known dependency names to framework labels (no LLM)."""
    out = set()
    for d in deps:
        key = d.lower().split("/")[-1].split("-")[0].split("_")[0]
        if key in _KNOWN:
            out.add(_KNOWN[key])
    return sorted(out)


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
