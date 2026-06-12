"""Graph MCP handlers: symbol lookup, call traversal, impact analysis."""
from __future__ import annotations

import collections
import json
import logging
import time
import xml.etree.ElementTree as _ET
from pathlib import Path
from typing import TYPE_CHECKING, Any

from opencode_search.config import get_project_graph_db_path
from opencode_search.discover import is_document_language

if TYPE_CHECKING:
    from opencode_search.graph.storage import GraphStorage

log = logging.getLogger(__name__)

# In-process TTL cache for handle_detect_patterns (expensive file walk on large repos)
_PATTERNS_CACHE: dict[str, tuple[float, dict]] = {}
_PATTERNS_TTL = 300.0  # 5 minutes in-process
_PATTERNS_FILE_TTL = 86400.0  # 24 hours for on-disk cache

# ---------------------------------------------------------------------------
# Pattern-detection helpers (called from handle_project_structure)
# ---------------------------------------------------------------------------


_VERSION_SEPS = (">=", "==", "!=", "~=", "<=", "<", ">")


def _parse_dep_spec(raw: str) -> tuple[str, str]:
    """Parse a PEP 508-style dependency specifier into (name, version) via str-ops — no regex."""
    spec = raw.split(";")[0].split("#")[0].strip()
    for sep in _VERSION_SEPS:
        if sep in spec:
            name, _, rest = spec.partition(sep)
            return name.strip().split("[")[0], sep + rest.strip().split()[0]
    return spec.split("[")[0].strip(), "*"


def _detect_dependencies(root: Path) -> dict[str, Any]:
    """Parse dependency manifest files to extract package names and versions.

    All parsers use grammar-based or stdlib format parsers — no regex.
    """
    packages: list[dict[str, Any]] = []
    files_found: list[str] = []
    manager = "unknown"

    def _try_go_mod(p: Path) -> None:
        nonlocal manager
        try:
            text = p.read_text(errors="replace")
            if manager == "unknown":
                manager = "go_modules"
            in_require = False
            for line in text.splitlines():
                stripped = line.strip()
                if stripped.startswith("require ("):
                    in_require = True
                    continue
                if in_require and stripped == ")":
                    in_require = False
                    continue
                if in_require or stripped.startswith("require "):
                    cleaned = stripped.removeprefix("require ").strip()
                    if cleaned.startswith("(") or not cleaned or cleaned.startswith("//"):
                        continue
                    # "module_path version [// indirect]" — str.split, no regex
                    parts = cleaned.split()
                    if len(parts) >= 2 and parts[1].startswith("v"):
                        indirect = len(parts) > 2 and "indirect" in " ".join(parts[2:])
                        packages.append({"name": parts[0], "version": parts[1], "direct": not indirect})
        except Exception:
            pass

    def _try_requirements(p: Path) -> None:
        nonlocal manager
        try:
            if manager == "unknown":
                manager = "pip"
            for line in p.read_text(errors="replace").splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or stripped.startswith("-"):
                    continue
                name, version = _parse_dep_spec(stripped)
                if name:
                    packages.append({"name": name, "version": version, "direct": True})
        except Exception:
            pass

    def _try_package_json(p: Path) -> None:
        nonlocal manager
        try:
            data = json.loads(p.read_text(errors="replace"))
            if manager == "unknown":
                manager = "npm"
            for dep, ver in (data.get("dependencies") or {}).items():
                packages.append({"name": dep, "version": ver, "direct": True})
            for dep, ver in (data.get("devDependencies") or {}).items():
                packages.append({"name": dep, "version": ver, "direct": False})
        except Exception:
            pass

    def _try_cargo_toml(p: Path) -> None:
        nonlocal manager
        try:
            import tomllib
            if manager == "unknown":
                manager = "cargo"
            data = tomllib.loads(p.read_bytes().decode("utf-8", errors="replace"))
            for section in ("dependencies", "dev-dependencies", "build-dependencies"):
                for name, spec in (data.get(section) or {}).items():
                    if isinstance(spec, str):
                        version = spec
                    elif isinstance(spec, dict):
                        version = spec.get("version", "*")
                    else:
                        version = "*"
                    packages.append({"name": name, "version": version or "*", "direct": True})
        except Exception:
            pass

    def _try_pyproject(p: Path) -> None:
        nonlocal manager
        try:
            import tomllib
            data = tomllib.loads(p.read_bytes().decode("utf-8", errors="replace"))
            project = data.get("project") or {}
            poetry = (data.get("tool") or {}).get("poetry") or {}
            if project:
                if manager == "unknown":
                    manager = "pip"
                for dep in project.get("dependencies") or []:
                    if isinstance(dep, str):
                        name, version = _parse_dep_spec(dep)
                        if name:
                            packages.append({"name": name, "version": version, "direct": True})
            if poetry:
                if manager == "unknown":
                    manager = "poetry"
                for name, spec in (poetry.get("dependencies") or {}).items():
                    if name == "python":
                        continue
                    version = spec if isinstance(spec, str) else (spec.get("version", "*") if isinstance(spec, dict) else "*")
                    packages.append({"name": name, "version": version or "*", "direct": True})
                for name, spec in (poetry.get("dev-dependencies") or {}).items():
                    version = spec if isinstance(spec, str) else (spec.get("version", "*") if isinstance(spec, dict) else "*")
                    packages.append({"name": name, "version": version or "*", "direct": False})
        except Exception:
            pass

    def _try_pom_xml(p: Path) -> None:
        nonlocal manager
        try:
            if manager == "unknown":
                manager = "maven"
            tree = _ET.parse(p)
            ns = {"m": "http://maven.apache.org/POM/4.0.0"}
            deps_list = tree.findall(".//m:dependency", ns)
            if not deps_list:
                deps_list = tree.findall(".//dependency")
            for dep in deps_list:
                gid = dep.find("m:groupId", ns) if dep.find("m:groupId", ns) is not None else dep.find("groupId")
                aid = dep.find("m:artifactId", ns) if dep.find("m:artifactId", ns) is not None else dep.find("artifactId")
                ver = dep.find("m:version", ns) if dep.find("m:version", ns) is not None else dep.find("version")
                if gid is not None and aid is not None:
                    name = f"{gid.text}:{aid.text}"
                    version = ver.text if ver is not None else "*"
                    packages.append({"name": name, "version": version, "direct": True})
        except Exception:
            pass

    def _try_go_work(p: Path) -> None:
        nonlocal manager
        try:
            text = p.read_text(errors="replace")
            manager = "go_workspace"
            in_use = False
            for line in text.splitlines():
                stripped = line.strip()
                if stripped.startswith("use ("):
                    in_use = True
                    continue
                if in_use and stripped == ")":
                    in_use = False
                    continue
                if in_use and stripped and not stripped.startswith("//"):
                    packages.append({"name": stripped, "version": "workspace", "direct": True})
                elif stripped.startswith("use ") and not stripped.startswith("use ("):
                    mod = stripped[4:].strip()
                    if mod:
                        packages.append({"name": mod, "version": "workspace", "direct": True})
        except Exception:
            pass

    def _try_gradle(p: Path) -> None:
        nonlocal manager
        try:
            if manager == "unknown":
                manager = "gradle"
            # Parse with tree-sitter groovy grammar; walk leaf string literals.
            # Maven coordinates (group:artifact:version) identified by three ':'-separated
            # parts — structural format check, no keyword/regex matching.
            from tree_sitter_language_pack.api import get_parser as _ts_get_parser
            parser = _ts_get_parser("groovy")
            tree = parser.parse(p.read_bytes())

            def _collect(node) -> None:
                if not node.children:  # leaf node — check if it looks like a quoted string
                    raw = node.text.decode("utf-8", errors="replace")
                    for q in ('"', "'"):
                        if raw.startswith(q) and raw.endswith(q) and len(raw) >= 5:
                            inner = raw[1:-1]
                            if "${" not in inner:  # skip GString interpolation
                                parts = inner.split(":")
                                if len(parts) == 3:
                                    name = f"{parts[0]}:{parts[1]}"
                                    version = parts[2].strip()
                                    if name and version and " " not in name:
                                        packages.append({
                                            "name": name,
                                            "version": version,
                                            "direct": True,
                                        })
                            break
                for child in node.children:
                    _collect(child)

            _collect(tree.root_node)
        except Exception:
            pass

    manifest_handlers: dict[str, Any] = {
        "go.work": _try_go_work,
        "go.mod": _try_go_mod,
        "requirements.txt": _try_requirements,
        "requirements-dev.txt": _try_requirements,
        "requirements-test.txt": _try_requirements,
        "package.json": _try_package_json,
        "Cargo.toml": _try_cargo_toml,
        "pyproject.toml": _try_pyproject,
        "pom.xml": _try_pom_xml,
        "build.gradle": _try_gradle,
    }

    # Build search dirs: root + 1st-level dirs + symlinked repos inside container dirs.
    # Symlinked entries (federation members) are added first so they aren't squeezed
    # out by non-repo subdirs when the cap is reached.
    _SKIP_SCAN = {".git", ".venv", "venv", "node_modules", "__pycache__", "target", "dist", "build"}
    search_dirs: list[Path] = [root]
    first_level: list[Path] = []
    try:
        for d in root.iterdir():
            if d.is_dir() and not d.name.startswith(".") and d.name not in _SKIP_SCAN:
                search_dirs.append(d)
                first_level.append(d)
    except PermissionError:
        pass

    # Second level: scan non-symlink first-level dirs for federation member repos.
    # Symlinked sub-entries (federation members) are prioritised over plain dirs.
    for d in first_level:
        if d.is_symlink():
            continue
        try:
            subs = sorted(d.iterdir())
        except PermissionError:
            continue
        for sub in subs:  # symlinked repos first (federation pattern)
            if sub.is_symlink() and sub.is_dir() and len(search_dirs) < 80:
                search_dirs.append(sub)
        for sub in subs:  # then plain dirs
            if not sub.is_symlink() and sub.is_dir() and len(search_dirs) < 80:
                search_dirs.append(sub)

    seen_rel: set[str] = set()
    for d in search_dirs:
        for fname, handler in manifest_handlers.items():
            candidate = d / fname
            if not candidate.exists():
                continue
            try:
                rel = str(candidate.relative_to(root))
            except ValueError:
                rel = str(candidate)
            if rel not in seen_rel:
                seen_rel.add(rel)
                files_found.append(rel)
                handler(candidate)

    return {
        "manager": manager,
        "packages": packages[:200],
        "manifest_files": files_found,
    }


