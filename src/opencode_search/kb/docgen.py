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


def run_docgen(project_path: str) -> None:
    """Generate or update the docs/ C4×Diátaxis tree for project_path.

    Deterministic ($0) unless OSE_DOCGEN_LLM=1. Writes into <project>/docs/.
    Never raises — all errors are logged.
    """
    if os.environ.get("OSE_DOCGEN", "1") == "0":
        return
    if not _inject_vendor():
        log.warning("docgen: vendor/docgen/src not found at %s — skipping", _VENDOR_SRC)
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
