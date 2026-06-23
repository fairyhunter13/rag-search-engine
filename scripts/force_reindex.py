"""One-time cold rebuild: re-embed all projects (cAST headers) + re-enrich (Phase 4 fixes).

Usage:
    .venv/bin/python scripts/force_reindex.py [--dry-run] [--enrich-only]

Requires daemon sweeps to be paused first:
    curl -s -X POST http://127.0.0.1:8765/api/sweeps/pause

Rebuilds in dependency order:
  1. All non-federation standalone projects (OSE, payment-gateway, astro members)
  2. The federation root (astro-project) last — its L3/BPRE depend on member enrichment
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# Ensure the src/ package is on the path when run via scripts/
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def _real_projects() -> list[tuple[str, bool]]:
    """Return (path, is_federation_root) for all enabled non-tmp projects."""
    from opencode_search.core.registry import list_projects
    from opencode_search.daemon.federation import expand_federation
    projects = []
    for p in list_projects():
        if not p.enabled:
            continue
        if "ocs-test-dirs" in p.path:
            continue
        is_root = len(expand_federation(p.path)) >= 2
        projects.append((p.path, is_root))
    # Members first, roots last
    projects.sort(key=lambda x: x[1])
    return projects


def rebuild(path: str, enrich_only: bool = False, force_enrich: bool = False) -> None:
    from opencode_search.core.config import project_graph_db
    from opencode_search.daemon.sweeps import _enrich_project, _index_project
    from opencode_search.embed.embedder import get_embedder
    from opencode_search.graph.community import detect_communities
    from opencode_search.graph.store import GraphStore
    tag = Path(path).name
    if not enrich_only:
        print(f"[{tag}] embed ...", flush=True)
        t0 = time.time()
        _index_project(path)
        print(f"[{tag}] embed done in {time.time()-t0:.0f}s", flush=True)
    else:
        emb = get_embedder()
        emb.warmup()  # ensure CUDA EP is bound before enrichment
    if force_enrich:
        # Clear L1+ then re-detect from existing symbols/edges so _enrich_project
        # sees a fresh community set built with Phase 4 k-core logic.
        # Note: _enrich_project does NOT call detect_communities — it works on
        # existing L1 rows. So we must re-detect explicitly after clearing.
        gdb = project_graph_db(path)
        if gdb.exists():
            gs = GraphStore(gdb)
            try:
                gs._con.execute("DELETE FROM communities WHERE level>=1")
                gs.commit()
                n_spine = gs._con.execute("SELECT COUNT(*) FROM communities").fetchone()[0]
                n_syms = gs._con.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
                if n_syms:
                    detect_communities(gs)
                    n_l1 = gs._con.execute(
                        "SELECT COUNT(*) FROM communities WHERE level=1"
                    ).fetchone()[0]
                    print(
                        f"[{tag}] re-detected {n_l1} L1 communities "
                        f"({n_spine} spine nodes, {n_syms} symbols)",
                        flush=True,
                    )
                else:
                    print(f"[{tag}] no symbols — skipping detect_communities", flush=True)
            finally:
                gs.close()
    print(f"[{tag}] enrich ...", flush=True)
    t0 = time.time()
    _enrich_project(path)
    print(f"[{tag}] enrich done in {time.time()-t0:.0f}s", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--enrich-only", action="store_true",
                    help="Skip re-embedding; only re-run _enrich_project")
    ap.add_argument("--force-enrich", action="store_true",
                    help="Clear L1+ communities before enrichment (forces full re-enrich)")
    ap.add_argument("--project", metavar="SUBSTR", default="",
                    help="Only rebuild projects whose path contains SUBSTR")
    args = ap.parse_args()

    projects = _real_projects()
    if args.project:
        projects = [(p, r) for p, r in projects if args.project in p]
    print(f"Projects to rebuild: {len(projects)}", flush=True)
    for path, is_root in projects:
        role = "root" if is_root else "member"
        print(f"  [{role}] {Path(path).name} → {path}", flush=True)
    if args.dry_run:
        print("--dry-run: exiting without rebuilding", flush=True)
        return

    for path, _is_root in projects:
        try:
            rebuild(path, enrich_only=args.enrich_only, force_enrich=args.force_enrich)
        except Exception as exc:
            print(f"[{Path(path).name}] ERROR: {exc}", flush=True)

    print("Rebuild complete. Reload the daemon:", flush=True)
    print("  curl -s -X POST http://127.0.0.1:8765/api/reload", flush=True)


if __name__ == "__main__":
    main()
