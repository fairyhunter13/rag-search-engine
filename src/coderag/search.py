"""Two retrievers, one fusion, one cross-encoder, and a ranked list of locations.

Both lanes ship because each owns a query modality, and the same agent sends
both minutes apart. *Practical Code RAG at Scale* separates them and gets
opposite answers: PL->PL -- an identifier, a signature, an error string -- goes
to BM25 by =10 pp EM and an order of magnitude faster; NL->PL -- a question in
English -- goes to the dense arm. That is why `mode` is a real parameter and
not a debugging aid.

**Results are locations, not bodies, by default.** *Is Grep All You Need?* wired
a literal grep and a vector tool into the same agents and inline grep won every
harness-model pair -- but vector retrieval beat grep on 5 of 10 pairs when its
results arrived as file locations to open rather than inline content. So `path`,
`lines` and a three-line preview are the default payload, and `include_body` is
opt-in.

RRF is adjacent to both retrievers it fuses on purpose: a fusion function that
lives away from the two rank lists it consumes is a function nobody re-reads
when either list changes shape.
"""

from __future__ import annotations

import difflib
import time
from fnmatch import fnmatch
from pathlib import Path

from . import config, embed, federation, filters, lexical, registry, store
from .rank import Hit, diversify, pool_cut


class SearchError(ValueError):
    """A caller error that names the valid set. Never a silently widened corpus."""


def _rows(conn, ids: list[int]) -> dict[int, dict]:
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    sql = (
        "SELECT c.id, c.start_line, c.end_line, c.text, f.path, f.lang "
        f"FROM chunks c JOIN files f ON f.id = c.file_id WHERE c.id IN ({marks})"
    )
    return {r["id"]: dict(r) for r in conn.execute(sql, ids)}


def _lexical(conn, query: str, limit: int) -> list[int]:
    """BM25 over the body, the identifier parts, and the scope header.

    All three columns, unweighted: the header is where the path and the
    enclosing declaration live, and a query naming a class the chunk sits inside
    matches nothing in the body.
    """
    match = lexical.fts_query(query)
    if not match:
        return []
    rows = conn.execute(
        "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY rank LIMIT ?",
        (match, limit),
    )
    return [r["rowid"] for r in rows]


def _dense(conn, vector, limit: int) -> list[int]:
    rows = conn.execute(
        "SELECT chunk_id FROM chunks_vec WHERE embedding MATCH ? AND k = ? ORDER BY distance",
        (vector.astype("float32").tobytes(), limit),
    )
    return [r["chunk_id"] for r in rows]


def rrf(lists: list[list[int]], k: int = 0) -> dict[int, float]:
    """Reciprocal rank fusion. Ranks, never scores.

    BM25 and cosine distance are not comparable in any units, and normalising
    them means inventing a scale. RRF only ever reads position, so a lane that
    changes its scoring cannot move the fusion.
    """
    k = k or config.RRF_K
    fused: dict[int, float] = {}
    for ranked in lists:
        for position, chunk_id in enumerate(ranked):
            fused[chunk_id] = fused.get(chunk_id, 0.0) + 1.0 / (k + position + 1)
    return fused


def _project_candidates(project: Path, query: str, vector, mode: str, limit: int) -> list[Hit]:
    try:
        conn = store.connect(project, create=False)
    except FileNotFoundError:
        return []

    lanes, ranks = [], {}
    if mode in ("hybrid", "lexical"):
        ids = _lexical(conn, query, limit)
        lanes.append(ids)
        ranks["bm25"] = {cid: i for i, cid in enumerate(ids)}
    if mode in ("hybrid", "semantic") and vector is not None:
        ids = _dense(conn, vector, limit)
        lanes.append(ids)
        ranks["dense"] = {cid: i for i, cid in enumerate(ids)}

    fused = rrf(lanes)
    rows = _rows(conn, list(fused))
    hits = []
    for chunk_id, score in fused.items():
        row = rows.get(chunk_id)
        if row is None:
            continue
        hits.append(
            Hit(
                project=str(project),
                path=str(project / row["path"]),
                rel_path=row["path"],
                start_line=row["start_line"],
                end_line=row["end_line"],
                lang=row["lang"],
                text=row["text"],
                scores={
                    "rrf": round(score, 6),
                    "bm25": ranks.get("bm25", {}).get(chunk_id),
                    "dense": ranks.get("dense", {}).get(chunk_id),
                    "rerank": None,
                },
            )
        )
    return hits