# is_document_language() from discover.py replaces this frozenset — single source of truth.


def _count_languages_accurate(root: Path, project_path: str) -> list[dict[str, Any]]:
    """Count source-code files per language using detect_language() + iter_files().

    Uses follow_symlinks=True so federation member repos (symlinked dirs) are
    included. Caps on recognized-source-file count, not total files visited, so
    the 10k limit isn't exhausted by documentation or config files.
    """
    try:
        from opencode_search.discover import detect_language, iter_files
        counter: collections.Counter = collections.Counter()
        source_total = 0
        for path in iter_files(root, follow_symlinks=True):
            lang = detect_language(path)
            if is_document_language(lang):
                continue  # skip docs/unknown — don't count toward cap
            counter[lang] += 1
            source_total += 1
            if source_total >= 10_000:
                break
        if source_total == 0:
            return []
        result = []
        for lang, count in counter.most_common(10):
            result.append({"name": lang, "files": count, "percentage": round(count / source_total * 100, 1)})
        return result
    except Exception:
        return []


def _detect_module_structure(root: Path) -> dict[str, Any]:
    """Return raw directory facts for the module structure — no heuristic type label.

    top_packages and detected_dirs are real parsed facts from the filesystem.
    The LLM patterns_cache.json provides the architecture/module_style label.
    """
    _SKIP = {".git", "node_modules", "__pycache__", ".venv", "venv", "target", "dist", "build"}
    try:
        top_dirs = sorted(
            d.name for d in root.iterdir()
            if d.is_dir() and not d.name.startswith(".") and d.name not in _SKIP
        )
    except Exception:
        return {"top_packages": [], "detected_dirs": []}

    if not top_dirs:
        return {"top_packages": [], "detected_dirs": []}

    return {"top_packages": top_dirs[:8], "detected_dirs": top_dirs[:20]}


