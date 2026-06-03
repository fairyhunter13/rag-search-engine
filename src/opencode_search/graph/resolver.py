"""Call resolver: maps raw callee strings to real node IDs.

Six strategies (tried in priority order per edge):
1. import_map (0.95)      — callee prefix matches a known import
2. same_module (0.90)     — callee exists in the same file
3. import_map_suffix (0.85) — suffix of callee matches an import
4. unique_name (0.75)     — exactly one node in the project with that name
5. suffix_match (0.55)    — best suffix match among all nodes

Unresolvable edges are dropped (fuzzy removed: O(n) per edge on large graphs).
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .extractor import _RawEdge
    from .storage import EdgeData, NodeData

log = logging.getLogger(__name__)


class CallResolver:
    """Resolve raw edges (callee strings) to real NodeData IDs.

    Usage:
        resolver = CallResolver(all_nodes)
        resolved_edges = resolver.resolve(raw_edges)
    """

    def __init__(self, nodes: list[NodeData]) -> None:
        from collections import defaultdict
        # Build lookup indexes — all O(1) at query time
        self._by_qualified: dict[str, NodeData] = {}
        self._by_name: dict[str, list[NodeData]] = defaultdict(list)
        self._by_file: dict[str, list[NodeData]] = defaultdict(list)
        self._by_suffix: dict[str, list[NodeData]] = defaultdict(list)  # last name segment → nodes
        self._id_to_file: dict[str, str] = {}  # node_id → file path

        for n in nodes:
            self._by_qualified[n.qualified_name] = n
            self._by_name[n.name].append(n)
            self._by_file[n.file].append(n)
            self._by_suffix[n.name].append(n)
            # Also index by last segment of qualified_name for dotted names
            last_seg = n.qualified_name.rsplit(".", 1)[-1]
            if last_seg != n.name:
                self._by_suffix[last_seg].append(n)
            self._id_to_file[n.id] = n.file

        # Fuzzy step removed — O(n) per unresolvable edge, low confidence (0.30),
        # and causes multi-minute stalls on large projects with millions of raw edges.

    def resolve(self, raw_edges: list[_RawEdge]) -> list[EdgeData]:
        """Convert _RawEdge list to real EdgeData, dropping unresolvable ones."""
        from .storage import EdgeData

        result: list[EdgeData] = []
        for raw in raw_edges:
            node_id, confidence, strategy = self._resolve_one(raw)
            if node_id is not None:
                # Graphify-compatible confidence labels:
                # confidence=1.0 → EXTRACTED (found directly in source)
                # confidence≥0.31 → INFERRED (heuristic resolution, confident pick)
                # confidence≤0.30 → AMBIGUOUS (multiple plausible candidates, best guess)
                if confidence >= 1.0:
                    label = "EXTRACTED"
                elif confidence > 0.30:
                    label = "INFERRED"
                else:
                    label = "AMBIGUOUS"
                score = confidence if label != "EXTRACTED" else None
                result.append(EdgeData(
                    from_id=raw.from_id,
                    to_id=node_id,
                    kind=raw.kind,
                    confidence=confidence,
                    resolution_strategy=strategy,
                    confidence_label=label,
                    confidence_score=score,
                ))
        return result

    def _resolve_one(
        self, raw: _RawEdge
    ) -> tuple[str | None, float, str | None]:
        """Return (node_id, confidence, strategy) or (None, 0, None)."""
        callee = raw.raw_callee.strip()
        if not callee:
            return None, 0.0, None

        # Determine which file the caller lives in
        caller_file = self._file_for_id(raw.from_id)

        # 1. Exact qualified name
        if callee in self._by_qualified:
            return self._by_qualified[callee].id, 0.95, "import_map"

        # 2. Same-module: callee name exists in the same file
        if caller_file:
            for n in self._by_file.get(caller_file, []):
                if n.name == callee or n.qualified_name.endswith(f".{callee}"):
                    return n.id, 0.90, "same_module"

        # 3. Suffix of callee matches a qualified name (import_map_suffix)
        # Use _by_suffix index for O(1) lookup, then filter by prefix — avoids O(n) scan
        parts = callee.split(".")
        if len(parts) >= 2:
            last = parts[-1]
            prefix = ".".join(parts[:-1])
            candidates = [
                n for n in self._by_suffix.get(last, [])
                if prefix in n.qualified_name
            ]
            if len(candidates) == 1:
                return candidates[0].id, 0.85, "import_map_suffix"
            if len(candidates) > 1:
                # Pick by file proximity
                if caller_file:
                    for c in candidates:
                        if c.file == caller_file:
                            return c.id, 0.85, "import_map_suffix"
                return candidates[0].id, 0.85, "import_map_suffix"

        # 4. Unique name across project
        name_matches = self._by_name.get(callee, [])
        # Also try last segment
        if not name_matches and "." in callee:
            name_matches = self._by_name.get(callee.split(".")[-1], [])
        if len(name_matches) == 1:
            return name_matches[0].id, 0.75, "unique_name"
        if len(name_matches) > 1:
            # Prefer same file
            if caller_file:
                for n in name_matches:
                    if n.file == caller_file:
                        return n.id, 0.75, "unique_name"
            # Prefer non-file nodes
            non_file = [n for n in name_matches if n.kind != "file"]
            if len(non_file) == 1:
                return non_file[0].id, 0.75, "unique_name"
            # Multiple ambiguous candidates — keep best guess at low confidence
            best = (non_file or name_matches)[0]
            return best.id, 0.30, "ambiguous_name"

        # 5. Suffix match — O(1) via pre-built suffix index
        target = callee.split(".")[-1]
        suffix_candidates = self._by_suffix.get(target, [])
        if len(suffix_candidates) == 1:
            return suffix_candidates[0].id, 0.55, "suffix_match"
        if len(suffix_candidates) > 1 and caller_file:
            for n in suffix_candidates:
                if n.file == caller_file:
                    return n.id, 0.55, "suffix_match"
        if len(suffix_candidates) > 1:
            # Multiple suffix candidates — keep best guess at low confidence
            return suffix_candidates[0].id, 0.30, "ambiguous_suffix"

        return None, 0.0, None

    def _file_for_id(self, node_id: str) -> str | None:
        """Find the file path for a given node ID — O(1) via pre-built index."""
        return self._id_to_file.get(node_id)
