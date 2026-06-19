"""LLM enrichment: symbol intent (batch 20/call) + community summary + semantic type classification."""
from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np

from opencode_search.graph.llm import chat
from opencode_search.graph.store import GraphStore

_INTENT_PROMPT = """\
For each symbol below, reply with ONLY a JSON array of short intent strings (≤8 words each).
Format: ["intent for 1", "intent for 2", ...]
Symbols:
{symbols}"""

_L2_COMMUNITY_PROMPT = """\
Summarize this architecture domain in 2 sentences. Cover: purpose, main sub-systems.
Sub-communities: {children}
Reply ONLY with JSON: {{"title": "<short domain name>", "summary": "<2 sentences>"}}"""

_COMMUNITY_PROMPT = """\
Summarize this code community in 2 sentences. Cover: purpose, main patterns.
Members: {members}
Reply ONLY with JSON: {{"title": "<short title>", "summary": "<2 sentences>", \
"semantic_type": "<business_process|business_rule|feature|utility|infrastructure|domain|test>"}}
Hint: if orchestrating multi-step workflows -> business_process; if enforcing constraints/validation -> business_rule."""

# ---------------------------------------------------------------------------
# Dynamic semantic type classification — Prototypical Networks (June 2026)
# No static description text, no keywords, no vocabulary assumptions.
# Prototype vectors are derived from ACTUAL community embeddings (LLM-labeled seeds).
# ---------------------------------------------------------------------------

_TYPE_ORDER: list[str] = [
    "business_process", "business_rule", "feature",
    "utility", "infrastructure", "domain", "test",
]
_EMBED_CONF_THRESHOLD: float = float(os.getenv("OSE_EMBED_CONF_THRESHOLD", "0.08"))
_MMR_SEED_N: int = 70        # diverse seeds via MMR (≈10 per type × 7 types)
_MIN_SEEDS_PER_TYPE: int = 3  # min LLM-labeled examples per type to trust centroid

# Minimal prompt — type names + one contrastive note; no vocabulary definitions.
_BACKFILL_BATCH_PROMPT = """\
Classify each code community into ONE of these semantic types:
business_process | business_rule | feature | utility | infrastructure | domain | test

Reason from what the code DOES (read member intents, file paths, summary).
Key distinction: business_process orchestrates multiple steps; business_rule enforces a constraint.

Communities:
{items}

Reply with JSON: [{{"id": <N>, "semantic_type": "<type>", "reasoning": "<1 sentence why>"}}]"""


def _classify_batch(batch: list[tuple[int, str, str]]) -> list[tuple[int, str]]:
    """Send one LLM call to classify ≤20 communities. Returns [(cid, semantic_type)]."""
    valid = frozenset(_TYPE_ORDER)
    items_str = json.dumps([
        {"id": cid, "title": title, "summary": (summary or "")[:120]}
        for cid, title, summary in batch
    ], ensure_ascii=False)
    try:
        raw = chat(_BACKFILL_BATCH_PROMPT.format(items=items_str))
        parsed = json.loads(raw.strip())
        return [
            (int(item["id"]),
             item.get("semantic_type", "feature")
             if item.get("semantic_type", "") in valid else "feature")
            for item in parsed
        ]
    except Exception:
        return [(cid, "feature") for cid, _, _ in batch]


# ---------------------------------------------------------------------------
# Rich context assembly — DeepWiki-style
# ---------------------------------------------------------------------------

def _community_rich_text(
    store: GraphStore, cid: int, title: str, summary: str,
    member_count: int, parent_title: str | None,
) -> str:
    """Rich context: title + summary + member intents + file paths + edge count + L2 domain."""
    rows = store._con.execute(
        "SELECT name, kind, COALESCE(intent,''), file FROM symbols "
        "WHERE community_id=? ORDER BY CASE WHEN intent!='' THEN 0 ELSE 1 END LIMIT 10",
        (cid,),
    ).fetchall()
    member_lines = "\n  ".join(
        f"- [{r[1]}] {r[0]}" + (f": {r[2]}" if r[2] else "") for r in rows
    )
    files = list(dict.fromkeys(r[3] for r in rows if r[3]))[:4]
    edge_count = store._con.execute(
        "SELECT COUNT(*) FROM edges e "
        "JOIN symbols s ON (e.caller_sid=s.sid OR e.callee_sid=s.sid) "
        "WHERE s.community_id=?", (cid,)
    ).fetchone()[0]
    parts = [
        title,
        f"Summary: {summary}" if summary else "",
        f"Members ({member_count}):\n  {member_lines}" if member_lines else "",
        "Files: " + ", ".join(files) if files else "",
        f"Connectivity: high ({edge_count} edges)" if edge_count >= 5 else "",
        f"Domain: {parent_title}" if parent_title else "",
    ]
    return "\n".join(p for p in parts if p)