def _load_external_imports(project_path: str) -> list[dict[str, Any]]:
    """Return top external imports from external_imports.json (real parsed imports, no mapping)."""
    try:
        from opencode_search.config import get_project_index_dir
        ext_file = get_project_index_dir(project_path) / "external_imports.json"
        if not ext_file.exists():
            return []
        data = json.loads(ext_file.read_text(encoding="utf-8"))
        return data.get("top_imports", [])
    except Exception:
        return []


async def handle_detect_patterns(project_path: str, force: bool = False) -> dict[str, Any]:
    """Detect code style, architecture, dependencies, and module organization.

    Results are cached in-process for 5 minutes — the full file walk is expensive
    on large polyrepos (astro-project: ~2 min scan). Pass force=True to bypass cache.

    Returns comprehensive pattern analysis:
    - languages: file counts by language name (accurate, gitignore-aware)
    - dependencies: manifests + packages with versions (go.work, go.mod, build.gradle, ...)
    - package_versions: flat {name: version} convenience map
    - version_summary: count of pinned vs floating dependencies
    - conventions: indent, naming, test style, logging, struct tags
    - key_frameworks: detected frameworks (gRPC, Spring Boot, React, ...)
    - module_structure: layout pattern (clean_architecture, monorepo, go_standard, ...)
    - architecture: synthesized high-level label (microservices_federation, ...)
    """
    import asyncio

    root = Path(project_path).expanduser().resolve()
    if not root.is_dir():
        return {"error": f"Not a directory: {project_path}", "project_path": project_path}

    cache_key = str(root)
    if not force:
        # 1. Check in-process cache (sub-second)
        cached_entry = _PATTERNS_CACHE.get(cache_key)
        if cached_entry and (time.monotonic() - cached_entry[0]) < _PATTERNS_TTL:
            return cached_entry[1]
        # 2. Check on-disk cache (persists across daemon restarts — 24h TTL)
        try:
            from opencode_search.config import get_project_index_dir
            disk_cache = get_project_index_dir(str(root)) / "patterns_detect_cache.json"
            if disk_cache.exists():
                import json as _json
                disk_data = _json.loads(disk_cache.read_text(encoding="utf-8"))
                cached_at = disk_data.get("_cached_at", 0)
                if time.time() - cached_at < _PATTERNS_FILE_TTL:
                    _PATTERNS_CACHE[cache_key] = (time.monotonic(), disk_data)
                    return disk_data
        except Exception:
            pass

    def _run() -> dict[str, Any]:
        # Facts: entirely deterministic, no LLM, no keyword maps
        languages = _count_languages_accurate(root, project_path)
        dependencies = _detect_dependencies(root)
        module_structure = _detect_module_structure(root)
        imports_in_use = _load_external_imports(project_path)

        package_versions: dict[str, str] = {}
        for pkg in dependencies.get("packages", []):
            pkg_name = pkg.get("name", "")
            ver = pkg.get("version", "")
            if pkg_name and ver and pkg_name not in package_versions:
                package_versions[pkg_name] = ver

        pinned = sum(
            1 for v in package_versions.values()
            if v and v not in ("*", "workspace", "latest")
        )
        return {
            "status": "ok",
            "project_path": str(root),
            "languages": languages,
            "dependencies": dependencies,
            "package_versions": package_versions,
            "version_summary": {
                "pinned": pinned,
                "floating": len(package_versions) - pinned,
                "total": len(package_versions),
            },
            "module_structure": module_structure,
            "imports_in_use": imports_in_use,
            # Labels from LLM patterns_cache — filled in below, cold cache → None
            "conventions": None,
            "key_frameworks": [],
            "architecture": None,
            "llm_used": False,
        }

    result = await asyncio.to_thread(_run)

    # Merge cached LLM labels (non-blocking — never slows the fast path)
    # Cold cache → labels remain None/[]/None with degraded:true
    degraded = True
    try:
        from opencode_search.handlers._patterns import load_patterns_cache
        llm_cached = load_patterns_cache(project_path)
        if llm_cached and llm_cached.get("llm_analysis"):
            llm = llm_cached["llm_analysis"]
            result["conventions"] = llm.get("conventions")
            result["key_frameworks"] = llm.get("key_frameworks") or []
            result["architecture"] = llm.get("architecture")
            result["llm_analysis"] = llm
            result["llm_cached_at"] = llm_cached.get("cached_at")
            degraded = False
        else:
            result["llm_analysis"] = None
            result["llm_cached_at"] = None
    except Exception:
        result["llm_analysis"] = None
        result["llm_cached_at"] = None

    result["degraded"] = degraded

    _PATTERNS_CACHE[cache_key] = (time.monotonic(), result)
    # Persist to disk so next daemon restart skips the expensive file walk
    try:
        import json as _json

        from opencode_search.config import get_project_index_dir
        disk_data = dict(result)
        disk_data["_cached_at"] = time.time()
        disk_cache = get_project_index_dir(str(root)) / "patterns_detect_cache.json"
        disk_cache.parent.mkdir(parents=True, exist_ok=True)
        disk_cache.write_text(_json.dumps(disk_data), encoding="utf-8")
    except Exception:
        pass

    return result


def _open_graph(project_path: str) -> GraphStorage | None:
    from opencode_search.graph.storage import GraphStorage

    db_path = get_project_graph_db_path(project_path)
    if not Path(db_path).exists():
        return None
    gs = GraphStorage(db_path)
    gs.open()
    return gs


