"""What a fused pool has to survive before it is an answer.

Two shaping passes, either side of the cross-encoder: `pool_cut` decides who
reaches it, `diversify` decides who leaves it. Both exist because the fused
score is a rank within one project and means nothing across two.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class Hit:
    project: str
    path: str
    rel_path: str
    start_line: int
    end_line: int
    lang: str
    text: str
    scores: dict


def _fingerprint(hit: Hit) -> tuple[str, str]:
    """Same file, same text modulo whitespace: the chunker's overlap makes these."""
    return (hit.rel_path, " ".join(hit.text.split())[:400])


def pool_cut(pool: list[Hit], root: Path, limit: int) -> list[Hit]:
    """Take from each project by its own rank, because the scores do not compare.

    RRF fuses lanes *within* a project, so what it returns is a rank: every
    project's best hit scores about the same whatever it is about. Sorting the
    flat pool by that and truncating keeps roughly everyone's top one -- across
    136 members that is the whole pool, and the caller's own third hit, the one
    the reranker would have put first, never reaches the reranker.

    The root is the subject of the query, so it takes half the slots outright
    and the members share the rest round-robin. That order is also the answer's
    order when `rerank=False`, which is the caller-settable case where the
    incomparable score would otherwise be final.
    """
    by_project: dict[str, deque[Hit]] = defaultdict(deque)
    for hit in sorted(pool, key=lambda h: h.scores["rrf"], reverse=True):
        by_project[hit.project].append(hit)

    own = by_project.pop(str(root), deque())
    members = list(by_project.values())
    taken = [own.popleft() for _ in range(min(len(own), max(1, limit // 2)))]
    while len(taken) < limit and (own or any(members)):
        for queue in members:
            if queue and len(taken) < limit:
                taken.append(queue.popleft())
        if own and len(taken) < limit:
            # Whatever the members left unfilled goes back to the caller's own
            # project rather than shrinking the pool.
            taken.append(own.popleft())
    return taken


def diversify(hits: list[Hit], k: int, max_per_file: int, root: Path | str = "") -> list[Hit]:
    """Collapse near-duplicates, cap per file, then backfill to k.

    Position matters: this runs **after** the cross-encoder sort and **before**
    the k cut, so the reranker still scores the whole pool and only the answer
    is thinned. Both rules keep the highest-scoring member, so nothing ever
    overtakes a better result from another file.

    Only the cap backfills, and the asymmetry is the point. A file's third-best
    chunk is a real answer held back for spread, so it returns when there is
    room -- diversity is a preference between equally good answers, never a
    reason to return fewer of them. A near-duplicate is the same answer twice,
    and giving it a slot back is how a k of 10 becomes five distinct results.
    """
    kept, held, seen, per_file = [], [], set(), {}
    # Which copy survives a collapse, not whether one does. The fingerprint is
    # project-blind, so an identical chunk vendored into a member used to win on
    # score and the caller was shown a member's path for code in their own tree.
    own = {_fingerprint(h) for h in hits if h.project == str(root)}
    for hit in hits:
        fingerprint = _fingerprint(hit)
        if fingerprint in seen or (fingerprint in own and hit.project != str(root)):
            continue
        seen.add(fingerprint)
        count = per_file.get((hit.project, hit.rel_path), 0)
        if count >= max_per_file:
            held.append(hit)
            continue
        per_file[(hit.project, hit.rel_path)] = count + 1
        kept.append(hit)
    return (kept + held)[:k]
