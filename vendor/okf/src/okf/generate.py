"""OKF v0.1 generator -- LLM-native via claude -p.

generate(project_path, out_dir=None) -> dict
Kill-switch: OSE_OKF=0 -> returns empty dict immediately (no output).
No deterministic skeleton. claude -p reads repo source, identifies semantic
concepts, infers type, synthesizes bodies with [code: file:line] citations.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

OKF_VERSION = "0.1"
MODEL_HAIKU = "claude-haiku-4-5"
MODEL_SONNET = "claude-sonnet-4-6"
_TIMEOUT = 180

_raw_profiles = os.environ.get(
    "OSE_OKF_CLAUDE_PROFILES",
    f"{os.path.expanduser('~/.claude')},{os.path.expanduser('~/.claude-account1')}",
)
_PROFILES: list[str] = [p.strip() for p in _raw_profiles.split(",") if p.strip()]


def _claude() -> str:
    c = shutil.which("claude")
    if not c:
        raise RuntimeError("'claude' CLI not found in PATH")
    return c


def _subprocess_env(config_dir: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    env["CLAUDE_CONFIG_DIR"] = config_dir
    env["CLAUDE_CODE_SAFE_MODE"] = "1"
    return env


def _pick_profile() -> str | None:
    return _PROFILES[0] if _PROFILES else None


_last_run_stderr: str = ""
_last_run_returncode: int = 0


def _run_claude(prompt: str, model: str, add_dirs: list[str], profile: str) -> str | None:
    global _last_run_stderr, _last_run_returncode
    cmd = [_claude(), "-p", prompt, "--model", model,
           "--output-format", "json", "--allow-dangerously-skip-permissions",
           "--allowedTools", "Read,Bash"]
    for d in add_dirs:
        cmd += ["--add-dir", d]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                           timeout=_TIMEOUT, env=_subprocess_env(profile))
        _last_run_stderr = r.stderr
        _last_run_returncode = r.returncode
        if r.returncode != 0:
            return None
        data = json.loads(r.stdout)
        if isinstance(data, list):
            for b in data:
                if isinstance(b, dict) and b.get("type") == "text":
                    return b.get("text", "")
        if isinstance(data, dict):
            return data.get("result") or data.get("text") or ""
        return str(data) or None
    except Exception as e:
        _last_run_stderr = str(e)
        _last_run_returncode = -1
        return None


def _frontmatter(concept_type: str, title: str) -> str:
    return (
        f"---\nokf_version: \"{OKF_VERSION}\"\ntype: {concept_type}\n"
        f"title: \"{title}\"\ngenerated: true\n---\n\n"
    )


def _write(path: Path, content: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if not _is_generated(path):
            return "skipped"
        if path.read_text(encoding="utf-8") == content:
            return "skipped"
    path.write_text(content, encoding="utf-8")
    return "written"


def _is_generated(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8")
        return "okf_version:" in text and "generated: true" in text
    except OSError:
        return False


def generate(
    project_path: str | Path,
    out_dir: str | Path | None = None,
) -> dict:
    """Generate OKF v0.1 bundle via claude -p. Kill-switch: OSE_OKF=0 -> no output."""
    if os.environ.get("OSE_OKF", "1") == "0":
        return {"written": [], "skipped": [], "version": OKF_VERSION, "mode": "off"}

    root = Path(project_path).resolve()
    if out_dir is None:
        out_dir = root / "docs" / "okf"
    out = Path(out_dir)

    profile = _pick_profile()
    if not profile:
        return {"written": [], "skipped": [], "errors": ["no_profile"], "version": OKF_VERSION}

    # Phase 1: discover concepts via LLM
    prompt_discover = (
        "Analyze this repository and identify its key semantic concepts for an OKF v0.1 knowledge bundle. "
        "Output ONLY valid JSON with key 'concepts' (list of objects). "
        "Each concept: 'name' (semantic kebab-case filename without .md, e.g. 'search-pipeline', 'gpu-inference'), "
        "'type' (open vocabulary: Module|Service|Command|Event|Policy|Process|DataModel|Endpoint|Invariant|Pattern|Pipeline|Protocol|Configuration), "
        "'title' (human-readable), 'description' (one sentence), "
        "'grounding_sources' (list of up to 5 repo-relative file paths). "
        "Include 5-15 concepts. Names must be semantic domain terms (e.g. 'search-pipeline'), never numeric sequences. "
        "No /home/ or absolute paths.\n\n"
        "Analyze the repository structure and source files to identify the most important concepts."
    )
    text = _run_claude(prompt_discover, MODEL_SONNET, [str(root)], profile)
    concepts_data = _parse_json(text)
    if not concepts_data or "concepts" not in concepts_data:
        return {
            "written": [], "skipped": [], "errors": ["discover_failed"], "version": OKF_VERSION,
            "_debug": {"returncode": _last_run_returncode, "stderr": _last_run_stderr[:500],
                       "text": (text or "")[:200]},
        }

    concepts = concepts_data["concepts"]
    written: list[str] = []
    skipped: list[str] = []
    out.mkdir(parents=True, exist_ok=True)

    # Phase 2: write one page per concept
    for concept in concepts:
        name = concept.get("name", "").strip().replace(" ", "-").lower()
        if not name:
            continue
        ctype = concept.get("type", "Module")
        title = concept.get("title", name.replace("-", " ").title())
        srcs = ", ".join(concept.get("grounding_sources", [])[:5]) or "repository source"
        prompt_write = (
            f"Write the OKF v0.1 concept page for '{title}' (type: {ctype}). "
            f"Ground it in: {srcs}. Output ONLY the markdown body (no frontmatter). "
            "Include: definition, key responsibilities, important code references as [code: file:line], "
            "and cross-links to related concepts using markdown links. "
            "No /home/ or absolute paths. Be factual -- only what you can verify."
        )
        body = _run_claude(prompt_write, MODEL_HAIKU, [str(root)], profile)
        if not body or not body.strip():
            continue
        content = _frontmatter(ctype, title) + body.strip() + "\n"
        status = _write(out / f"{name}.md", content)
        (written if status == "written" else skipped).append(f"{name}.md")

    # index.md — OKF v0.1: reserved file, MUST NOT carry frontmatter (spec §3).
    concept_links = "\n".join(
        f"- [{c.get('title', c.get('name', ''))}](/{c.get('name', '').replace(' ', '-').lower()}.md)"
        for c in concepts if c.get("name")
    )
    index_content = (
        f"# {root.name} Knowledge Graph\n\n"
        + "This OKF v0.1 bundle maps the semantic concepts of this repository.\n\n"
        + "## Concepts\n\n" + concept_links + "\n"
    )
    s = _write(out / "index.md", index_content)
    (written if s == "written" else skipped).append("index.md")

    return {"written": written, "skipped": skipped, "version": OKF_VERSION, "project": root.name}


def _parse_json(text: str | None) -> dict | None:
    if not text:
        return None
    t = text.strip()
    if t.startswith("```"):
        t = t.split("```", 1)[1].lstrip("json").rsplit("```", 1)[0].strip()
    try:
        return json.loads(t)
    except Exception:
        return None