# ---------------------------------------------------------------------------
# Prototype persistence + MMR seed selection
# ---------------------------------------------------------------------------

def _prototypes_path(store: GraphStore) -> Path:
    return Path(str(store._db_path)).parent / "type_prototypes.npy"


def _load_stored_prototypes(store: GraphStore) -> np.ndarray | None:
    p = _prototypes_path(store)
    return np.load(str(p)) if p.exists() else None


def _save_prototypes(store: GraphStore, proto_vecs: np.ndarray) -> None:
    np.save(str(_prototypes_path(store)), proto_vecs)


def _select_diverse_seeds_mmr(vecs: np.ndarray, n: int) -> list[int]:
    """Select n indices maximally covering the embedding space (no keywords, pure geometry).

    Maximum Marginal Relevance: greedily pick each next point farthest from all
    already-selected points (cosine distance). SIGIR '25: improves few-shot ICL in 70% of settings.
    """
    n = min(n, len(vecs))
    centroid = vecs.mean(axis=0, keepdims=True)
    dist_to_centroid = 1.0 - (vecs @ centroid.T).squeeze()
    selected = [int(np.argmin(dist_to_centroid))]
    selected_set = set(selected)
    while len(selected) < n:
        sel_vecs = vecs[selected]
        max_sim = (vecs @ sel_vecs.T).max(axis=1)
        dist = 1.0 - max_sim
        for idx in selected_set:
            dist[idx] = -1.0
        next_idx = int(np.argmax(dist))
        selected.append(next_idx)
        selected_set.add(next_idx)
    return selected


def _build_dynamic_prototypes(
    store: GraphStore, rows: list, all_vecs: np.ndarray, thermal_guard_fn=None,
) -> np.ndarray | None:
    """Build per-type centroid prototypes from LLM-labeled diverse seed communities.

    Returns (7, 768) L2-normalised centroids, or None if too few seeds per type.
    Prototypes come from ACTUAL community embeddings, never from human-authored text.
    """
    import time
    seed_indices = _select_diverse_seeds_mmr(all_vecs, n=_MMR_SEED_N)
    seed_rows = [(rows[i][0], rows[i][1] or "", rows[i][2] or "") for i in seed_indices]
    batches = [seed_rows[i: i + 20] for i in range(0, len(seed_rows), 20)]
    seed_labels: dict[int, str] = {}
    valid = frozenset(_TYPE_ORDER)
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(_classify_batch, b) for b in batches]
        for future in as_completed(futures):
            if thermal_guard_fn and thermal_guard_fn():
                time.sleep(3)
            try:
                for cid, stype in future.result():
                    if stype in valid:
                        seed_labels[cid] = stype
            except Exception:
                pass
    cid_to_idx = {rows[i][0]: i for i in seed_indices}
    type_vecs: dict[str, list] = {t: [] for t in _TYPE_ORDER}
    for cid, stype in seed_labels.items():
        if cid in cid_to_idx:
            type_vecs[stype].append(all_vecs[cid_to_idx[cid]])
    if not all(len(type_vecs[t]) >= _MIN_SEEDS_PER_TYPE for t in _TYPE_ORDER):
        return None
    centroids = []
    for stype in _TYPE_ORDER:
        c = np.stack(type_vecs[stype]).mean(axis=0)
        centroids.append(c / (np.linalg.norm(c) + 1e-8))
    return np.stack(centroids)


def _classify_by_prototypes(
    all_vecs: np.ndarray,
    proto_vecs: np.ndarray,
) -> list[tuple[str, float]]:
    """Cosine similarity to data-derived centroid prototypes → (type, confidence_margin)."""
    scores = all_vecs @ proto_vecs.T          # (N, 7)
    top1_idx = scores.argmax(axis=1)
    sorted_sc = np.sort(scores, axis=1)
    confidence = sorted_sc[:, -1] - sorted_sc[:, -2]
    return [(_TYPE_ORDER[int(i)], float(c)) for i, c in zip(top1_idx, confidence, strict=False)]


