"""Graph MCP handlers: symbol lookup, call traversal, impact analysis."""
from __future__ import annotations

import collections
import json
import logging
import re
import xml.etree.ElementTree as _ET
from pathlib import Path
from typing import TYPE_CHECKING, Any

from opencode_search.config import get_project_graph_db_path

if TYPE_CHECKING:
    from opencode_search.graph.storage import GraphStorage

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pattern-detection helpers (called from handle_project_structure)
# ---------------------------------------------------------------------------

_FRAMEWORK_MAP: dict[str, str] = {
    "github.com/gin-gonic/gin": "gin",
    "github.com/labstack/echo": "echo",
    "github.com/gorilla/mux": "gorilla/mux",
    "github.com/go-chi/chi": "chi",
    "google.golang.org/grpc": "gRPC",
    "gorm.io/gorm": "gorm",
    "github.com/jmoiron/sqlx": "sqlx",
    "github.com/go-redis/redis": "redis",
    "github.com/redis/go-redis": "redis",
    "github.com/confluentinc/confluent-kafka-go": "kafka",
    "github.com/segmentio/kafka-go": "kafka",
    "github.com/elastic/go-elasticsearch": "elasticsearch",
    "github.com/prometheus/client_golang": "prometheus",
    "go.uber.org/zap": "zap",
    "github.com/sirupsen/logrus": "logrus",
    "go.opentelemetry.io": "OpenTelemetry",
    "github.com/spf13/cobra": "cobra",
    "github.com/spf13/viper": "viper",
    "react": "React",
    "next": "Next.js",
    "vue": "Vue",
    "angular": "Angular",
    "svelte": "Svelte",
    "django": "Django",
    "flask": "Flask",
    "fastapi": "FastAPI",
    "spring": "Spring",
    "javax.persistence": "JPA",
    "hibernate": "Hibernate",
    "lombok": "Lombok",
    "sqlalchemy": "SQLAlchemy",
    "pydantic": "Pydantic",
}


def _detect_dependencies(root: Path) -> dict[str, Any]:
    """Parse dependency manifest files to extract package names and versions."""
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
                line = line.strip()
                if line.startswith("require ("):
                    in_require = True
                    continue
                if in_require and line == ")":
                    in_require = False
                    continue
                if in_require or line.startswith("require "):
                    cleaned = line.removeprefix("require ").strip()
                    if cleaned.startswith("(") or not cleaned:
                        continue
                    # github.com/pkg/name v1.2.3 // indirect
                    m = re.match(r"^(\S+)\s+(v\S+)(.*)$", cleaned)
                    if m:
                        indirect = "indirect" in m.group(3)
                        packages.append({"name": m.group(1), "version": m.group(2), "direct": not indirect})
        except Exception:
            pass

    def _try_requirements(p: Path) -> None:
        nonlocal manager
        try:
            if manager == "unknown":
                manager = "pip"
            for line in p.read_text(errors="replace").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or line.startswith("-"):
                    continue
                m = re.match(r"^([A-Za-z0-9_\-\.]+)\s*[=><!~]{1,2}=?\s*([^\s;#]+)", line)
                if m:
                    packages.append({"name": m.group(1), "version": m.group(2), "direct": True})
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
            if manager == "unknown":
                manager = "cargo"
            text = p.read_text(errors="replace")
            in_deps = False
            for line in text.splitlines():
                stripped = line.strip()
                if stripped.startswith("[dependencies]") or stripped.startswith("[dev-dependencies]"):
                    in_deps = True
                    continue
                if stripped.startswith("[") and in_deps:
                    in_deps = False
                if in_deps and "=" in stripped and not stripped.startswith("#"):
                    name, _, rest = stripped.partition("=")
                    name = name.strip()
                    rest = rest.strip().strip('"').strip("'")
                    # version = { version = "1.0" } or version = "1.0"
                    vm = re.search(r'"([^"]+)"', rest)
                    if vm and name:
                        packages.append({"name": name, "version": vm.group(1), "direct": True})
        except Exception:
            pass

    def _try_pyproject(p: Path) -> None:
        nonlocal manager
        try:
            text = p.read_text(errors="replace")
            if manager == "unknown":
                manager = "poetry" if "[tool.poetry]" in text else "pip"
            # PEP 621: [project] dependencies = [...]
            in_deps = False
            for line in text.splitlines():
                stripped = line.strip()
                if stripped in ('[project.dependencies]', 'dependencies = ['):
                    in_deps = True
                    continue
                if in_deps:
                    if stripped.startswith("[") or stripped == "]":
                        in_deps = False
                        continue
                    m = re.match(r'"?([A-Za-z0-9_\-\.]+)\s*[>=<!~]{0,2}=?\s*([^",\]]*)', stripped.strip('"').strip("'"))
                    if m and m.group(1):
                        packages.append({"name": m.group(1), "version": m.group(2).strip() or "*", "direct": True})
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
            text = p.read_text(errors="replace")
            # Spring Boot version from plugin block (handles both quote styles)
            m = re.search(r"['\"']org\.springframework\.boot['\"].*?version\s+['\"]([^'\"]+)['\"]", text)
            if not m:
                m = re.search(r"org\.springframework\.boot['\"]?\s+version\s+['\"]([^'\"]+)['\"]", text)
            if m:
                packages.append({"name": "org.springframework.boot", "version": m.group(1), "direct": True})
            # Standard dependency declarations
            in_deps = False
            brace_depth = 0
            for line in text.splitlines():
                stripped = line.strip()
                if re.match(r"dependencies\s*\{", stripped):
                    in_deps = True
                    brace_depth = 1
                    continue
                if in_deps:
                    brace_depth += stripped.count("{") - stripped.count("}")
                    if brace_depth <= 0:
                        in_deps = False
                        continue
                    if stripped.startswith("//"):
                        continue
                    dep_m = re.search(
                        r"['\"]([a-zA-Z0-9][\w.\-]*:[a-zA-Z0-9][\w.\-]*):([^'\"]+)['\"]",
                        stripped,
                    )
                    if dep_m:
                        packages.append({
                            "name": dep_m.group(1),
                            "version": dep_m.group(2).strip(),
                            "direct": True,
                        })
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


