"""Call resolver: maps raw callee strings to real node IDs.

Six strategies (tried in priority order per edge):
1. import_map (0.95)      — callee prefix matches a known import
2. same_module (0.90)     — callee exists in the same file
3. import_map_suffix (0.85) — suffix of callee matches an import
4. unique_name (0.75)     — exactly one node in the project with that name
5. suffix_match (0.55)    — best suffix match among all nodes
6. fuzzy (0.30)           — difflib similarity > 0.8

Unresolvable edges are dropped.
"""
from __future__ import annotations

import difflib
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .extractor import _RawEdge
    from .storage import NodeData, EdgeData

log = logging.getLogger(__name__)


class CallResolver:
    """Resolve raw edges (callee strings) to real NodeData IDs.

    Usage:
        resolver = CallResolver(all_nodes)
        resolved_edges = resolver.resolve(raw_edges)
    """

    def __init__(self, nodes: list[NodeData]) -> None:
        from collections import defaultdict
        # Build lookup indexes
        self._by_qualified: dict[str, NodeData] = {}
        self._by_name: dict[str, list[NodeData]] = defaultdict(list)
        self._by_file: dict[str, list[NodeData]] = defaultdict(list)

        for n in nodes:
            self._by_qualified[n.qualified_name] = n
            self._by_name[n.name].append(n)
            self._by_file[n.file].append(n)

    def resolve(self, raw_edges: list[_RawEdge]) -> list[EdgeData]:
        """Convert _RawEdge list to real EdgeData, dropping unresolvable ones."""
        from .storage import EdgeData

        result: list[EdgeData] = []
        for raw in raw_edges:
            node_id, confidence, strategy = self._resolve_one(raw)
            if node_id is not None:
                result.append(EdgeData(
                    from_id=raw.from_id,
                    to_id=node_id,
                    kind=raw.kind,
                    confidence=confidence,
                    resolution_strategy=strategy,
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
        parts = callee.split(".")
        if len(parts) >= 2:
            last = parts[-1]
            prefix = ".".join(parts[:-1])
            # Look for qualified names ending with .last where the prefix fragment matches
            candidates = []
            for qn, n in self._by_qualified.items():
                if qn.endswith(f".{last}") and prefix in qn:
                    candidates.append(n)
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

        # 5. Suffix match
        target = callee.split(".")[-1]
        suffix_candidates = [
            n for n in self._by_qualified.values()
            if n.qualified_name.endswith(f".{target}") or n.name == target
        ]
        if len(suffix_candidates) == 1:
            return suffix_candidates[0].id, 0.55, "suffix_match"
        if len(suffix_candidates) > 1 and caller_file:
            for n in suffix_candidates:
                if n.file == caller_file:
                    return n.id, 0.55, "suffix_match"

        # 6. Fuzzy
        all_qualified = list(self._by_qualified.keys())
        close = difflib.get_close_matches(callee, all_qualified, n=1, cutoff=0.8)
        if close:
            return self._by_qualified[close[0]].id, 0.30, "fuzzy"

        return None, 0.0, None

    def _file_for_id(self, node_id: str) -> str | None:
        """Find the file path for a given node ID."""
        for file_nodes in self._by_file.values():
            for n in file_nodes:
                if n.id == node_id:
                    return n.file
        return None