def _check_lang(lang: str) -> None:
    """An unknown `lang` is a caller error, the same as an unknown `mode`.

    Without this it returns an empty list -- indistinguishable from "nothing
    matched", which is the one failure a caller cannot debug. Note what is *not*
    in the valid set: a file whose extension carries no label gets `lang=""` and
    is matched by no filter at all. That is the denylist discovery paying off --
    a new language is indexed with no change here -- and the cost is that it is
    reachable only by leaving `lang` unset.
    """
    valid = sorted(set(filters.LANGS.values()))
    if lang in valid:
        return
    near = difflib.get_close_matches(lang, valid, n=1, cutoff=0.6)
    hint = f" -- did you mean {near[0]!r}?" if near else f" -- valid: {valid}"
    raise SearchError(f"unknown lang {lang!r}{hint}")


def _filter(hits: list[Hit], path_glob: str | None, lang: str | None) -> list[Hit]:
    if path_glob:
        hits = [h for h in hits if fnmatch(h.rel_path, path_glob)]
    if lang:
        hits = [h for h in hits if h.lang == lang]
    return hits


def _payload(hit: Hit, rank: int, preview_lines: int, include_body: bool) -> dict:
    out = {
        "rank": rank,
        "project": hit.project,
        "path": hit.path,
        "rel_path": hit.rel_path,
        "lines": [hit.start_line, hit.end_line],
        "lang": hit.lang,
        "score": hit.scores.get("rerank") or hit.scores["rrf"],
        "scores": hit.scores,
    }
    if preview_lines:
        out["preview"] = "\n".join(hit.text.splitlines()[:preview_lines])
    if include_body:
        out["body"] = hit.text
    return out


def search(
    query: str,
    root: Path | str = "",
    *,
    k: int = 10,
    mode: str = "hybrid",
    rerank: bool = True,
    path_glob: str | None = None,
    lang: str | None = None,
    max_per_file: int = 2,
    preview_lines: int = 3,
    include_body: bool = False,
) -> dict:
    """A root together with its federated members, or a member with its root's.

    One root, never a list. The expansion is the engine's job, not the
    caller's, and the fleet-wide alternative is not offered at all: unscoped
    fan-out across 148 projects measured 164.78 s against 7.01 s scoped, and
    answered a question about one repo with a member's vendored JavaScript.

    `federation.unit` is what widens a member, and it is not free. Measured
    from `gen3-app-c` on 2026-08-25: 0.65 s for the member alone against
    17.6 s for the 143 projects its root federates. The one-project answer was
    the cheaper of the two and it was the wrong one.
    """
    started = time.perf_counter()
    if mode not in config.MODES:
        raise SearchError(f"mode must be one of {list(config.MODES)}, got {mode!r}")
    if not query.strip():
        raise SearchError("query is empty")
    if lang:
        _check_lang(lang)
    k = max(1, min(int(k), config.MAX_K))

    root = registry.resolve(root or Path.cwd())
    # Three conditions, and the message has always claimed all three. `get`
    # returns disabled rows and unflagging deletes no store, so a project the
    # user explicitly unflagged stayed searchable by name until this line said
    # `enabled`; `indexed_at` is what the user asked for and what makes the
    # message true. `members_of` already filters on `enabled`, never the root.
    entry = registry.get(root)
    if entry is None or not entry.enabled or entry.indexed_at is None:
        raise SearchError(f"{root} is not indexed -- call index(root={str(root)!r}) first")
    projects = federation.unit(root)

    vector = None
    if mode in ("hybrid", "semantic"):
        vector = embed.get_embedder().embed([query], side=embed.QUERY)[0]

    pool: list[Hit] = []
    for project in projects:
        pool.extend(_project_candidates(project, query, vector, mode, config.CANDIDATES))
    pool = _filter(pool, path_glob, lang)
    pool = pool_cut(pool, root, config.CANDIDATES)

    reranked = rerank and bool(pool)
    if reranked:
        scores = embed.get_reranker().score(query, [h.text for h in pool])
        for hit, score in zip(pool, scores, strict=True):
            hit.scores["rerank"] = round(float(score), 6)
        pool.sort(key=lambda h: h.scores["rerank"], reverse=True)

    hits = diversify(pool, k, max_per_file, root)
    files, chunks = 0, 0
    for project in projects:
        try:
            counted = store.counts(store.connect(project, create=False))
        except FileNotFoundError:
            continue
        files, chunks = files + counted[0], chunks + counted[1]

    return {
        "query": query,
        "mode": mode,
        "reranked": reranked,
        "took_ms": round((time.perf_counter() - started) * 1000, 2),
        "searched": {"projects": len(projects), "files": files, "chunks": chunks},
        "results": [_payload(h, i + 1, preview_lines, include_body) for i, h in enumerate(hits)],
        "hint": "" if hits else "no matches; retry with mode='lexical' for an exact identifier",
    }