async def handle_get_symbol(name: str, project_path: str) -> dict[str, Any]:
    """Find a symbol by name or qualified_name. Returns definition + caller/callee counts."""
    import asyncio

    def _run() -> dict[str, Any]:
        gs = _open_graph(project_path)
        if gs is None:
            return {"error": "project not indexed or graph not built", "project_path": project_path}
        try:
            nodes = gs.get_nodes_by_name(name)
            if not nodes:
                return {"error": f"symbol '{name}' not found", "matches": []}
            results = []
            for n in nodes:
                callers = gs.get_callers(n.id, depth=1)
                callees = gs.get_callees(n.id, depth=1)
                results.append({
                    "id": n.id,
                    "name": n.name,
                    "qualified_name": n.qualified_name,
                    "kind": n.kind,
                    "file": n.file,
                    "start_line": n.start_line,
                    "end_line": n.end_line,
                    "language": n.language,
                    "signature": n.signature,
                    "docstring": n.docstring,
                    "community_id": n.community_id,
                    "intent": n.intent,
                    "caller_count": len(callers),
                    "callee_count": len(callees),
                })
            return {"matches": results, "count": len(results)}
        finally:
            gs.close()

    return await asyncio.to_thread(_run)


async def handle_get_callers(
    symbol: str,
    project_path: str,
    depth: int = 5,
) -> dict[str, Any]:
    """BFS upstream: who calls this symbol."""
    import asyncio

    def _run() -> dict[str, Any]:
        gs = _open_graph(project_path)
        if gs is None:
            return {"error": "graph not built", "callers": []}
        try:
            node = gs.get_node(symbol)
            if node is None:
                return {"error": f"symbol '{symbol}' not found", "callers": []}
            chain = gs.get_callers(node.id, depth=depth)
            return {
                "symbol": symbol,
                "node_id": node.id,
                "callers": [
                    {
                        "node_id": c.node_id,
                        "name": c.name,
                        "qualified_name": c.qualified_name,
                        "file": c.file,
                        "kind": c.kind,
                        "depth": c.depth,
                        "confidence": round(c.confidence, 3),
                    }
                    for c in chain
                ],
                "total": len(chain),
            }
        finally:
            gs.close()

    return await asyncio.to_thread(_run)


async def handle_get_callees(
    symbol: str,
    project_path: str,
    depth: int = 5,
) -> dict[str, Any]:
    """BFS downstream: what does this symbol call."""
    import asyncio

    def _run() -> dict[str, Any]:
        gs = _open_graph(project_path)
        if gs is None:
            return {"error": "graph not built", "callees": []}
        try:
            node = gs.get_node(symbol)
            if node is None:
                return {"error": f"symbol '{symbol}' not found", "callees": []}
            chain = gs.get_callees(node.id, depth=depth)
            return {
                "symbol": symbol,
                "node_id": node.id,
                "callees": [
                    {
                        "node_id": c.node_id,
                        "name": c.name,
                        "qualified_name": c.qualified_name,
                        "file": c.file,
                        "kind": c.kind,
                        "depth": c.depth,
                        "confidence": round(c.confidence, 3),
                    }
                    for c in chain
                ],
                "total": len(chain),
            }
        finally:
            gs.close()

    return await asyncio.to_thread(_run)


async def handle_trace_path(
    from_symbol: str,
    to_symbol: str,
    project_path: str,
) -> dict[str, Any]:
    """BFS shortest path between two symbols."""
    import asyncio

    def _run() -> dict[str, Any]:
        gs = _open_graph(project_path)
        if gs is None:
            return {"error": "graph not built", "path": []}
        try:
            from_node = gs.get_node(from_symbol)
            to_node = gs.get_node(to_symbol)
            if from_node is None:
                return {"error": f"symbol '{from_symbol}' not found", "path": []}
            if to_node is None:
                return {"error": f"symbol '{to_symbol}' not found", "path": []}
            node_ids = gs.trace_path(from_node.id, to_node.id)
            if node_ids is None:
                return {
                    "from": from_symbol, "to": to_symbol,
                    "path": [], "connected": False,
                }
            steps = []
            for nid in node_ids:
                n = gs.get_node_by_id(nid)
                steps.append({
                    "node_id": nid,
                    "name": n.name if n else nid,
                    "qualified_name": n.qualified_name if n else nid,
                    "file": n.file if n else "",
                    "kind": n.kind if n else "",
                })
            return {
                "from": from_symbol, "to": to_symbol,
                "path": steps,
                "hops": len(steps) - 1,
                "connected": True,
            }
        finally:
            gs.close()

    return await asyncio.to_thread(_run)


async def handle_detect_impact(
    symbol: str,
    project_path: str,
) -> dict[str, Any]:
    """Blast radius: everything that transitively calls this symbol."""
    import asyncio
    from collections import defaultdict

    def _run() -> dict[str, Any]:
        gs = _open_graph(project_path)
        if gs is None:
            return {"error": "graph not built", "callers_by_depth": {}}
        try:
            node = gs.get_node(symbol)
            if node is None:
                return {"error": f"symbol '{symbol}' not found", "callers_by_depth": {}}
            chain = gs.get_callers(node.id, depth=10)
            by_depth: dict[int, list[dict]] = defaultdict(list)
            for c in chain:
                by_depth[c.depth].append({
                    "node_id": c.node_id,
                    "name": c.name,
                    "qualified_name": c.qualified_name,
                    "file": c.file,
                    "kind": c.kind,
                    "confidence": round(c.confidence, 3),
                })
            return {
                "symbol": symbol,
                "node_id": node.id,
                "total_affected": len(chain),
                "callers_by_depth": {str(k): v for k, v in sorted(by_depth.items())},
            }
        finally:
            gs.close()

    return await asyncio.to_thread(_run)


async def handle_import_cycles(
    project_path: str,
    max_cycle_length: int = 8,
    top_n: int = 20,
) -> dict[str, Any]:
    """Detect circular import dependencies using Tarjan's SCC on the file-level graph."""
    import asyncio

    def _run() -> dict[str, Any]:
        gs = _open_graph(project_path)
        if gs is None:
            return {"error": "project not indexed or graph not built", "cycles": [], "cycle_count": 0}
        try:
            cycles = gs.find_import_cycles(max_cycle_length=max_cycle_length, top_n=top_n)
            return {
                "project_path": project_path,
                "cycles": cycles,
                "cycle_count": len(cycles),
                "has_cycles": len(cycles) > 0,
            }
        finally:
            gs.close()

    return await asyncio.to_thread(_run)