def classify_communities_semantic(
    store: GraphStore, thermal_guard_fn=None, *, reclassify_all: bool = False,
) -> int:
    """Fully dynamic semantic classification. Returns count of type changes."""
    import time

    from opencode_search.embed.embedder import get_embedder

    valid = frozenset(_TYPE_ORDER)
    if reclassify_all:
        rows = store._con.execute(
            "SELECT c.id, c.title, c.summary, c.member_count, c.semantic_type, p.title "
            "FROM communities c LEFT JOIN communities p ON c.parent_id=p.id "
            "WHERE c.level=1 AND c.summary IS NOT NULL AND c.summary!=''"
        ).fetchall()
    else:
        rows = store._con.execute(
            "SELECT c.id, c.title, c.summary, c.member_count, c.semantic_type, p.title "
            "FROM communities c LEFT JOIN communities p ON c.parent_id=p.id "
            "WHERE c.level=1 AND c.summary IS NOT NULL AND c.summary!='' "
            f"AND (c.semantic_type IS NULL OR c.semantic_type NOT IN "
            f"({','.join('?' * len(valid))}))",
            tuple(valid),
        ).fetchall()
    if not rows:
        return 0
    if thermal_guard_fn and thermal_guard_fn():
        time.sleep(3)
    texts = [
        _community_rich_text(store, r[0], r[1] or "", r[2] or "", r[3] or 0, r[5])
        for r in rows
    ]
    all_vecs = get_embedder().embed(texts)
    proto_vecs = _load_stored_prototypes(store)
    if proto_vecs is None:
        proto_vecs = _build_dynamic_prototypes(store, rows, all_vecs, thermal_guard_fn)
        if proto_vecs is not None:
            _save_prototypes(store, proto_vecs)
    if proto_vecs is not None:
        results = _classify_by_prototypes(all_vecs, proto_vecs)
        high_conf = [
            (rows[i][0], t) for i, (t, c) in enumerate(results) if c >= _EMBED_CONF_THRESHOLD
        ]
        llm_pending = [
            (rows[i][0], rows[i][1] or "", rows[i][2] or "")
            for i, (_, c) in enumerate(results) if c < _EMBED_CONF_THRESHOLD
        ]
    else:
        high_conf = []
        llm_pending = [(r[0], r[1] or "", r[2] or "") for r in rows]
    llm_results: list[tuple[int, str]] = []
    batches = [llm_pending[i: i + 20] for i in range(0, len(llm_pending), 20)]
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(_classify_batch, b) for b in batches]
        for future in as_completed(futures):
            if thermal_guard_fn and thermal_guard_fn():
                time.sleep(3)
            try:  # noqa: SIM105
                llm_results.extend(future.result())
            except Exception:
                pass
    proposed = dict(high_conf + llm_results)
    current = {r[0]: r[4] for r in rows}
    updates = 0
    for cid, new_type in proposed.items():
        if new_type != current.get(cid):
            store._con.execute("UPDATE communities SET semantic_type=? WHERE id=?", (new_type, cid))
            updates += 1
    if updates:
        store.commit()
    return updates


def enrich_community_l2(store: GraphStore, community_id: int) -> None:
    """Assign title+summary to one L2 community from its enriched L1 child summaries."""
    rows = store._con.execute(
        "SELECT title, summary FROM communities "
        "WHERE parent_id=? AND summary IS NOT NULL AND summary!=''",
        (community_id,),
    ).fetchall()
    if not rows:
        return
    children = "; ".join(f"{r[0]}: {r[1][:100]}" for r in rows if r[0])
    try:
        raw = chat(_L2_COMMUNITY_PROMPT.format(children=children[:2000]))
        data = json.loads(raw.strip())
        store.upsert_community(
            community_id, level=2,
            title=data.get("title", "")[:200],
            summary=data.get("summary", "")[:2000],
            member_count=len(rows),
        )
        store.commit()
    except Exception:
        pass


def enrich_symbols(store: GraphStore, batch_size: int = 20) -> int:
    """Assign LLM-generated intent to symbols that lack one. Returns count enriched."""
    symbols = [s for s in store.list_symbols() if not s.get("intent")]
    enriched = 0
    for i in range(0, len(symbols), batch_size):
        batch = symbols[i : i + batch_size]
        lines = "\n".join(
            f"{j + 1}. [{s['kind']}] {s['name']} in {s['file']}"
            for j, s in enumerate(batch)
        )
        try:
            raw = chat(_INTENT_PROMPT.format(symbols=lines))
            intents = json.loads(raw.strip())
            if isinstance(intents, list):
                for sym, intent in zip(batch, intents, strict=False):
                    store.set_intent(sym["sid"], str(intent)[:120])
                    enriched += 1
        except Exception:
            pass
    store.commit()
    return enriched


def enrich_community(store: GraphStore, community_id: int) -> None:
    """Assign title + summary + semantic_type to one community via LLM."""
    rows = store._con.execute(
        "SELECT name,kind,file FROM symbols WHERE community_id=? LIMIT 30",
        (community_id,),
    ).fetchall()
    if not rows:
        return
    members = "; ".join(f"{r[1]} {r[0]}" for r in rows)
    try:
        raw = chat(_COMMUNITY_PROMPT.format(members=members[:2000]))
        data = json.loads(raw.strip())
        store.upsert_community(
            community_id, level=1,
            title=data.get("title", "")[:200],
            summary=data.get("summary", "")[:2000],
            member_count=len(rows),
            semantic_type=data.get("semantic_type", "")[:50],
        )
        store.commit()
    except Exception:
        pass
