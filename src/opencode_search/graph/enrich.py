"""LLM enrichment: community summary (batched, prefix-cached) + semantic type classification."""
from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from opencode_search.graph.llm import _accumulate_llm_tokens, deepseek_chat, deepseek_extract
from opencode_search.graph.store import GraphStore

log = logging.getLogger(__name__)

_L2_COMMUNITY_PROMPT = """\
Summarize this architecture domain in 2 sentences. Cover: purpose, main sub-systems.
Sub-communities: {children}
Reply ONLY with JSON: {{"title": "<short domain name>", "summary": "<2 sentences>"}}"""

# Stable system prompt for batched enrichment via deepseek_extract (prefix-cached).
# Byte-identical across all batch calls → high prompt_cache_hit_tokens from batch 2 on.
_COMMUNITY_SYSTEM_PROMPT = """\
You are a senior software architect summarizing code communities for a knowledge graph.
For each community, write a 2-sentence summary covering purpose and main patterns.
Assign semantic_type from: business_process | business_rule | feature | utility | infrastructure | domain | test
(business_process: multi-step workflow; business_rule: constraint/validation; test: test code)
Reply ONLY with a JSON array in the same order as input:
[{"id": <N>, "title": "<≤8 word title>", "summary": "<2 sentences>", "semantic_type": "<type>"}]"""

# ---------------------------------------------------------------------------
# Semantic type classification — direct LLM (DeepSeek) over title + summary.
# Fully semantic (LLM world knowledge); no keywords, no embeddings, no prototypes.
# DeepSeek separates test-of-business from business logic — which a 1.7B model + cosine
# similarity to data-derived centroids could not (the embedding of "Test Suite for
# Campaign Management" sits next to the business centroid). Persisted in
# communities.semantic_type; the daemon classifies only new/unclassified communities
# (reclassify_all=False) so it never churns.
# ---------------------------------------------------------------------------

_TYPE_ORDER: list[str] = [
    "business_process", "business_rule", "feature",
    "utility", "infrastructure", "domain", "test",
]

# Communities with these types are excluded from retrieval context (ask.py).
# Keep literals in ask.py in sync with this set; test_schema_consistency.py enforces it.
EXCLUDED_FROM_RETRIEVAL: frozenset[str] = frozenset({"test"})

# Minimal prompt — type names + one contrastive note; no vocabulary definitions.
# Stable system prompt for classify (prefix-cached — byte-identical across all classify batches).
_CLASSIFY_SYSTEM = """\
Classify each code community into ONE of these semantic types:
business_process | business_rule | feature | utility | infrastructure | domain | test

Reason from what the code DOES (file paths, summary).
Key distinction: business_process orchestrates multiple steps; business_rule enforces a constraint.

Reply with JSON: [{"id": <N>, "semantic_type": "<type>", "reasoning": "<1 sentence why>"}]"""

# Stable system prompt for batched L2 domain narration (prefix-cached).
_L2_BATCH_SYSTEM = """\
You are a senior software architect summarizing architecture domains.
For each domain, write a 2-sentence summary covering purpose and main sub-systems.
Reply ONLY with a JSON array: [{"id": <N>, "title": "<short domain name>", "summary": "<2 sentences>"}]"""


def _kb_chat(prompt: str, *, max_tokens: int = 2048) -> str:
    """KB-build LLM: cloud DeepSeek only. Raises if no key or unreachable.

    Local generative LLM is decommissioned — DeepSeek is the sole KB-build path.
    temperature=0 keeps summaries/classification reproducible (no churn).
    """
    return deepseek_chat(prompt, max_tokens=max_tokens)