async def handle_suggest_questions(
    project_path: str,
    top_n: int = 7,
) -> dict[str, Any]:
    """Generate questions the graph is uniquely positioned to answer."""
    import asyncio

    def _run() -> dict[str, Any]:
        gs = _open_graph(project_path)
        if gs is None:
            return {"error": "project not indexed or graph not built", "questions": []}
        try:
            questions = gs.suggest_questions(top_n=top_n)
            return {
                "project_path": project_path,
                "questions": questions,
                "count": len(questions),
            }
        finally:
            gs.close()

    return await asyncio.to_thread(_run)


async def handle_graph_diff(
    project_path: str,
    since: str = "",
    since_hours: int | None = None,
) -> dict[str, Any]:
    """Return what changed in the graph since a given ISO timestamp or hours window."""
    import asyncio

    def _run() -> dict[str, Any]:
        gs = _open_graph(project_path)
        if gs is None:
            return {"error": "project not indexed or graph not built"}
        try:
            return gs.graph_diff(since_iso=since, since_hours=since_hours)
        finally:
            gs.close()

    return await asyncio.to_thread(_run)


async def handle_get_communities(
    project_path: str,
    top_k: int = 100,
) -> dict[str, Any]:
    """Return top Leiden communities for a project, ordered by size.

    Args:
        top_k: Maximum communities to return (default 100). Singleton communities
               (node_count == 1) are always excluded as they carry no structural
               information. Use a lower value on large projects to avoid timeouts.
    """
    import asyncio

    def _run() -> dict[str, Any]:
        gs = _open_graph(project_path)
        if gs is None:
            return {"communities": [], "total": 0, "error": "graph not built"}
        try:
            communities = gs.get_communities(
                limit=top_k,
                min_node_count=2,
                order_by_size=True,
            )
            result = []
            for c in communities:
                result.append({
                    "id": c.id,
                    "title": c.title,
                    "summary": c.summary,
                    "node_count": c.node_count,
                    "key_entry_points": c.key_entry_points,
                    "generated_at": c.generated_at,
                    "level": c.level,
                    "parent_community_id": c.parent_community_id,
                    "semantic_type": c.semantic_type,
                })
            god_nodes = gs.get_god_nodes(top_n=10)
            bridges = gs.get_cross_community_bridges(top_n=10)
            return {
                "communities": result,
                "total": len(result),
                "god_nodes": god_nodes,
                "cross_community_bridges": bridges,
            }
        finally:
            gs.close()

    return await asyncio.to_thread(_run)


async def handle_global_search(
    query: str,
    project_path: str,
    top_k: int = 10,
    include_federation: bool = False,
) -> dict[str, Any]:
    """Search across architectural knowledge: community summaries + wiki pages.

    Combines:
    - Community titles/summaries (fuzzy text match from graph DB)
    - Wiki pages (vector search via search_code filtered to wiki languages)

    Best for questions like 'which layer handles authentication?' or
    'where is the billing logic?'
    """
    import asyncio

    query_lower = query.lower()

    # Build the effective list of project paths (root + federation if requested)
    from opencode_search.config import load_registry
    registry = load_registry()
    effective_paths = [project_path]
    if include_federation:
        from opencode_search.handlers._federation import _expand_with_federation
        effective_paths = _expand_with_federation([project_path], registry)

    def _search_communities_for(path: str) -> list[dict[str, Any]]:
        gs = _open_graph(path)
        if gs is None:
            return []
        try:
            communities = gs.get_communities(
                limit=500, min_node_count=2, order_by_size=True
            )
            matches: list[dict[str, Any]] = []
            for c in communities:
                haystack = " ".join(filter(None, [c.title, c.summary])).lower()
                if not haystack:
                    continue
                tokens = [t for t in query_lower.split() if len(t) > 2]
                if not tokens:
                    score = 1.0 if query_lower in haystack else 0.0
                else:
                    score = sum(1 for t in tokens if t in haystack) / len(tokens)
                if score > 0:
                    matches.append({
                        "type": "community",
                        "id": c.id,
                        "title": c.title or f"Community {c.id}",
                        "summary": c.summary or "",
                        "node_count": c.node_count,
                        "key_entry_points": c.key_entry_points,
                        "score": round(score, 4),
                        "project_path": path,
                    })
            matches.sort(key=lambda x: x["score"] or 0.0, reverse=True)
            return matches[:top_k]
        finally:
            gs.close()

    async def _search_all_communities() -> list[dict[str, Any]]:
        # Parallel scatter-gather across all projects (federation-aware)
        per_project = await asyncio.gather(
            *[asyncio.to_thread(_search_communities_for, path) for path in effective_paths]
        )
        all_matches: list[dict[str, Any]] = []
        for hits in per_project:
            all_matches.extend(hits)
        all_matches.sort(key=lambda x: x["score"] or 0.0, reverse=True)
        return all_matches[:top_k]

    from opencode_search.handlers._wiki import handle_wiki_query

    async def _wiki_for_path(path: str) -> list[dict[str, Any]]:
        result = await handle_wiki_query(query=query, project_path=path, top_k=top_k)
        return [
            {
                "type": "wiki",
                "path": r["path"],
                "content": r["content"],
                "score": r["score"],
                "project_path": path,
            }
            for r in result.get("results", [])
        ]

    wiki_tasks = [_wiki_for_path(p) for p in effective_paths]
    community_hits_list, *wiki_hits_per_path = await asyncio.gather(
        _search_all_communities(),
        *wiki_tasks,
    )
    community_hits = community_hits_list
    wiki_hits: list[dict[str, Any]] = []
    for hits in wiki_hits_per_path:
        wiki_hits.extend(hits)
    wiki_hits.sort(key=lambda x: x.get("score") or 0.0, reverse=True)
    wiki_hits = wiki_hits[:top_k]

    all_hits: list[dict[str, Any]] = community_hits + wiki_hits
    all_hits.sort(key=lambda x: x["score"] or 0.0, reverse=True)

    return {
        "query": query,
        "results": all_hits[:top_k],
        "community_matches": len(community_hits),
        "wiki_matches": len(wiki_hits),
        "total": len(all_hits),
    }