_DOC_LANGS = frozenset({"markdown", "text", "unknown"})


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
            if lang in _DOC_LANGS:
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


def _detect_conventions(root: Path) -> dict[str, Any]:
    """Detect code style conventions by sampling source files."""
    try:
        from opencode_search.discover import detect_language, iter_files, SOURCE_EXTENSIONS

        # Prefer main application source files; skip SQL/migrations/generated code
        _PREFERRED_EXTS = {'.go', '.py', '.java', '.kt', '.ts', '.tsx', '.js', '.jsx',
                           '.rs', '.rb', '.cs', '.swift', '.scala', '.cpp', '.c'}
        sample_files: list[Path] = []
        overflow_files: list[Path] = []
        for path in iter_files(root):
            ext = path.suffix.lower()
            if ext not in SOURCE_EXTENSIONS:
                continue
            if ext in _PREFERRED_EXTS and len(sample_files) < 40:
                sample_files.append(path)
            elif len(overflow_files) < 10:
                overflow_files.append(path)
            if len(sample_files) >= 40:
                break
        if not sample_files:
            sample_files = overflow_files

        if not sample_files:
            return {}

        # Detect primary language
        lang_counter: collections.Counter = collections.Counter()
        for p in sample_files:
            lang_counter[detect_language(p)] += 1
        primary_lang = lang_counter.most_common(1)[0][0] if lang_counter else "unknown"

        # Read samples for heuristics (800 chars captures package + imports + first functions)
        combined = ""
        for p in sample_files[:20]:
            try:
                combined += p.read_text(errors="replace")[:800]
            except Exception:
                pass

        # Error handling
        err_handling = "unknown"
        if primary_lang == "go":
            if "if err != nil" in combined:
                err_handling = "if_err_nil"
            elif re.search(r"errors\.As|errors\.Is", combined):
                err_handling = "errors_as_is"
            elif "Result[" in combined or "errors.Wrap(" in combined:
                err_handling = "wrapped_errors"
        elif primary_lang == "python":
            err_handling = "try_except" if "try:" in combined else "unknown"
        elif primary_lang in ("java", "kotlin"):
            err_handling = "try_catch" if "try {" in combined else "unknown"
        elif primary_lang == "rust":
            err_handling = "result_type" if "Result<" in combined else "unknown"

        # Test style
        test_style = "unknown"
        if primary_lang == "go":
            if "t.Run(" in combined:
                test_style = "table_driven"
            elif "testify" in combined or "assert.Equal(" in combined or "require.Equal(" in combined:
                test_style = "testify"
            elif "func Test" in combined:
                test_style = "stdlib_testing"
        elif primary_lang == "python":
            test_style = "pytest" if "def test_" in combined else "unknown"
        elif primary_lang == "java":
            test_style = "junit" if "@Test" in combined else "unknown"

        # Logging library
        logging_lib = "unknown"
        if "go.uber.org/zap" in combined or '"zap"' in combined or "zap.L()." in combined:
            logging_lib = "zap"
        elif "logrus" in combined:
            logging_lib = "logrus"
        elif "slog." in combined:
            logging_lib = "slog"
        elif "log.Printf(" in combined or "log.Println(" in combined or "log.Fatal(" in combined:
            logging_lib = "stdlib_log"
        elif "logging." in combined:
            logging_lib = "python_logging"
        elif "zerolog" in combined:
            logging_lib = "zerolog"

        # Naming convention (sample identifier names)
        naming = "unknown"
        if primary_lang == "go":
            naming = "camelCase"  # Go always uses camelCase/PascalCase
        elif primary_lang == "python":
            naming = "snake_case"  # Python convention
        elif primary_lang in ("java", "kotlin"):
            naming = "camelCase"

        # Struct/annotation tags
        common_tags: list[str] = []
        if 'json:"' in combined:
            common_tags.append("json")
        if 'db:"' in combined or 'column:"' in combined:
            common_tags.append("db")
        if 'validate:"' in combined:
            common_tags.append("validate")
        if 'yaml:"' in combined:
            common_tags.append("yaml")
        if '@JsonProperty' in combined or '@Column' in combined:
            common_tags.append("java_annotations")

        return {
            "language": primary_lang,
            "error_handling": err_handling,
            "test_style": test_style,
            "logging_lib": logging_lib,
            "naming": naming,
            "common_struct_tags": common_tags,
        }
    except Exception:
        return {}