def _parse_types(raw: str, valid: frozenset[str]) -> dict[int, str]:
    """Best-effort extract {id: semantic_type} from possibly-noisy LLM output.

    Tolerates <think>…</think> blocks and ```json fences (no regex, string ops only).
    Returns only items whose type is canonical; {} if nothing parseable.
    """
    text = raw.split("</think>")[-1] if "</think>" in raw else raw
    text = text.replace("```json", "").replace("```", "")
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end <= start:
        return {}
    try:
        parsed = json.loads(text[start:end + 1])
    except Exception:
        return {}
    out: dict[int, str] = {}
    for item in parsed if isinstance(parsed, list) else []:
        try:
            st = item.get("semantic_type", "")
            if st in valid:
                out[int(item["id"])] = st
        except Exception:
            continue
    return out


def _classify_batch(batch: list[tuple[int, str, str]]) -> list[tuple[int, str]]:
    """Classify ≤20 narrated communities via prefix-cached deepseek_extract call.

    On a total parse failure, splits and retries once rather than mislabeling the
    whole batch as 'feature'; unparsed communities are omitted (left for the next run).
    """
    if not batch:
        return []
    valid = frozenset(_TYPE_ORDER)
    items_str = json.dumps(
        [{"id": cid, "title": title, "summary": (summary or "")[:120]}
         for cid, title, summary in batch],
        ensure_ascii=False,
    )
    try:
        raw, usage = deepseek_extract(_CLASSIFY_SYSTEM, "Communities:\n" + items_str)
        _accumulate_llm_tokens(usage, "classify")
    except Exception:
        return []
    labels = _parse_types(raw, valid)
    if not labels and len(batch) > 1:
        mid = len(batch) // 2
        return _classify_batch(batch[:mid]) + _classify_batch(batch[mid:])
    return [(cid, labels[cid]) for cid, _, _ in batch if cid in labels]


def compute_significance(store: GraphStore) -> tuple[list[int], list[int]]:
    """Classify all unenriched L1 communities into (head, tail) in two SQL queries.

    Head = worth LLM narration: member_count≥8 OR ≥2 cross-community edges.
    Tail = deterministic structural labels only (zero tokens).
    L2/L3 communities are always handled separately (enrich_community_l2).
    """
    unenriched = store._con.execute(
        "SELECT id, member_count FROM communities "
        "WHERE (summary IS NULL OR summary = '') AND level = 1"
    ).fetchall()
    if not unenriched:
        return [], []
    cross_deg: dict[int, int] = dict(store._con.execute(
        """SELECT sc.community_id, COUNT(*)
           FROM edges e
           JOIN symbols sc  ON e.caller_sid  = sc.sid
           JOIN symbols sc2 ON e.callee_sid = sc2.sid
           WHERE sc.community_id != sc2.community_id
           GROUP BY sc.community_id"""
    ).fetchall())
    head, tail = [], []
    for cid, mc in unenriched:
        if (mc or 0) >= 8 or cross_deg.get(cid, 0) >= 2:
            head.append(cid)
        else:
            tail.append(cid)
    return head, tail


def _enrich_one_batch(
    store: GraphStore, batch_ids: list[int], valid: frozenset[str],
) -> tuple[list[int], dict]:
    """One deepseek_extract call for ≤20 communities; parse + upsert results."""
    items = [
        {"id": cid, "members": "; ".join(
            f"{r[1]} {r[0]}" for r in store._con.execute(
                "SELECT name, kind FROM symbols WHERE community_id=? LIMIT 30", (cid,)
            ).fetchall()
        )[:800]}
        for cid in batch_ids
        if store._con.execute(
            "SELECT COUNT(*) FROM symbols WHERE community_id=?", (cid,)
        ).fetchone()[0] > 0
    ]
    if not items:
        return [], {}
    try:
        raw, usage = deepseek_extract(
            _COMMUNITY_SYSTEM_PROMPT,
            "Communities:\n" + json.dumps(items, ensure_ascii=False),
            max_tokens=min(len(items) * 200 + 50, 4096),
        )
        _accumulate_llm_tokens(usage, "enrich")
    except Exception:
        return [], {}
    text = raw.split("</think>")[-1] if "</think>" in raw else raw
    text = text.replace("```json", "").replace("```", "")
    s, e = text.find("["), text.rfind("]")
    if s == -1 or e <= s:
        return [], usage
    try:
        parsed = json.loads(text[s:e + 1])
    except Exception:
        return [], usage
    enriched: list[int] = []
    for item in (parsed if isinstance(parsed, list) else []):
        try:
            cid = int(item["id"])
            if cid not in set(batch_ids):
                continue
            st = str(item.get("semantic_type", ""))
            mc = store._con.execute(
                "SELECT member_count FROM communities WHERE id=?", (cid,)
            ).fetchone()
            store.upsert_community(
                cid, level=1,
                title=str(item.get("title", ""))[:200],
                summary=str(item.get("summary", ""))[:2000],
                member_count=(mc[0] if mc else 0),
                semantic_type=(st if st in valid else None),
                narrated=1,
            )
            # Explicit NULL when LLM returns invalid type (abstain, not force "utility").
            if st not in valid:
                store._con.execute("UPDATE communities SET semantic_type=NULL WHERE id=?", (cid,))
            enriched.append(cid)
        except Exception:
            continue
    store.commit()
    return enriched, usage