async def handle_project_structure(
    project_path: str,
    max_depth: int = 4,
    include_graph_stats: bool = True,
) -> dict[str, Any]:
    """Return a structural overview of the project.

    Produces:
    - Directory tree (up to max_depth levels, skipping common noise dirs)
    - Top-level language breakdown (file counts per language)
    - Graph stats: node/edge/community counts from the code graph
    - Top communities (enriched, largest-first) as architectural anchors
    - Key entry points extracted from the largest communities
    """
    import os
    from collections import Counter

    root = Path(project_path).expanduser().resolve()
    if not root.is_dir():
        return {"error": f"Not a directory: {project_path}"}

    _SKIP_DIRS = {
        ".git", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache",
        ".pytest_cache", "dist", "build", "target", ".idea", ".vscode",
        "vendor", ".tox", "coverage", ".coverage", "htmlcoverage",
    }

    # Build directory tree
    def _tree(path: Path, depth: int, prefix: str = "") -> list[str]:
        if depth == 0:
            return []
        try:
            entries = sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        except PermissionError:
            return []
        lines: list[str] = []
        visible = [e for e in entries if e.name not in _SKIP_DIRS and not e.name.startswith(".")]
        for i, entry in enumerate(visible[:40]):  # cap dirs shown per level
            connector = "└── " if i == len(visible) - 1 or i == 39 else "├── "
            lines.append(f"{prefix}{connector}{entry.name}{'/' if entry.is_dir() else ''}")
            if entry.is_dir() and not entry.is_symlink():
                extension = "    " if i == len(visible) - 1 else "│   "
                lines.extend(_tree(entry, depth - 1, prefix + extension))
        return lines

    tree_lines = [f"{root.name}/", *_tree(root, max_depth)]
    tree_str = "\n".join(tree_lines[:200])  # cap output size

    # Language breakdown from file walk
    lang_counts: Counter = Counter()
    file_count = 0
    try:
        for _dirpath, dirnames, filenames in os.walk(root, followlinks=True):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
            for fname in filenames:
                ext = Path(fname).suffix.lower()
                lang_counts[ext] += 1
                file_count += 1
                if file_count > 100_000:
                    break
    except Exception:
        pass

    top_langs = [
        {"extension": ext, "count": cnt}
        for ext, cnt in lang_counts.most_common(15)
        if ext
    ]

    # Graph stats + top communities
    graph_stats: dict[str, Any] = {}
    top_communities: list[dict[str, Any]] = []

    if include_graph_stats:
        gs = _open_graph(project_path)
        if gs is not None:
            try:
                communities = gs.get_communities(limit=10, min_node_count=2, order_by_size=True)
                all_communities = gs.get_communities()
                enriched = sum(1 for c in all_communities if c.title)
                graph_stats = {
                    "total_communities": len(all_communities),
                    "enriched_communities": enriched,
                }
                top_communities = [
                    {
                        "id": c.id,
                        "title": c.title or f"Community {c.id}",
                        "summary": (c.summary or "")[:200],
                        "node_count": c.node_count,
                        "key_entry_points": c.key_entry_points[:3],
                    }
                    for c in communities
                ]
            finally:
                gs.close()

    return {
        "status": "ok",
        "project_path": str(root),
        "file_count": file_count,
        "directory_tree": tree_str,
        "language_breakdown": top_langs,
        "graph_stats": graph_stats,
        "top_communities": top_communities,
    }


async def handle_graph_export(
    project_path: str,
    format: str = "json",
    max_nodes: int = 5000,
    min_community_size: int = 2,
) -> dict[str, Any]:
    """Export the code knowledge graph for external visualization.

    Returns nodes, edges, and communities as JSON or GraphML suitable for
    Gephi, Cytoscape, NetworkX, or any graph analysis tool.

    format: "json" (default) | "graphml" | "mermaid"
    max_nodes: cap on nodes exported (largest communities first, default 5000)
    min_community_size: minimum node_count to include a community (default 2)
    """
    import asyncio

    gs = _open_graph(project_path)
    if gs is None:
        return {"error": f"Graph not built for {project_path}. Run build(action='index') first."}

    try:
        # Single SQL query: nodes from the largest communities, ordered by community size.
        # Much faster than N+1 get_community_nodes() calls on large graphs.
        def _fetch_nodes_and_comms() -> tuple[list[dict], list[dict], bool]:
            db = gs._db()
            rows = db.execute("""
                SELECT n.id, n.name, n.qualified_name, n.kind, n.file, n.language,
                       n.community_id, c.node_count, c.title, c.summary
                FROM nodes n
                JOIN communities c ON c.id = n.community_id
                WHERE c.node_count >= ?
                ORDER BY c.node_count DESC, n.community_id, n.id
                LIMIT ?
            """, (min_community_size, max_nodes + 1)).fetchall()
            truncated = len(rows) > max_nodes
            rows = rows[:max_nodes]
            nodes_out = [
                {
                    "id": r[0], "name": r[1], "qualified_name": r[2],
                    "kind": r[3], "file": r[4], "language": r[5],
                    "community_id": r[6],
                }
                for r in rows
            ]
            seen_comms: dict = {}
            for r in rows:
                cid = r[6]
                if cid not in seen_comms:
                    seen_comms[cid] = {"id": cid, "title": r[8], "summary": r[9], "node_count": r[7]}
            return nodes_out, list(seen_comms.values()), truncated

        nodes_out, communities_out, truncated = await asyncio.to_thread(_fetch_nodes_and_comms)
        node_id_set = {n["id"] for n in nodes_out}

        # Fetch only edges whose both endpoints are in the included node set.
        # Use SQL IN clause — avoids full table scan on large graphs.
        def _fetch_edges() -> list[dict]:
            if not node_id_set:
                return []
            db = gs._db()
            ids = list(node_id_set)
            ph = ",".join("?" * len(ids))
            edge_cols = {r[1] for r in db.execute("PRAGMA table_info(edges)").fetchall()}
            has_label = "confidence_label" in edge_cols
            select = "from_id, to_id, kind, confidence" + (", confidence_label, confidence_score" if has_label else "")
            rows = db.execute(
                f"SELECT {select} FROM edges WHERE from_id IN ({ph}) AND to_id IN ({ph})",
                ids + ids,
            ).fetchall()
            result = []
            for e in rows:
                rec = {"from": e[0], "to": e[1], "kind": e[2], "confidence": e[3]}
                if has_label:
                    rec["confidence_label"] = e[4]
                    rec["confidence_score"] = e[5]
                result.append(rec)
            return result

        edges_out = await asyncio.to_thread(_fetch_edges)

    finally:
        gs.close()

    if format == "graphml":
        graphml = _to_graphml(nodes_out, edges_out, communities_out)
        return {
            "status": "ok",
            "format": "graphml",
            "project_path": project_path,
            "nodes": len(nodes_out),
            "edges": len(edges_out),
            "communities": len(communities_out),
            "truncated": truncated,
            "max_nodes_limit": max_nodes,
            "graphml": graphml,
        }

    if format == "mermaid":
        diagram = _graph_to_mermaid(nodes_out, edges_out, communities_out)
        return {
            "status": "ok",
            "format": "mermaid",
            "project_path": project_path,
            "nodes": len(nodes_out),
            "edges": len(edges_out),
            "communities": len(communities_out),
            "truncated": truncated,
            "max_nodes_limit": max_nodes,
            "mermaid": diagram,
        }

    return {
        "status": "ok",
        "format": "json",
        "project_path": project_path,
        "nodes": nodes_out,
        "edges": edges_out,
        "communities": communities_out,
        "truncated": truncated,
        "max_nodes_limit": max_nodes,
        "stats": {
            "node_count": len(nodes_out),
            "edge_count": len(edges_out),
            "community_count": len(communities_out),
        },
    }


