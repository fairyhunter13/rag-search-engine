"""_tree_html.py — Interactive collapsible file tree HTML from the graph's file nodes."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any


async def handle_tree_html(
    project_path: str,
    *,
    fmt: str = "html",
    max_files: int = 2000,
) -> dict[str, Any]:
    """Generate an interactive collapsible file tree from indexed file nodes.

    fmt: "html" (default) — standalone HTML page with expand/collapse JS
         "json" — raw tree dict structure
    max_files: cap on files shown (largest directories first, default 2000)
    """
    from opencode_search.handlers._graph import _open_graph

    def _run() -> dict[str, Any]:
        gs = _open_graph(project_path)
        if gs is None:
            return {"error": f"Graph not built for {project_path}. Run build(action='index') first."}

        try:
            db = gs._db()
            rows = db.execute(
                "SELECT file, COUNT(*) AS cnt FROM nodes WHERE file IS NOT NULL AND file != '' "
                "GROUP BY file ORDER BY cnt DESC LIMIT ?",
                (max_files,),
            ).fetchall()
        finally:
            gs.close()

        files = [r[0] for r in rows]
        if not files:
            return {"error": "No file nodes in graph. Run build(action='index') first."}

        # Find common root to make paths relative
        root = _common_root(files) or project_path
        rel_files = []
        for f in files:
            try:
                rel_files.append(Path(f).relative_to(root))
            except ValueError:
                rel_files.append(Path(f))

        tree = _build_tree(rel_files)

        if fmt == "json":
            return {"status": "ok", "project_path": project_path, "root": root, "tree": tree}

        html = _emit_html(tree, project_path, root)
        return {"status": "ok", "project_path": project_path, "html": html}

    return await asyncio.to_thread(_run)


def _common_root(paths: list[str]) -> str:
    if not paths:
        return ""
    parts_list = [Path(p).parts for p in paths]
    common: list[str] = []
    for parts in zip(*parts_list, strict=False):
        if len(set(parts)) == 1:
            common.append(parts[0])
        else:
            break
    return str(Path(*common)) if common else ""


def _build_tree(rel_paths: list[Path]) -> dict:
    root: dict = {}
    for p in rel_paths:
        node = root
        for part in p.parts[:-1]:
            node = node.setdefault(part, {})
        node[p.name] = None  # leaf = file
    return root


def _emit_html(tree: dict, project_path: str, root: str) -> str:
    proj_name = Path(project_path).name
    tree_html = _render_node(tree, depth=0)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>File Tree — {proj_name}</title>
<style>
  body{{font-family:monospace;font-size:13px;background:#1e1e2e;color:#cdd6f4;margin:0;padding:16px}}
  h2{{color:#89b4fa;margin-bottom:8px}}
  .root{{color:#a6adc8;font-size:11px;margin-bottom:16px;word-break:break-all}}
  ul{{list-style:none;padding-left:18px;margin:0}}
  li{{line-height:1.7}}
  .dir{{cursor:pointer;user-select:none}}
  .dir::before{{content:"▶ ";color:#f38ba8;font-size:10px}}
  .dir.open::before{{content:"▼ ";color:#a6e3a1}}
  .dir-name{{color:#89dceb;font-weight:bold}}
  .file{{color:#cdd6f4}}
  .file::before{{content:"  ";}}
  .hidden{{display:none}}
  .badge{{background:#313244;color:#a6adc8;border-radius:4px;padding:1px 5px;font-size:10px;margin-left:6px}}
</style>
</head>
<body>
<h2>📁 {proj_name}</h2>
<div class="root">Root: {root}</div>
<ul id="tree">{tree_html}</ul>
<script>
document.querySelectorAll('.dir').forEach(function(el){{
  el.addEventListener('click',function(e){{
    e.stopPropagation();
    this.classList.toggle('open');
    var ul=this.nextElementSibling;
    if(ul){{ul.classList.toggle('hidden');}}
  }});
}});
</script>
</body>
</html>"""


def _render_node(node: dict, depth: int) -> str:
    parts = []
    dirs = sorted(k for k, v in node.items() if v is not None)
    files = sorted(k for k, v in node.items() if v is None)

    for dname in dirs:
        child_count = _count_leaves(node[dname])
        inner = _render_node(node[dname], depth + 1)
        hidden = " hidden" if depth > 0 else ""
        parts.append(
            f'<li><span class="dir"><span class="dir-name">{dname}/</span>'
            f'<span class="badge">{child_count}</span></span>'
            f'<ul class="{hidden}">{inner}</ul></li>'
        )
    for fname in files:
        parts.append(f'<li><span class="file">{fname}</span></li>')
    return "".join(parts)


def _count_leaves(node: dict) -> int:
    count = 0
    for v in node.values():
        if v is None:
            count += 1
        else:
            count += _count_leaves(v)
    return count
