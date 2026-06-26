"""Thin OSE adapter: run OKF v0.1 generator for a project.

Injects vendor/okf/src so OSE calls the tool without import coupling.
Kill-switch: OSE_OKF=0 skips (default=1, $0/deterministic always).
Output dir: <project>/docs/okf/ (override with OSE_OKF_DIR).
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

log = logging.getLogger(__name__)

_VENDOR_SRC = Path(__file__).parent.parent.parent.parent / "vendor" / "okf" / "src"


def _inject_vendor() -> bool:
    if not _VENDOR_SRC.exists():
        return False
    if str(_VENDOR_SRC) not in sys.path:
        sys.path.insert(0, str(_VENDOR_SRC))
    return True


def run_okf(project_path: str) -> None:
    """Generate or update the OKF v0.1 bundle for project_path.

    Deterministic, $0. Writes into <project>/docs/okf/.
    Federation members are skipped (OKF is root-only). Never raises.
    """
    if os.environ.get("OSE_OKF", "1") == "0":
        return
    if not _inject_vendor():
        log.warning("okf: vendor/okf/src not found at %s — skipping", _VENDOR_SRC)
        return
    try:
        from okf.generate import generate  # type: ignore[import]

        out_dir = str(
            Path(project_path) / os.environ.get("OSE_OKF_DIR", "docs/okf")
        )
        result = generate(project_path=project_path, out_dir=out_dir)
        log.info(
            "okf %s: written=%d skipped=%d",
            project_path,
            len(result.get("written", [])),
            len(result.get("skipped", [])),
        )
    except Exception as exc:
        log.error("okf failed for %s: %s", project_path, exc, exc_info=True)
