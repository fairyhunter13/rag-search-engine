"""Thin OSE adapter: run ose-docgen generate() for a project.

Injects vendor/docgen/src so OSE calls the tool without import coupling.
Kill-switch: OSE_DOCGEN=0 skips (default=1, $0/deterministic unless OSE_DOCGEN_LLM=1).
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

log = logging.getLogger(__name__)

_VENDOR_SRC = (
    Path(__file__).parent.parent.parent.parent / "vendor" / "docgen" / "src"
)


def _inject_vendor() -> bool:
    if not _VENDOR_SRC.exists():
        return False
    if str(_VENDOR_SRC) not in sys.path:
        sys.path.insert(0, str(_VENDOR_SRC))
    return True


def _is_federation_member(project_path: str) -> bool:
    """True iff project_path appears in any enabled root's federation list (HR27)."""
    from opencode_search.core.registry import list_projects
    return any(
        e.enabled and project_path in (e.federation or [])
        for e in list_projects()
    )


def _cleanup_generated_docs(project_path: str) -> None:
    """Remove generated docs/ from a federation member (R5 self-heal). O(1) if no marker."""
    docs_dir = Path(project_path) / os.environ.get("OSE_DOCGEN_DIR", "docs")
    if not (docs_dir / "_meta" / "provenance.json").exists():
        return
    if not _inject_vendor():
        return
    try:
        from ose_docgen.cleanup import clean_generated  # type: ignore[import]
        result = clean_generated(docs_dir)
        log.info(
            "docgen cleanup %s: removed=%d preserved=%d",
            project_path, len(result["removed"]), len(result["preserved"]),
        )
    except Exception as exc:
        log.error("docgen cleanup failed for %s: %s", project_path, exc, exc_info=True)


def cleanup_member_docs() -> dict:
    """One-shot R5 remediation: clean generated docs/ from every federation member.

    Idempotent. Returns aggregate report {"members": [{path, removed, preserved}]}.
    """
    from opencode_search.core.registry import list_projects

    members: set[str] = set()
    for entry in list_projects():
        if entry.enabled and entry.federation:
            members.update(entry.federation)

    report: list[dict] = []
    for member in sorted(members):
        docs_dir = Path(member) / os.environ.get("OSE_DOCGEN_DIR", "docs")
        if not (docs_dir / "_meta" / "provenance.json").exists():
            continue
        if not _inject_vendor():
            break
        try:
            from ose_docgen.cleanup import clean_generated  # type: ignore[import]
            result = clean_generated(docs_dir)
            report.append({
                "path": member,
                "removed": len(result["removed"]),
                "preserved": len(result["preserved"]),
            })
        except Exception as exc:
            log.error("docgen cleanup %s: %s", member, exc)
            report.append({"path": member, "error": str(exc)})

    return {"members": report}


def run_docgen(project_path: str) -> None:
    """Generate or update the docs/ C4×Diátaxis tree for project_path.

    Deterministic ($0) unless OSE_DOCGEN_LLM=1. Writes into <project>/docs/.
    Federation members are cleaned (not generated) per HR27. Never raises.
    """
    if os.environ.get("OSE_DOCGEN", "1") == "0":
        return
    if not _inject_vendor():
        log.warning("docgen: vendor/docgen/src not found at %s — skipping", _VENDOR_SRC)
        return
    # R4 (HR27): federation members never own a generated docs/ — clean existing and return
    if _is_federation_member(project_path):
        _cleanup_generated_docs(project_path)
        return
    try:
        from ose_docgen.generate import generate  # type: ignore[import]

        from opencode_search.core.config import project_graph_db
        from opencode_search.daemon.federation import expand_federation

        gdb = project_graph_db(project_path)
        if not gdb.exists():
            return

        members = expand_federation(project_path)
        member_dbs = [
            project_graph_db(m) for m in members if m != project_path
        ]
        docs_dir = str(
            Path(project_path) / os.environ.get("OSE_DOCGEN_DIR", "docs")
        )
        llm = os.environ.get("OSE_DOCGEN_LLM", "0") == "1"

        result = generate(
            project_path=project_path,
            graph_db_path=gdb,
            member_db_paths=[str(p) for p in member_dbs if p.exists()],
            docs_dir=docs_dir,
            llm=llm,
        )
        log.info(
            "docgen %s: written=%d skipped=%d errors=%d",
            project_path,
            len(result.get("written", [])),
            len(result.get("skipped", [])),
            len(result.get("errors", [])),
        )
        if result.get("errors"):
            log.warning(
                "docgen errors for %s: %s", project_path, result["errors"][:3]
            )
    except Exception as exc:
        log.error("docgen failed for %s: %s", project_path, exc, exc_info=True)
