"""LLM enrichment: symbol intent (batch 20/call) + community summary + semantic type classification."""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed

from opencode_search.graph.llm import chat, deepseek_chat, deepseek_key
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

# Minimal prompt — type names + one contrastive note; no vocabulary definitions.
_BACKFILL_BATCH_PROMPT = """\
Classify each code community into ONE of these semantic types:
business_process | business_rule | feature | utility | infrastructure | domain | test

Reason from what the code DOES (read member intents, file paths, summary).
Key distinction: business_process orchestrates multiple steps; business_rule enforces a constraint.

Communities:
{items}

Reply with JSON: [{{"id": <N>, "semantic_type": "<type>", "reasoning": "<1 sentence why>"}}]"""


def _kb_chat(prompt: str, *, max_tokens: int = 2048) -> str:
    """KB-build LLM call: cloud DeepSeek primary, local Ollama fallback, '' on total failure.

    Removing the *resident* local LLM is the fix for the idle busy-spin load root cause:
    when a DeepSeek key is present the build path makes stateless HTTPS calls and Ollama is
    never loaded (no llama-server, no 8 GB RSS, no spinning core). Ollama remains only as an
    offline fallback. temperature=0 keeps summaries/classification reproducible (no churn).
    """
    try:
        if deepseek_key():
            return deepseek_chat(prompt, max_tokens=max_tokens)
    except Exception:
        pass
    try:
        return chat(prompt, temperature=0.0, num_predict=max_tokens, think=False)
    except Exception:
        return ""


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
    """Classify ≤20 communities via one LLM call. Returns only confidently-parsed (cid, type).

    qwen3 thinking-mode is disabled and the output budget raised so the JSON answer isn't
    starved. On a total parse failure, splits and retries once rather than mislabeling the
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
    # DeepSeek (cloud) primary — separates tests from business logic far better than the
    # local 1.7B model — with local Ollama fallback. No resident LLM when a key is present.
    labels = _parse_types(_kb_chat(_BACKFILL_BATCH_PROMPT.format(items=items_str)), valid)
    if not labels and len(batch) > 1:
        mid = len(batch) // 2
        return _classify_batch(batch[:mid]) + _classify_batch(batch[mid:])
    return [(cid, labels[cid]) for cid, _, _ in batch if cid in labels]


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
            "WHERE level=1 AND summary IS NOT NULL AND summary!=''"
        ).fetchall()
    else:
        rows = store._con.execute(
            "SELECT id, title, summary, semantic_type, member_count FROM communities "
            "WHERE level=1 AND summary IS NOT NULL AND summary!='' "
            f"AND (semantic_type IS NULL OR semantic_type NOT IN "
            f"({','.join('?' * len(valid))}))",
            tuple(valid),
        ).fetchall()
    if not rows:
        return 0
    member_count = {r[0]: (r[4] or 0) for r in rows}
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
        # Structural guard: a multi-step business process / enforced rule spans >1 symbol;
        # a degenerate (<3-member) community cannot be one — demote to feature.
        if member_count.get(cid, 0) < 3 and new_type in ("business_process", "business_rule"):
            new_type = "feature"
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
        raw = chat(_L2_COMMUNITY_PROMPT.format(children=children[:2000]),
                   temperature=0.0, num_predict=512, think=False)
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
            raw = chat(_INTENT_PROMPT.format(symbols=lines),
                   temperature=0.0, num_predict=512, think=False)
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
        raw = chat(_COMMUNITY_PROMPT.format(members=members[:2000]),
                   temperature=0.0, num_predict=512, think=False)
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
