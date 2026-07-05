"""Deterministic modularity community detection on the symbol call graph."""
from __future__ import annotations

import os
import random
from collections import Counter

from rag_search.graph.store import GraphStore

# Bump when the detection algorithm changes so reconcile re-derives stale graphs.
ALGO_VERSION = "fg1"  # fastgreedy modularity + directory-group for edgeless, v1

# Seed igraph's RNG once so fastgreedy tie-breaks are byte-reproducible.
# community_fastgreedy is agglomerative-greedy (no stochastic step), but
# igraph does not doc-guarantee bit-identical output across builds without this.
random.seed(0)
try:
    import igraph as _ig_seed
    _ig_seed.set_random_number_generator(random.Random(0))
    del _ig_seed
except Exception:
    pass


def _label_from_names(names: list[str]) -> str:
    """Cheap structural label: most-frequent snake_case token from member names."""
    tokens: list[str] = []
    for n in names:
        tokens.extend(p for p in n.split("_") if len(p) > 2)
    if not tokens:
        return names[0][:30] if names else ""
    word, _ = Counter(t.lower() for t in tokens).most_common(1)[0]
    return word.capitalize()


def detect_communities(store: GraphStore) -> dict[str, int]:
    """Deterministic modularity community detection on the symbol call graph.

    Uses igraph community_fastgreedy (agglomerative Clauset-Newman-Moore) on the
    edged subgraph, then groups edgeless symbols by directory. Replaces the prior
    exact-k-shell partition which fragmented connected nodes across shell boundaries
    into singletons (k-core is a node ranking, not a partition).

    Returns {sid: community_id} and commits assignments + community records.
    """
    import igraph as ig

    # Sort by sid so runs on the same data produce identical vertex ordering.
    symbols = sorted(store.list_symbols(), key=lambda s: s["sid"])
    if not symbols:
        return {}

    idx: dict[str, int] = {s["sid"]: i for i, s in enumerate(symbols)}
    all_edges = store._con.execute("SELECT caller_sid,callee_sid FROM edges").fetchall()
    edges_ig = [(idx[c], idx[e]) for c, e in all_edges if c in idx and e in idx]

    g = ig.Graph(n=len(symbols), edges=edges_ig, directed=True)
    g_und = g.as_undirected(combine_edges="first")

    mapping: dict[str, int] = {}
    cid_counter = 0

    if g_und.ecount() == 0:
        # No call edges at all — group every symbol by directory.
        dirs = sorted({os.path.dirname(s["file"] or "") for s in symbols})
        dir_idx = {d: i for i, d in enumerate(dirs)}
        mapping = {
            s["sid"]: dir_idx[os.path.dirname(s["file"] or "")] for s in symbols
        }
    else:
        # Partition symbols with ≥1 call edge using fastgreedy modularity.
        edged_verts = sorted({v for e in edges_ig for v in e})
        sub = g_und.induced_subgraph(edged_verts)
        # Simplified undirected subgraph for fastgreedy (no self-loops).
        sub = sub.simplify()
        membership = sub.community_fastgreedy().as_clustering().membership
        for local_i, label in enumerate(membership):
            mapping[symbols[edged_verts[local_i]]["sid"]] = label
        cid_counter = max(membership) + 1 if membership else 0

        # Group edgeless symbols by directory (avoids N-singleton explosion).
        dir_cid: dict[str, int] = {}
        for s in symbols:
            if s["sid"] not in mapping:
                d = os.path.dirname(s["file"] or "")
                if d not in dir_cid:
                    dir_cid[d] = cid_counter
                    cid_counter += 1
                mapping[s["sid"]] = dir_cid[d]

    for sid, cid in mapping.items():
        store.assign_community(sid, cid)

    counts = Counter(mapping.values())
    for cid, cnt in counts.items():
        store.upsert_community(cid, level=1, title=None,
                               summary=None, member_count=cnt)
    sid_to_name = {s["sid"]: s["name"] for s in symbols}
    cid_to_names: dict[int, list[str]] = {}
    for sid, cid in mapping.items():
        cid_to_names.setdefault(cid, []).append(sid_to_name.get(sid, ""))
    for cid, names in cid_to_names.items():
        label = _label_from_names(names)
        if label:
            store._con.execute(
                "UPDATE communities SET title=? WHERE id=? AND title IS NULL",
                (label, cid),
            )
    store._con.execute(
        "DELETE FROM communities WHERE level=1 AND id NOT IN "
        "(SELECT DISTINCT community_id FROM symbols WHERE community_id IS NOT NULL)"
    )
    store.commit()
    return mapping


def label_community_structural(store: GraphStore, cid: int) -> None:
    """Deterministic structural label for a tail community — zero LLM tokens.

    Sets title (reuses _label_from_names if absent) and templated summary listing
    member kinds and source files. semantic_type is left NULL (abstained): it is a
    Knowledge-rung judgment, only assigned by the LLM on head/lazy paths (HR23).
    Byte-identical on repeated runs.
    """
    rows = store._con.execute(
        "SELECT name, kind, file FROM symbols WHERE community_id=? LIMIT 30",
        (cid,),
    ).fetchall()
    if not rows:
        return
    existing = store._con.execute(
        "SELECT title, member_count FROM communities WHERE id=?", (cid,)
    ).fetchone()
    title = (existing[0] if existing and existing[0] else None) or _label_from_names(
        [r[0] for r in rows]
    )
    _label_community_structural_finish(store, cid, rows, title,
                                        (existing[1] if existing else None) or len(rows))


def _label_community_structural_finish(
    store: GraphStore, cid: int, rows: list, title: str, member_count: int,
) -> None:
    kinds = list(dict.fromkeys(r[1] for r in rows if r[1]))[:4]
    files = list(dict.fromkeys(r[2] for r in rows if r[2]))[:3]
    file_names = [os.path.basename(f) for f in files]
    kind_str = ", ".join(kinds) if kinds else "mixed"
    file_str = ", ".join(file_names) if file_names else "—"
    summary = f"{member_count} symbol(s) ({kind_str}) from {file_str}."
    if file_names:
        summary += f" Primary: {file_names[0]}."
    store.upsert_community(
        cid, level=1,
        title=title[:200],
        summary=summary[:2000],
        member_count=member_count,
    )
    # Explicit NULL clobbers the COALESCE in upsert_community's ON CONFLICT clause,
    # which would otherwise preserve any prior '' or 'utility' on the stored row.
    # semantic_type is a Knowledge judgment — the tail abstains until lazily read.
    store._con.execute("UPDATE communities SET semantic_type=NULL WHERE id=?", (cid,))