def classify_communities_semantic(
    store: GraphStore, thermal_guard_fn=None, *, reclassify_all: bool = False,
) -> int:
    """Classify L1 communities into semantic types via direct LLM (DeepSeek). No embeddings.

    Returns count of type changes. reclassify_all=True re-labels every L1 community
    (one-time migration / forced refresh). reclassify_all=False (daemon default) labels only
    NULL/non-canonical rows, so once a community is classified it is never re-run → stable.
    """
    import time

    valid = frozenset(_TYPE_ORDER)
    if reclassify_all:
        rows = store._con.execute(
            "SELECT id, title, summary, semantic_type, member_count FROM communities "
            "WHERE level=1 AND narrated=1 AND summary IS NOT NULL AND summary!=''"
        ).fetchall()
    else:
        rows = store._con.execute(
            "SELECT id, title, summary, semantic_type, member_count FROM communities "
            "WHERE level=1 AND narrated=1 AND summary IS NOT NULL AND summary!='' "
            f"AND (semantic_type IS NULL OR semantic_type NOT IN "
            f"({','.join('?' * len(valid))}))",
            tuple(valid),
        ).fetchall()
    if not rows:
        return 0
    pending = [(r[0], r[1] or "", r[2] or "") for r in rows]
    results: list[tuple[int, str]] = []
    batches = [pending[i: i + 20] for i in range(0, len(pending), 20)]
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(_classify_batch, b) for b in batches]
        for future in as_completed(futures):
            if thermal_guard_fn and thermal_guard_fn():
                time.sleep(3)
            try:  # noqa: SIM105
                results.extend(future.result())
            except Exception:
                pass
    current = {r[0]: r[3] for r in rows}
    updates = 0
    for cid, new_type in results:
        if new_type != current.get(cid):
            store._con.execute("UPDATE communities SET semantic_type=? WHERE id=?", (new_type, cid))
            updates += 1
    if updates:
        store.commit()
    return updates


def enrich_communities_batch(
    store: GraphStore, community_ids: list[int], *,
    thermal_guard_fn=None, budget: int = 50_000,
) -> tuple[list[int], dict]:
    """Narrate a list of L1 communities via batched prefix-cached DeepSeek calls.

    Each chunk of 20 is one deepseek_extract call.  System prompt is byte-identical
    across all chunks → high prompt_cache_hit_tokens from chunk 2 onward.
    budget: max completion tokens before stopping (safety cap, default 50k).
    Returns (enriched_ids, aggregate_usage).
    """
    import time

    if not community_ids:
        return [], {}
    agg: dict[str, int] = {
        "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 0,
        "completion_tokens": 0, "calls": 0,
    }
    valid = frozenset(_TYPE_ORDER)
    enriched: list[int] = []
    for i in range(0, len(community_ids), 20):
        if agg["completion_tokens"] >= budget:
            log.warning("enrich_communities_batch: budget %d tokens reached, stopping", budget)
            break
        if thermal_guard_fn and thermal_guard_fn():
            time.sleep(5)
        batch_enriched, usage = _enrich_one_batch(store, community_ids[i:i + 20], valid)
        enriched.extend(batch_enriched)
        for k in ("prompt_cache_hit_tokens", "prompt_cache_miss_tokens", "completion_tokens"):
            agg[k] = agg.get(k, 0) + usage.get(k, 0)
        if usage:
            agg["calls"] += 1
    return enriched, agg


