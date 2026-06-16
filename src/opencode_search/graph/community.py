"""Leiden community detection on the symbol call graph."""
from __future__ import annotations

from collections import Counter

from opencode_search.graph.store import GraphStore


def _label_from_names(names: list[str]) -> str:
    """Cheap structural label: most-frequent snake_case token from member names."""
    tokens: list[str] = []
    for n in names:
        tokens.extend(p for p in n.split("_") if len(p) > 2)
    if not tokens:
        return names[0][:30] if names else ""
    word, _ = Counter(t.lower() for t in tokens).most_common(1)[0]
    return word.capitalize()


def detect_communities(store: GraphStore, *, resolution: float = 1.0) -> dict[str, int]:
    """Run Leiden on call edges; fall back to file-level grouping if no edges.

    Returns {sid: community_id} and commits assignments + community records.
    """
    import igraph as ig
    import leidenalg

    symbols = store.list_symbols()
    if not symbols:
        return {}

    idx: dict[str, int] = {s["sid"]: i for i, s in enumerate(symbols)}
    all_edges = store._con.execute("SELECT caller_sid,callee_sid FROM edges").fetchall()
    edges_ig = [(idx[c], idx[e]) for c, e in all_edges if c in idx and e in idx]

    g = ig.Graph(n=len(symbols), edges=edges_ig, directed=True)
    if g.ecount() == 0:
        files = sorted({s["file"] for s in symbols})
        file_idx = {f: i for i, f in enumerate(files)}
        mapping: dict[str, int] = {s["sid"]: file_idx[s["file"]] for s in symbols}
    else:
        # ModularityVertexPartition does not accept resolution_parameter (CPM does).
        part = leidenalg.find_partition(
            g.as_undirected(),
            leidenalg.ModularityVertexPartition,
            n_iterations=3,
        )
        mapping = {symbols[i]["sid"]: part.membership[i] for i in range(len(symbols))}
        # Merge singleton communities into file-level groups so isolated/leaf
        # symbols share a community with their file-mates instead of staying alone.
        _counts = Counter(mapping.values())
        _singletons = {cid for cid, cnt in _counts.items() if cnt == 1}
        if _singletons:
            _file_base = max(_counts) + 1
            _sid_file = {s["sid"]: s["file"] for s in symbols}
            _file_cid: dict[str, int] = {}
            for _sid in list(mapping):
                if mapping[_sid] in _singletons:
                    _f = _sid_file.get(_sid, "")
                    if _f not in _file_cid:
                        _file_cid[_f] = _file_base
                        _file_base += 1
                    mapping[_sid] = _file_cid[_f]

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