def _detect_frameworks_from_dependencies(deps: dict[str, Any]) -> list[str]:
    """Identify key frameworks from dependency manifest packages."""
    packages = deps.get("packages", [])
    if not packages:
        return []
    frameworks: list[str] = []
    seen: set[str] = set()
    for pkg in packages:
        name = pkg.get("name", "")
        for prefix, framework in _FRAMEWORK_MAP.items():
            if prefix in name and framework not in seen:
                seen.add(framework)
                frameworks.append(framework)
    return frameworks[:10]


def _detect_module_structure(root: Path) -> dict[str, Any]:
    """Detect the module/package organization pattern from directory layout."""
    try:
        top_dirs = sorted(
            [d.name for d in root.iterdir() if d.is_dir() and not d.name.startswith(".") and d.name not in {
                ".git", "node_modules", "__pycache__", ".venv", "venv", "target", "dist", "build",
            }]
        )

        # Detect known patterns
        dir_set = set(top_dirs)
        pattern = "unknown"
        if {"cmd", "internal"}.issubset(dir_set) or {"cmd", "pkg"}.issubset(dir_set):
            pattern = "go_standard"
        elif {"domain", "usecase", "infrastructure"}.issubset(dir_set) or {"domain", "usecase", "interface"}.issubset(dir_set):
            pattern = "clean_architecture"
        elif {"controller", "service", "repository"}.issubset(dir_set) or {"controllers", "services", "models"}.issubset(dir_set):
            pattern = "layered_mvc"
        elif "features" in dir_set or "modules" in dir_set:
            pattern = "feature_sliced"
        elif len(top_dirs) > 8 and all((root / d).is_dir() for d in top_dirs):
            pattern = "monorepo"
        elif {"src", "lib", "test"}.issubset(dir_set) or "src" in dir_set:
            pattern = "src_layout"

        # Top packages: second-level directories of important top-level dirs
        top_packages: list[str] = []
        priority_dirs = [d for d in ("internal", "src", "lib", "pkg", "app") if d in dir_set]
        for pdir in priority_dirs[:3]:
            sub = root / pdir
            try:
                for sd in sorted(sub.iterdir()):
                    if sd.is_dir() and not sd.name.startswith("."):
                        top_packages.append(f"{pdir}/{sd.name}")
            except Exception:
                pass
        if not top_packages:
            top_packages = [d for d in top_dirs[:8]]

        return {
            "type": pattern,
            "top_packages": top_packages[:10],
            "detected_dirs": top_dirs[:20],
        }
    except Exception:
        return {"type": "unknown", "top_packages": [], "detected_dirs": []}


