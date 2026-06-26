#!/usr/bin/env python3
"""Sweep: remove ose-docgen-generated docs/ from all repos under specified roots.

Finds every docs/_meta/provenance.json marker and calls clean_generated() on
that docs/ tree. Human-authored docs are preserved byte-for-byte. Idempotent.

Usage:
    python scripts/sweep_docgen.py [--root PATH ...] [--dry-run]

    --root PATH   One or more directory roots to search (repeatable).
                  Falls back to OSE_SWEEP_ROOTS env var (colon-separated), then cwd.

Examples:
    python scripts/sweep_docgen.py --root ~/git/github.com --root ~/go/src/github.com
    OSE_SWEEP_ROOTS=/path/to/repos python scripts/sweep_docgen.py --dry-run
    python scripts/sweep_docgen.py --dry-run  # sweeps current directory
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Inject vendor/docgen/src so the script works without pip-install.
_HERE = Path(__file__).resolve().parent.parent
_VENDOR = _HERE / "vendor" / "docgen" / "src"
if _VENDOR.exists() and str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))

_DOCS_DIR_NAME = os.environ.get("OSE_DOCGEN_DIR", "docs")
_META_MARKER = "_meta/provenance.json"


def _resolve_roots(cli_roots: list[str]) -> list[Path]:
    if cli_roots:
        return [Path(r).expanduser().resolve() for r in cli_roots]
    env = os.environ.get("OSE_SWEEP_ROOTS", "")
    if env:
        return [Path(r).expanduser().resolve() for r in env.split(":") if r.strip()]
    return [Path.cwd()]


def _find_generated_docs_dirs(roots: list[Path]) -> list[Path]:
    found: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        for marker in root.rglob(f"{_DOCS_DIR_NAME}/{_META_MARKER}"):
            docs_dir = marker.parent.parent
            found.append(docs_dir)
    return sorted(set(found))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be removed without deleting anything.")
    parser.add_argument("--root", dest="roots", metavar="PATH", action="append", default=[],
                        help="Root directory to search (repeatable). Default: OSE_SWEEP_ROOTS env or cwd.")
    args = parser.parse_args()

    try:
        from ose_docgen.cleanup import clean_generated
    except ImportError:
        print("ERROR: vendor/docgen/src not found — run from the opencode-search-engine root.")
        sys.exit(1)

    roots = _resolve_roots(args.roots)
    docs_dirs = _find_generated_docs_dirs(roots)
    if not docs_dirs:
        print("No generated docs trees found — nothing to sweep.")
        return

    total_removed = 0
    total_preserved = 0
    for docs_dir in docs_dirs:
        if args.dry_run:
            # Count what would be removed without touching anything.
            from ose_docgen.provenance import classify
            removed = []
            preserved = []
            for f in sorted(docs_dir.rglob("*")):
                if not f.is_file():
                    continue
                rel = f.relative_to(docs_dir)
                if "_meta" in rel.parts or classify(f) == "generated":
                    removed.append(str(rel))
                else:
                    preserved.append(str(rel))
            print(f"[dry-run] {docs_dir}: would remove {len(removed)}, preserve {len(preserved)}")
            for r in removed[:5]:
                print(f"    - {r}")
            if len(removed) > 5:
                print(f"    ... and {len(removed) - 5} more")
            total_removed += len(removed)
            total_preserved += len(preserved)
        else:
            result = clean_generated(docs_dir)
            r, p = len(result["removed"]), len(result["preserved"])
            print(f"[sweep] {docs_dir}: removed={r} preserved={p}")
            total_removed += r
            total_preserved += p

    action = "would remove" if args.dry_run else "removed"
    print(f"\nTotal: {action} {total_removed} files, preserved {total_preserved} files.")


if __name__ == "__main__":
    main()