def _l2_upsert_parsed(store: GraphStore, parsed: list, batch_set: set) -> int:
    n = 0
    for it in (x for x in parsed if isinstance(x, dict)):
        try:
            cid = int(it["id"])
            if cid not in batch_set:
                continue
            mc = store._con.execute("SELECT COALESCE(member_count,0) FROM communities WHERE id=?", (cid,)).fetchone()
            store.upsert_community(cid, level=2, narrated=1,
                title=str(it.get("title",""))[:200], summary=str(it.get("summary",""))[:2000],
                member_count=(mc[0] if mc else 0))
            n += 1
        except Exception:
            continue
    return n


def enrich_communities_l2_batch(
    store: GraphStore, community_ids: list[int], *, thermal_guard_fn=None,
) -> int:
    """Batch L2 narration via prefix-cached deepseek_extract (≤20/call). Returns count enriched."""
    import time
    if not community_ids:
        return 0
    enriched = 0
    for i in range(0, len(community_ids), 20):
        if thermal_guard_fn and thermal_guard_fn():
            time.sleep(5)
        batch = community_ids[i : i + 20]
        items = []
        for cid in batch:
            ch = store._con.execute(
                "SELECT title, summary FROM communities "
                "WHERE parent_id=? AND summary IS NOT NULL AND summary!=''", (cid,)).fetchall()
            if ch:
                items.append({"id": cid, "children":
                    "; ".join(f"{r[0]}: {r[1][:100]}" for r in ch if r[0])[:2000]})
        if not items:
            continue
        try:
            raw, u = deepseek_extract(_L2_BATCH_SYSTEM,
                "Domains:\n" + json.dumps(items, ensure_ascii=False),
                max_tokens=min(len(items) * 200 + 50, 4096))
            _accumulate_llm_tokens(u, "l2")
        except Exception:
            continue
        t = (raw.split("</think>")[-1] if "</think>" in raw else raw).replace("```json","").replace("```","")
        if (s := t.find("[")) == -1 or (e := t.rfind("]")) <= s:
            continue
        try:
            parsed = json.loads(t[s : e + 1])
        except Exception:
            continue
        enriched += _l2_upsert_parsed(store, parsed, set(batch))
        store.commit()
    return enriched


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
        raw = _kb_chat(_L2_COMMUNITY_PROMPT.format(children=children[:2000]), max_tokens=512)
        data = json.loads(raw.strip())
        store.upsert_community(
            community_id, level=2,
            title=data.get("title", "")[:200],
            summary=data.get("summary", "")[:2000],
            member_count=len(rows),
            narrated=1,
        )
        store.commit()
    except Exception:
        pass


def narrate_community_lazy(store: GraphStore, cid: int) -> bool:
    """Lazy query-time narration for a single tail community (Phase 3).

    Generates title/summary/semantic_type via one deepseek_extract call, persists
    to DB, sets narrated=1 (idempotency guard — will never re-narrate this community).
    Returns True if narration succeeded, False on any error / already narrated.
    """
    row = store._con.execute(
        "SELECT narrated FROM communities WHERE id=?", (cid,)
    ).fetchone()
    if not row or row[0]:
        return False  # already narrated or not found
    valid = frozenset(_TYPE_ORDER)
    enriched, _ = _enrich_one_batch(store, [cid], valid)
    return bool(enriched)

