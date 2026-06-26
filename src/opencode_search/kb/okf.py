"""Thin OSE adapter: run OKF v0.1 generate() for a project.

Kill-switch: OSE_OKF=0 skips (default=1). LLM-native via claude -p.
Manual-trigger only — never wired into the auto-enrich sweep.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

log = logging.getLogger(__name__)

_VENDOR_SRC = (
    Path(__file__).parent.parent.parent.parent / "vendor" / "okf" / "src"
)


def _inject_vendor() -> bool:
    if not _VENDOR_SRC.exists():
        return False
    if str(_VENDOR_SRC) not in sys.path:
        sys.path.insert(0, str(_VENDOR_SRC))
    return True


def run_okf(project_path: str) -> dict:
    """Generate OKF v0.1 bundle for project_path. Kill-switch: OSE_OKF=0 → no output."""
    if os.environ.get("OSE_OKF", "1") == "0":
        return {"written": [], "skipped": [], "mode": "off"}
    if not _inject_vendor():
        log.warning("okf: vendor/okf/src not found at %s — skipping", _VENDOR_SRC)
        return {"written": [], "skipped": [], "errors": ["vendor_missing"]}
    try:
        from okf.generate import generate  # type: ignore[import]
        result = generate(project_path=project_path)
        log.info("okf %s: written=%d skipped=%d",
                 project_path, len(result.get("written", [])), len(result.get("skipped", [])))
        return result
    except Exception as exc:
        log.error("okf failed for %s: %s", project_path, exc, exc_info=True)
        return {"written": [], "skipped": [], "errors": [str(exc)]}
