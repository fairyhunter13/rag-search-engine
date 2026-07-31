"""Deterministic modularity community detection on the symbol call graph."""
from __future__ import annotations

import os
import random
from collections import Counter

from rag_search.graph.store import GraphStore

# Bump when the detection algorithm changes so reconcile re-derives stale graphs.
# v3 = `kind="data"` config keys no longer contribute label tokens (they stay members).
ALGO_VERSION = "fg3"  # fastgreedy modularity + directory-group for edgeless; v2 = dir+2-token labels

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


def _label_from_names(names: list[str], files: list[str] | None = None,
                      kinds: list[str] | None = None) -> str:
    """Cheap structural label: dominant directory + the two most-frequent snake_case tokens.

    One token was not enough to name a domain. Measured over every community on two real fleet
    graphs before this changed:

        project                 communities   distinct titles   worst collision
        claude-code-workflows        65        37 (57%)         'Test' x 22
        rag-search-engine            53        38 (72%)         'Test' x 14

    and after: 61/65 (94%) and 51/53 (96%), worst collisions 4 and 3. `overview(what=
    "communities")` is the whole architecture axis now that `ask` was retired into it, so it was
    reporting 22 of ccw's 65 domains under one name.

    The directory is not decoration — it is the half that separates two communities whose member
    names genuinely coincide (`test_login`/`test_logout` under `auth/` and under `api/`), which
    no amount of extra tokens can. GH6 pins exactly that pair.

    Deterministic, which is load-bearing rather than nice: titles are stored, so a label that
    varied per run would restamp and re-derive the fleet forever. `Counter.most_common` breaks
    ties by first-insertion order, and both callers feed it names in a sorted-by-sid order, so
    equal counts resolve the same way every time. That determinism is also what rules out the
    obvious "append a hash to force uniqueness" fix, which would score 100% distinct and mean
    nothing.

    `files` and `kinds` are both optional so the label degrades rather than raising when a caller
    has names but no paths, or names but no kinds.

    `kind="data"` rows name the *tokens* half but stay members. They are config keys — 28.1% of
    symbol rows fleet-wide (62,449 of 222,483, counted 2026-07-31) — and they have no call edges
    by construction, so they cannot have influenced the partition they would otherwise be naming.
    A community that is *entirely* data still gets named from its own keys: dropping to an empty
    token list would leave `title` NULL and lose the label altogether, which is worse than a
    config-flavoured name for a config-flavoured community.
    """
    naming = names
    if kinds is not None:
        code = [n for n, k in zip(names, kinds, strict=True) if k != "data"]
        naming = code or names
    tokens: list[str] = []
    for n in naming:
        tokens.extend(p for p in n.split("_") if len(p) > 2)
    counted = Counter(t.lower() for t in tokens).most_common(2)
    words = " ".join(w.capitalize() for w, _ in counted)
    if not words:
        words = naming[0][:30] if naming else ""
    scope = ""
    if files:
        dirs = Counter(os.path.dirname(f) for f in files if f)
        if dirs:
            top = dirs.most_common(1)[0][0]
            scope = os.path.basename(top) or top
    return f"{scope}/{words}" if scope and words else words


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
    all_edges = store.conn.execute("SELECT caller_sid,callee_sid FROM edges").fetchall()
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
    sid_to_sym = {s["sid"]: (s["name"], s["file"] or "", s["kind"] or "") for s in symbols}
    cid_to_members: dict[int, tuple[list[str], list[str], list[str]]] = {}
    for sid, cid in mapping.items():
        name, file, kind = sid_to_sym.get(sid, ("", "", ""))
        ns, fs, ks = cid_to_members.setdefault(cid, ([], [], []))
        ns.append(name)
        fs.append(file)
        ks.append(kind)
    for cid, (names, files, kinds) in cid_to_members.items():
        label = _label_from_names(names, files, kinds)
        if label:
            store.conn.execute(
                "UPDATE communities SET title=? WHERE id=? AND title IS NULL",
                (label, cid),
            )
    store.conn.execute(
        "DELETE FROM communities WHERE level=1 AND id NOT IN "
        "(SELECT DISTINCT community_id FROM symbols WHERE community_id IS NOT NULL)"
    )
    store.commit()
    return mapping


def label_community_structural(store: GraphStore, cid: int) -> None:
    """Deterministic structural label for a tail community — zero LLM tokens.

    Sets title (reuses _label_from_names if absent) and templated summary listing
    member kinds and source files. Byte-identical on repeated runs.

    This used to also clobber `semantic_type` to NULL, so a tail community abstained from
    the Knowledge-rung judgment the LLM made on head paths (HR23). Both the column and the
    judgment left with tier 3; the abstention has nothing left to abstain from.
    """
    # ORDER BY is not cosmetic: `LIMIT 30` with none left the sampled 30 up to sqlite, so the
    # title and the summary's kind/file lists could differ between two runs over identical data
    # — against this function's own "byte-identical on repeated runs" claim, and a stored title
    # that moves re-derives the fleet forever. Ordering by name makes the sample the same 30.
    rows = store.conn.execute(
        "SELECT name, kind, file FROM symbols WHERE community_id=? ORDER BY name LIMIT 30",
        (cid,),
    ).fetchall()
    if not rows:
        return
    existing = store.conn.execute(
        "SELECT title, member_count FROM communities WHERE id=?", (cid,)
    ).fetchone()
    title = (existing[0] if existing and existing[0] else None) or _label_from_names(
        [r[0] for r in rows], [r[2] or "" for r in rows], [r[1] or "" for r in rows]
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