def _to_graphml(nodes: list[dict], edges: list[dict], communities: list[dict]) -> str:
    """Minimal GraphML serialization for Gephi/Cytoscape compatibility."""
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<graphml xmlns="http://graphml.graphdrawing.org/graphml">',
        '  <key id="name" for="node" attr.name="name" attr.type="string"/>',
        '  <key id="kind" for="node" attr.name="kind" attr.type="string"/>',
        '  <key id="file" for="node" attr.name="file" attr.type="string"/>',
        '  <key id="language" for="node" attr.name="language" attr.type="string"/>',
        '  <key id="community" for="node" attr.name="community_id" attr.type="int"/>',
        '  <key id="community_title" for="node" attr.name="community_title" attr.type="string"/>',
        '  <key id="edge_kind" for="edge" attr.name="kind" attr.type="string"/>',
        '  <graph id="G" edgedefault="directed">',
    ]

    # Build community title lookup
    comm_titles = {c["id"]: (c.get("title") or f"Community {c['id']}") for c in communities}

    def _esc(s: str) -> str:
        return str(s).replace("&", "&amp;").replace("<", "&lt;").replace('"', "&quot;")

    for n in nodes:
        comm_id = n.get("community_id", -1)
        title = comm_titles.get(comm_id, "")
        lines.append(
            f'    <node id="{_esc(str(n["id"]))}">'
            f'<data key="name">{_esc(n.get("name",""))}</data>'
            f'<data key="kind">{_esc(n.get("kind",""))}</data>'
            f'<data key="file">{_esc(n.get("file",""))}</data>'
            f'<data key="language">{_esc(n.get("language",""))}</data>'
            f'<data key="community">{comm_id}</data>'
            f'<data key="community_title">{_esc(title)}</data>'
            f'</node>'
        )

    for i, e in enumerate(edges):
        lines.append(
            f'    <edge id="e{i}" source="{_esc(str(e["from"]))}" target="{_esc(str(e["to"]))}">'
            f'<data key="edge_kind">{_esc(e.get("kind",""))}</data>'
            f'</edge>'
        )

    lines += ["  </graph>", "</graphml>"]
    return "\n".join(lines)


def _graph_to_mermaid(
    nodes: list[dict], edges: list[dict], communities: list[dict]
) -> str:
    """Convert full graph export to a Mermaid flowchart (community-grouped subgraphs)."""
    comm_title: dict[int, str] = {c["id"]: c.get("title", f"C{c['id']}") for c in communities}
    comm_nodes: dict[int, list[dict]] = {}
    orphan_nodes: list[dict] = []
    for n in nodes:
        cid = n.get("community_id")
        if cid is not None:
            comm_nodes.setdefault(cid, []).append(n)
        else:
            orphan_nodes.append(n)

    lines = ["flowchart TD"]
    node_ids = {n["id"] for n in nodes}

    for cid, cnodes in comm_nodes.items():
        safe = f"C{cid}"
        title = comm_title.get(cid, safe).replace('"', "'")[:40]
        lines.append(f'  subgraph {safe}["{title}"]')
        for n in cnodes:
            mid = _mermaid_id(n["id"])
            label = n.get("name", n["id"])[:30].replace('"', "'")
            lines.append(f'    {mid}["{label}"]')
        lines.append("  end")

    for n in orphan_nodes:
        mid = _mermaid_id(n["id"])
        label = n.get("name", n["id"])[:30].replace('"', "'")
        lines.append(f'  {mid}["{label}"]')

    for e in edges:
        if e["from"] in node_ids and e["to"] in node_ids:
            fid = _mermaid_id(e["from"])
            tid = _mermaid_id(e["to"])
            lines.append(f"  {fid} --> {tid}")

    return "\n".join(lines)


