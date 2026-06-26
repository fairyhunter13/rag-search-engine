"""OKF v0.1 generator — standalone, zero OSE input.

generate(project_path, out_dir=None) → dict
  Writes an OKF v0.1 markdown bundle to out_dir (default: <project>/docs/okf/).
  All files carry okf_version: "0.1" in YAML frontmatter.
  Entry point: index.md + log.md + one fragment per top-level module.

No opencode_search import. No graph.db. GPU-free, daemon-free.
"""
from __future__ import annotations

import datetime
from pathlib import Path

from okf.repo_parser import repo_summary

OKF_VERSION = "0.1"
_FRAG_TYPES = ("architecture", "component", "pattern", "decision")


def _frontmatter(frag_type: str, title: str) -> str:
    return (
        f"---\nokf_version: \"{OKF_VERSION}\"\ntype: {frag_type}\n"
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
    """Generate OKF v0.1 bundle for project_path.

    Args:
        project_path: Root of the repo to document.
        out_dir: Output directory (default: <project_path>/docs/okf/).

    Returns:
        dict with keys: written, skipped, version, project.
    """
    root = Path(project_path).resolve()
    if out_dir is None:
        out_dir = root / "docs" / "okf"
    out = Path(out_dir)

    summary = repo_summary(root)
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")

    written: list[str] = []
    skipped: list[str] = []

    def record(path: Path, content: str) -> None:
        status = _write(path, content)
        (written if status == "written" else skipped).append(str(path.relative_to(out)))

    # index.md
    modules_list = "\n".join(f"- [{m}](fragment_{m}.md)" for m in summary["top_modules"])
    lang_list = ", ".join(
        f"{lang} ({count})" for lang, count in
        sorted(summary["languages"].items(), key=lambda x: -x[1])
    )
    index_body = (
        f"# {summary['name']} — OKF Index\n\n"
        f"Generated: {now}\n\n"
        f"## Overview\n\n"
        f"| Files | {summary['file_count']} |\n"
        f"| Languages | {lang_list or '—'} |\n\n"
        f"## Fragments\n\n{modules_list}\n"
    )
    record(out / "index.md", _frontmatter("architecture", f"{summary['name']} — Index") + index_body)

    # log.md
    log_body = f"# Change Log\n\n| Date | Change |\n|------|--------|\n| {now} | Initial OKF bundle generated |\n"
    record(out / "log.md", _frontmatter("decision", "Change Log") + log_body)

    # one fragment per top-level module
    for mod in summary["top_modules"]:
        body = (
            f"# {mod.replace('_', ' ').title()}\n\n"
            f"**Module**: `{mod}`\n\n"
            f"Top-level module in `{summary['name']}`. "
            f"See source under `{mod}/` for implementation details.\n"
        )
        record(out / f"fragment_{mod}.md", _frontmatter("component", mod.replace("_", " ").title()) + body)

    return {
        "written": written,
        "skipped": skipped,
        "version": OKF_VERSION,
        "project": summary["name"],
    }