def _detect_architecture(frameworks: list[str], module_structure: dict[str, Any]) -> str:
    """Synthesize a high-level architecture label from detected frameworks and structure."""
    struct_type = module_structure.get("type", "unknown")
    fw_lower = {f.lower() for f in frameworks}

    has_grpc = "grpc" in fw_lower
    has_spring = any("spring" in f for f in fw_lower)
    has_proto = "protobuf" in fw_lower
    has_react = "react" in fw_lower or "next.js" in fw_lower

    if struct_type == "monorepo":
        return "microservices_federation" if (has_grpc or has_proto) else "monorepo"
    if struct_type == "clean_architecture":
        return "clean_architecture_grpc_microservice" if has_grpc else "clean_architecture_ddd"
    if struct_type == "go_standard":
        return "go_grpc_service" if has_grpc else "go_standard"
    if struct_type == "layered_mvc":
        return "spring_boot_mvc" if has_spring else "layered_mvc"
    if has_react:
        return "frontend_spa"
    return struct_type if struct_type != "unknown" else "unknown"


async def handle_detect_patterns(project_path: str) -> dict[str, Any]:
    """Detect code style, architecture, dependencies, and module organization.

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

    def _run() -> dict[str, Any]:
        languages = _count_languages_accurate(root, project_path)
        dependencies = _detect_dependencies(root)
        conventions = _detect_conventions(root)
        key_frameworks = _detect_frameworks_from_dependencies(dependencies)
        module_structure = _detect_module_structure(root)
        architecture = _detect_architecture(key_frameworks, module_structure)

        package_versions: dict[str, str] = {}
        for pkg in dependencies.get("packages", []):
            name = pkg.get("name", "")
            ver = pkg.get("version", "")
            if name and ver and name not in package_versions:
                package_versions[name] = ver

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
            "conventions": conventions,
            "key_frameworks": key_frameworks,
            "module_structure": module_structure,
            "architecture": architecture,
        }

    result = await asyncio.to_thread(_run)

    # Merge cached LLM analysis if available (non-blocking — never slows the fast path)
    try:
        from opencode_search.handlers._patterns import load_patterns_cache
        cached = load_patterns_cache(project_path)
        if cached:
            result["llm_analysis"] = cached.get("llm_analysis")
            result["llm_cached_at"] = cached.get("cached_at")
        else:
            result["llm_analysis"] = None
            result["llm_cached_at"] = None
    except Exception:
        result["llm_analysis"] = None
        result["llm_cached_at"] = None

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
                })
            return {"communities": result, "total": len(result)}
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

    def _search_all_communities() -> list[dict[str, Any]]:
        all_matches: list[dict[str, Any]] = []
        for path in effective_paths:
            all_matches.extend(_search_communities_for(path))
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
        asyncio.to_thread(_search_all_communities),
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

    tree_lines = [f"{root.name}/"] + _tree(root, max_depth)
    tree_str = "\n".join(tree_lines[:200])  # cap output size

    # Language breakdown from file walk
    lang_counts: Counter = Counter()
    file_count = 0
    try:
        for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
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

    format: "json" (default) | "graphml"
    max_nodes: cap on nodes exported (largest communities first, default 5000)
    min_community_size: minimum node_count to include a community (default 2)
    """
    import asyncio

    gs = _open_graph(project_path)
    if gs is None:
        return {"error": f"Graph not built for {project_path}. Run build(action='index') first."}

    try:
        communities = await asyncio.to_thread(
            gs.get_communities, None, min_community_size, True  # limit=None, order_by_size=True
        )
        # Collect nodes from largest communities until we hit max_nodes
        included_node_ids: set = set()
        selected_communities = []
        for c in communities:
            if len(included_node_ids) >= max_nodes:
                break
            nodes = await asyncio.to_thread(gs.get_community_nodes, c.id)
            for n in nodes:
                included_node_ids.add(n.id)
            selected_communities.append((c, nodes))

        # Build full node + edge lists for included nodes
        all_nodes = await asyncio.to_thread(gs.all_nodes)
        all_edges = await asyncio.to_thread(gs.all_edges)

        nodes_out = [
            {
                "id": n.id,
                "name": n.name,
                "qualified_name": n.qualified_name,
                "kind": n.kind,
                "file": n.file,
                "language": n.language,
                "community_id": n.community_id,
            }
            for n in all_nodes if n.id in included_node_ids
        ]

        edges_out = [
            {"from": e.from_id, "to": e.to_id, "kind": e.kind}
            for e in all_edges
            if e.from_id in included_node_ids and e.to_id in included_node_ids
        ]

        communities_out = [
            {
                "id": c.id,
                "title": c.title,
                "summary": c.summary,
                "node_count": c.node_count,
            }
            for c, _ in selected_communities
        ]

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
            "graphml": graphml,
        }

    return {
        "status": "ok",
        "format": "json",
        "project_path": project_path,
        "nodes": nodes_out,
        "edges": edges_out,
        "communities": communities_out,
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