async def handle_callflow_html(
    symbol: str,
    project_path: str,
    *,
    direction: str = "callees",
    depth: int = 5,
    fmt: str = "html",
) -> dict[str, Any]:
    """Render a call chain as a Mermaid flowchart (HTML page or raw diagram text).

    direction: "callees" (default) = what the symbol calls downstream
               "callers" = who calls this symbol upstream
    depth:     BFS depth (default 5)
    fmt:       "html" (default standalone HTML) | "mermaid" (raw diagram text)
    """
    import asyncio

    def _run() -> dict[str, Any]:
        gs = _open_graph(project_path)
        if gs is None:
            return {"error": "project not indexed or graph not built", "project_path": project_path}
        try:
            root = gs.get_node(symbol)
            if root is None:
                return {"error": f"symbol '{symbol}' not found"}

            # BFS tracking parent edges so we can draw the actual call tree
            from collections import deque
            visited: set[str] = {root.id}
            edges_out: list[tuple[str, str]] = []  # (from_id, to_id)
            nodes_out: dict[str, Any] = {root.id: root}
            queue: deque[tuple[str, int]] = deque([(root.id, 0)])

            db = gs._db()
            while queue:
                nid, current_depth = queue.popleft()
                if current_depth >= depth:
                    continue
                if direction == "callees":
                    sql = "SELECT to_id FROM edges WHERE from_id=? AND kind='CALLS'"
                else:
                    sql = "SELECT from_id AS to_id FROM edges WHERE to_id=? AND kind='CALLS'"
                rows = db.execute(sql, (nid,)).fetchall()
                for r in rows:
                    child_id = r[0]
                    if child_id not in visited:
                        visited.add(child_id)
                        child_node = gs.get_node_by_id(child_id)
                        if child_node:
                            nodes_out[child_id] = child_node
                            queue.append((child_id, current_depth + 1))
                    if child_id in nodes_out or child_id == root.id:
                        if direction == "callees":
                            edges_out.append((nid, child_id))
                        else:
                            edges_out.append((child_id, nid))

            mermaid = _build_mermaid(root, nodes_out, edges_out, direction)
            if fmt == "mermaid":
                return {
                    "symbol": symbol,
                    "direction": direction,
                    "node_count": len(nodes_out),
                    "edge_count": len(edges_out),
                    "mermaid": mermaid,
                }
            html = _wrap_mermaid_html(symbol, direction, mermaid)
            return {
                "symbol": symbol,
                "direction": direction,
                "node_count": len(nodes_out),
                "edge_count": len(edges_out),
                "html": html,
                "mermaid": mermaid,
            }
        finally:
            gs.close()

    return await asyncio.to_thread(_run)


def _mermaid_id(node_id: str) -> str:
    """Safe Mermaid node ID (alphanumeric only)."""
    return "n" + node_id.replace("-", "").replace(".", "")[:16]


def _mermaid_label(node: Any) -> str:
    """Short display label: name (file:line)."""
    import os
    fname = os.path.basename(node.file) if node.file else ""
    line = f":{node.start_line}" if node.start_line else ""
    label = node.name or node.qualified_name
    if fname:
        return f"{label}\\n{fname}{line}"
    return label


def _build_mermaid(root: Any, nodes: dict, edges: list[tuple[str, str]], direction: str) -> str:
    """Produce a Mermaid `flowchart TD` diagram string."""
    arrow = "TD" if direction == "callees" else "BT"
    lines = [f"flowchart {arrow}"]

    # Root node — highlighted with double brackets
    rid = _mermaid_id(root.id)
    lines.append(f'    {rid}[["**{root.name}**"]]')
    lines.append(f"    style {rid} fill:#4a90d9,color:#fff,stroke:#2c5f8a")

    # Other nodes
    for nid, node in nodes.items():
        if nid == root.id:
            continue
        mid = _mermaid_id(nid)
        label = _mermaid_label(node)
        lines.append(f"    {mid}[{label!r}]")

    # Edges
    seen_edges: set[tuple[str, str]] = set()
    for from_id, to_id in edges:
        pair = (_mermaid_id(from_id), _mermaid_id(to_id))
        if pair in seen_edges:
            continue
        seen_edges.add(pair)
        lines.append(f"    {pair[0]} --> {pair[1]}")

    return "\n".join(lines)


_MERMAID_CDN = "https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.esm.min.mjs"

_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Callflow: {symbol}</title>
<style>
  body {{ font-family: sans-serif; background: #1a1a2e; color: #eee; margin: 0; padding: 20px; }}
  h1 {{ font-size: 1.2rem; color: #7ec8e3; margin-bottom: 4px; }}
  p.meta {{ font-size: 0.8rem; color: #888; margin: 0 0 20px; }}
  .mermaid {{ background: #fff; border-radius: 8px; padding: 20px; }}
</style>
</head>
<body>
<h1>Callflow: {symbol}</h1>
<p class="meta">{direction} · {node_count} nodes · {edge_count} edges</p>
<div class="mermaid">
{diagram}
</div>
<script type="module">
  import mermaid from '{cdn}';
  mermaid.initialize({{startOnLoad:true, theme:'default'}});
</script>
</body>
</html>"""


def _wrap_mermaid_html(symbol: str, direction: str, diagram: str) -> str:
    node_count = sum(1 for ln in diagram.splitlines() if ln.strip().startswith("n") and "[" in ln)
    edge_count = diagram.count("-->")
    return _HTML_TEMPLATE.format(
        symbol=symbol,
        direction=direction,
        node_count=node_count,
        edge_count=edge_count,
        diagram=diagram,
        cdn=_MERMAID_CDN,
    )


async def handle_dedup_nodes(
    project_path: str,
    *,
    threshold: float = 0.88,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Deduplicate graph nodes using MinHash/LSH + Jaro-Winkler (or exact-norm fallback).

    Finds nodes with nearly-identical qualified names in the same (file, kind) group
    and merges them, redirecting edges to the canonical node.
    """
    import asyncio

    def _run() -> dict[str, Any]:
        from opencode_search.graph.dedup import _FUZZY_AVAILABLE, DedupResult, dedup_nodes

        gs = _open_graph(project_path)
        if gs is None:
            return {"error": "project not indexed or graph not built", "project_path": project_path}
        try:
            result: DedupResult = dedup_nodes(gs, threshold=threshold, dry_run=dry_run)
            return {
                "project_path": project_path,
                "strategy": result.strategy,
                "merged_count": result.merged_count,
                "candidate_pairs_checked": result.candidate_pairs_checked,
                "skipped_low_entropy": result.skipped_low_entropy,
                "dry_run": dry_run,
                "fuzzy_available": _FUZZY_AVAILABLE,
                "merged_pairs": result.merged_pairs[:50],  # cap output size
                "errors": result.errors,
            }
        finally:
            gs.close()

    return await asyncio.to_thread(_run)
