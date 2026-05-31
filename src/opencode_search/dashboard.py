"""Mini dashboard — read-only browser view of everything the engine produces.

Registers routes on the existing FastMCP Starlette app (no new server/port).
Import this module in mcp.py to attach routes:  from opencode_search import dashboard

Routes:
  GET /dashboard                        — single-page HTML app
  GET /api/projects                     — list all indexed projects
  GET /api/overview?project=…           — directory tree + language breakdown + graph stats
  GET /api/communities?project=…&top_k= — enriched code clusters (knowledge semantics)
  GET /api/wiki?project=…               — wiki page list
  GET /api/wiki/page?project=…&name=…   — wiki page content (markdown)
  GET /api/ask?project=…&q=…&scope=     — architecture/wiki search
  GET /api/search?project=…&q=…         — code search
  GET /api/graph?project=…&symbol=…&relation= — callers/callees/impact/trace
  GET /api/federation?project=…         — federation member list
  GET /api/metrics                      — daemon session statistics
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# HTML template (single self-contained page, no build step, no CDN)
# ---------------------------------------------------------------------------

_DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>opencode-search dashboard</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:system-ui,sans-serif;background:#0f1117;color:#e2e8f0;height:100vh;display:flex;flex-direction:column}
header{background:#1a1d2e;padding:12px 20px;border-bottom:1px solid #2d3048;display:flex;align-items:center;gap:16px}
header h1{font-size:1.1rem;font-weight:600;color:#7c9fff}
header .status{font-size:.75rem;color:#64748b}
header .ok{color:#4ade80}
#project-select{margin-left:auto;background:#0f1117;color:#e2e8f0;border:1px solid #2d3048;border-radius:6px;padding:4px 10px;font-size:.85rem}
nav{background:#1a1d2e;border-bottom:1px solid #2d3048;display:flex;gap:2px;padding:0 12px}
nav button{background:none;border:none;color:#94a3b8;padding:10px 16px;font-size:.82rem;cursor:pointer;border-bottom:2px solid transparent}
nav button.active{color:#7c9fff;border-bottom-color:#7c9fff}
nav button:hover{color:#e2e8f0}
main{flex:1;overflow:auto;padding:20px}
.card{background:#1a1d2e;border:1px solid #2d3048;border-radius:8px;padding:16px;margin-bottom:14px}
.card h2{font-size:.85rem;font-weight:600;color:#94a3b8;text-transform:uppercase;letter-spacing:.05em;margin-bottom:12px}
table{width:100%;border-collapse:collapse;font-size:.82rem}
th{text-align:left;padding:6px 10px;color:#64748b;border-bottom:1px solid #2d3048}
td{padding:6px 10px;border-bottom:1px solid #1e2234}
tr:hover td{background:#1e2234}
.badge{display:inline-block;padding:2px 8px;border-radius:99px;font-size:.7rem;font-weight:600}
.badge.ok{background:#064e3b;color:#4ade80}
.badge.warn{background:#451a03;color:#fb923c}
.badge.none{background:#1e2234;color:#64748b}
pre{background:#0a0c14;border:1px solid #2d3048;border-radius:6px;padding:14px;font-size:.78rem;overflow:auto;max-height:420px;white-space:pre-wrap}
.search-row{display:flex;gap:8px;margin-bottom:14px}
.search-row input{flex:1;background:#0f1117;border:1px solid #2d3048;border-radius:6px;color:#e2e8f0;padding:8px 12px;font-size:.85rem}
.search-row select{background:#0f1117;border:1px solid #2d3048;border-radius:6px;color:#e2e8f0;padding:8px 10px;font-size:.85rem}
.search-row button{background:#2d3560;color:#7c9fff;border:none;border-radius:6px;padding:8px 18px;cursor:pointer;font-size:.85rem}
.search-row button:hover{background:#3d4570}
.result-item{margin-bottom:10px;border-left:2px solid #2d3048;padding-left:12px}
.result-item .path{font-size:.75rem;color:#64748b;margin-bottom:4px}
.result-item .score{float:right;font-size:.72rem;color:#4ade80}
.result-item pre{max-height:120px}
.wiki-list{list-style:none}
.wiki-list li{padding:6px 0;border-bottom:1px solid #1e2234}
.wiki-list li a{color:#7c9fff;cursor:pointer;text-decoration:none;font-size:.83rem}
.wiki-list li a:hover{text-decoration:underline}
.progress-bar{background:#0f1117;border-radius:4px;height:8px;overflow:hidden;margin-top:6px}
.progress-fill{height:100%;background:#3b82f6;transition:width .3s}
.stat-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:10px}
.stat-box{background:#0f1117;border:1px solid #2d3048;border-radius:6px;padding:12px}
.stat-box .val{font-size:1.4rem;font-weight:700;color:#7c9fff}
.stat-box .lbl{font-size:.72rem;color:#64748b;margin-top:2px}
.tree{font-family:monospace;font-size:.78rem;white-space:pre;color:#94a3b8}
.lang-bar{display:flex;align-items:center;gap:8px;margin-bottom:4px}
.lang-bar .name{width:60px;font-size:.75rem;color:#94a3b8;text-align:right}
.lang-bar .bar{flex:1;background:#0f1117;border-radius:3px;height:6px}
.lang-bar .fill{height:100%;background:#3b82f6;border-radius:3px}
.lang-bar .count{width:40px;font-size:.72rem;color:#64748b}
.graph-tree{font-family:monospace;font-size:.78rem;line-height:1.6;color:#94a3b8}
.graph-tree .symbol{color:#7c9fff}
#wiki-content h1,#wiki-content h2,#wiki-content h3{margin:1em 0 .5em;color:#7c9fff}
#wiki-content p{margin:.5em 0;line-height:1.6;font-size:.85rem;color:#cbd5e1}
#wiki-content ul,#wiki-content ol{margin:.5em 0 .5em 1.5em;font-size:.85rem;color:#cbd5e1}
#wiki-content code{background:#0a0c14;padding:1px 5px;border-radius:3px;font-size:.82em}
#wiki-content pre{margin:.5em 0}
.tab{display:none}.tab.active{display:block}
.loader{color:#64748b;padding:20px;text-align:center;font-size:.82rem}
</style>
</head>
<body>
<header>
  <h1>opencode-search</h1>
  <span class="status" id="daemon-status">connecting…</span>
  <select id="project-select" onchange="switchProject(this.value)"><option value="">Loading projects…</option></select>
</header>
<nav>
  <button class="active" onclick="showTab('projects',this)">Projects</button>
  <button onclick="showTab('structure',this)">Structure</button>
  <button onclick="showTab('architecture',this)">Architecture</button>
  <button onclick="showTab('graph',this)">Graph / Trace</button>
  <button onclick="showTab('wiki',this)">Wiki / KB</button>
  <button onclick="showTab('search',this)">Search</button>
  <button onclick="showTab('status',this)">Status</button>
</nav>
<main>

<!-- PROJECTS TAB -->
<div id="tab-projects" class="tab active">
  <div class="card"><h2>Indexed Projects</h2>
    <div id="projects-table"><div class="loader">Loading…</div></div>
  </div>
</div>

<!-- STRUCTURE TAB -->
<div id="tab-structure" class="tab">
  <div class="card"><h2>Directory Tree</h2><pre id="structure-tree" class="tree">Select a project…</pre></div>
  <div class="card"><h2>Language Breakdown</h2><div id="lang-breakdown"></div></div>
  <div class="card"><h2>Graph Stats</h2><div id="graph-stats" class="stat-grid"></div></div>
</div>

<!-- ARCHITECTURE TAB -->
<div id="tab-architecture" class="tab">
  <div class="card"><h2>Knowledge Semantics — Top Communities</h2>
    <div id="enrichment-progress" style="margin-bottom:14px"></div>
    <div id="communities-list"></div>
  </div>
</div>

<!-- GRAPH TAB -->
<div id="tab-graph" class="tab">
  <div class="card"><h2>Code Graph / Tracing</h2>
    <div class="search-row">
      <input id="graph-symbol" placeholder="Symbol name (e.g. http.Run)"/>
      <select id="graph-relation">
        <option value="definition">definition</option>
        <option value="callers">callers</option>
        <option value="callees">callees</option>
        <option value="impact">impact</option>
        <option value="path">path (enter to_symbol below)</option>
      </select>
      <input id="graph-to" placeholder="to_symbol (for path only)" style="max-width:220px"/>
      <button onclick="runGraph()">Run</button>
    </div>
    <pre id="graph-result">Enter a symbol above…</pre>
  </div>
  <div class="card"><h2>Knowledge Graph Export</h2>
    <p style="font-size:.8rem;color:#64748b;margin-bottom:12px">Export nodes, edges, and communities for external visualization (Gephi, Cytoscape, NetworkX). Up to 5,000 nodes from the largest communities.</p>
    <div style="display:flex;gap:8px">
      <button onclick="exportGraph('json')" style="background:#0d4429;color:#4ade80;border:none;border-radius:6px;padding:8px 16px;cursor:pointer;font-size:.82rem">⬇ Export JSON</button>
      <button onclick="exportGraph('graphml')" style="background:#1e3a5f;color:#7c9fff;border:none;border-radius:6px;padding:8px 16px;cursor:pointer;font-size:.82rem">⬇ Export GraphML</button>
    </div>
  </div>
</div>

<!-- WIKI TAB -->
<div id="tab-wiki" class="tab">
  <div class="card"><h2>Wiki Search</h2>
    <div class="search-row">
      <input id="wiki-search-q" placeholder="Ask an architectural question…"/>
      <select id="wiki-scope"><option value="all">all</option><option value="wiki">wiki only</option><option value="architecture">architecture only</option></select>
      <button onclick="runWikiSearch()">Ask</button>
    </div>
    <div id="wiki-search-results"></div>
  </div>
  <div class="card" style="display:flex;gap:16px">
    <div style="width:240px;flex-shrink:0">
      <h2 style="margin-bottom:10px">Pages</h2>
      <ul id="wiki-page-list" class="wiki-list"><li style="color:#64748b;font-size:.82rem">Loading…</li></ul>
    </div>
    <div style="flex:1;overflow:auto">
      <h2 style="margin-bottom:10px">Page Content</h2>
      <div id="wiki-content" style="color:#94a3b8;font-size:.82rem">Click a page to view it.</div>
    </div>
  </div>
</div>

<!-- SEARCH TAB -->
<div id="tab-search" class="tab">
  <div class="card"><h2>Code Search</h2>
    <div class="search-row">
      <input id="search-q" placeholder="Search code, functions, patterns…"/>
      <select id="search-scope"><option value="code">code</option><option value="docs">docs</option><option value="all">all</option></select>
      <button onclick="runSearch()">Search</button>
    </div>
    <div id="search-results"></div>
  </div>
</div>

<!-- STATUS TAB -->
<div id="tab-status" class="tab">
  <div class="card"><h2>Daemon Status</h2><div id="daemon-metrics" class="stat-grid"></div></div>
  <div class="card"><h2>Search Metrics</h2><pre id="search-metrics">Loading…</pre></div>
</div>

</main>
<script>
let currentProject = '';
const $ = id => document.getElementById(id);

async function api(path) {
  const r = await fetch('/api' + path);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

function showTab(name, btn) {
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('nav button').forEach(b => b.classList.remove('active'));
  $('tab-' + name).classList.add('active');
  btn.classList.add('active');
  if (name === 'structure') loadStructure();
  if (name === 'architecture') loadCommunities();
  if (name === 'wiki') loadWikiList();
  if (name === 'status') loadStatus();
}

function switchProject(p) {
  currentProject = p;
  // Reload current tab
  const active = document.querySelector('nav button.active');
  if (active) active.click();
}

// ── Projects ─────────────────────────────────────────────────────────────────
async function loadProjects() {
  const data = await api('/projects');
  const projects = data.projects || [];
  const sel = $('project-select');
  sel.innerHTML = projects.map(p =>
    `<option value="${p.path}">${p.path.split('/').slice(-2).join('/')}</option>`
  ).join('');
  if (projects.length) { currentProject = projects[0].path; sel.value = currentProject; }

  const rows = projects.map(p => `<tr>
    <td style="max-width:320px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${p.path}">${p.path}</td>
    <td>${p.indexed_at ? '<span class="badge ok">indexed</span>' : '<span class="badge none">not indexed</span>'}</td>
    <td>${(p.file_count||0).toLocaleString()}</td>
    <td>${p.chunks != null ? p.chunks.toLocaleString() : '—'}</td>
    <td>${p.watching ? '<span class="badge ok">watching</span>' : '<span class="badge none">—</span>'}</td>
  </tr>`).join('');
  $('projects-table').innerHTML = `<table>
    <thead><tr><th>Path</th><th>Status</th><th>Files</th><th>Chunks</th><th>Watching</th></tr></thead>
    <tbody>${rows}</tbody></table>`;

  $('daemon-status').innerHTML = '<span class="ok">● connected</span>';
}

// ── Structure ────────────────────────────────────────────────────────────────
async function loadStructure() {
  if (!currentProject) return;
  $('structure-tree').textContent = 'Loading…';
  const data = await api('/overview?project=' + encodeURIComponent(currentProject));
  $('structure-tree').textContent = data.directory_tree || '';

  const langs = data.language_breakdown || [];
  const maxCount = langs[0]?.count || 1;
  $('lang-breakdown').innerHTML = langs.slice(0,20).map(l => `
    <div class="lang-bar">
      <span class="name">${l.extension}</span>
      <div class="bar"><div class="fill" style="width:${(l.count/maxCount*100).toFixed(1)}%"></div></div>
      <span class="count">${l.count.toLocaleString()}</span>
    </div>`).join('');

  const gs = data.graph_stats || {};
  const enriched = gs.enriched_communities || 0;
  const total = gs.total_communities || 0;
  $('graph-stats').innerHTML = [
    {val: data.file_count?.toLocaleString() || '—', lbl:'Files'},
    {val: gs.total_communities?.toLocaleString() || '—', lbl:'Communities'},
    {val: gs.enriched_communities?.toLocaleString() || '—', lbl:'Enriched'},
    {val: total ? (enriched/total*100).toFixed(0)+'%' : '—', lbl:'Enriched %'},
  ].map(s => `<div class="stat-box"><div class="val">${s.val}</div><div class="lbl">${s.lbl}</div></div>`).join('');
}

// ── Architecture ─────────────────────────────────────────────────────────────
async function loadCommunities() {
  if (!currentProject) return;
  $('communities-list').innerHTML = '<div class="loader">Loading…</div>';
  const data = await api('/communities?project=' + encodeURIComponent(currentProject) + '&top_k=50');
  const cs = data.communities || [];
  const enriched = cs.filter(c => c.title && c.title !== `Community ${c.id}`).length;
  $('enrichment-progress').innerHTML = `
    <div style="display:flex;justify-content:space-between;font-size:.75rem;color:#64748b">
      <span>Enriched ${enriched} of ${cs.length} top communities shown</span>
      <span>${data.total || cs.length} total</span>
    </div>
    <div class="progress-bar"><div class="progress-fill" style="width:${cs.length ? (enriched/cs.length*100).toFixed(0) : 0}%"></div></div>`;

  $('communities-list').innerHTML = cs.slice(0,30).map(c => `
    <div class="card" style="margin-bottom:8px;padding:12px">
      <div style="display:flex;justify-content:space-between;align-items:start">
        <strong style="color:#7c9fff;font-size:.85rem">${escHtml(c.title || 'Community ' + c.id)}</strong>
        <span style="font-size:.72rem;color:#64748b">${c.node_count} nodes</span>
      </div>
      <p style="font-size:.78rem;color:#94a3b8;margin-top:6px;line-height:1.5">${escHtml((c.summary||'').slice(0,300))}</p>
      ${c.key_entry_points?.length ? `<div style="margin-top:6px;font-size:.72rem;color:#64748b">Entry: ${c.key_entry_points.slice(0,3).map(e=>escHtml(typeof e==='string'?e.split('/').pop():'')).join(', ')}</div>` : ''}
    </div>`).join('');
}

// ── Graph ────────────────────────────────────────────────────────────────────
async function runGraph() {
  const sym = $('graph-symbol').value.trim();
  const rel = $('graph-relation').value;
  const to  = $('graph-to').value.trim();
  if (!sym || !currentProject) return;
  $('graph-result').textContent = 'Querying…';
  const url = `/api/graph?project=${encodeURIComponent(currentProject)}&symbol=${encodeURIComponent(sym)}&relation=${rel}${to ? '&to='+encodeURIComponent(to) : ''}`;
  const data = await api(url.slice(4));
  $('graph-result').textContent = JSON.stringify(data, null, 2);
}

async function exportGraph(fmt) {
  if (!currentProject) return;
  $('graph-result').textContent = `Exporting graph as ${fmt}… (may take 10–30s for large projects)`;
  const data = await api('/graph_export?project=' + encodeURIComponent(currentProject) + '&format=' + fmt + '&max_nodes=5000');
  if (data.error) { $('graph-result').textContent = 'Error: ' + data.error; return; }
  const content = fmt === 'graphml' ? data.graphml : JSON.stringify(data, null, 2);
  const blob = new Blob([content], {type: fmt === 'graphml' ? 'application/xml' : 'application/json'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `knowledge_graph.${fmt === 'graphml' ? 'graphml' : 'json'}`;
  a.click();
  $('graph-result').textContent = `Exported ${data.node_count || data.nodes?.length || '?'} nodes, ${data.edge_count || data.edges?.length || '?'} edges as ${fmt}.`;
}

// ── Wiki ─────────────────────────────────────────────────────────────────────
async function loadWikiList() {
  if (!currentProject) return;
  const data = await api('/wiki?project=' + encodeURIComponent(currentProject));
  const pages = data.pages || [];
  $('wiki-page-list').innerHTML = pages.length
    ? pages.map(p => `<li><a onclick="loadWikiPage('${escAttr(p)}')">${escHtml(p)}</a></li>`).join('')
    : '<li style="color:#64748b;font-size:.82rem">No wiki pages yet. Run build(action=&quot;wiki&quot;).</li>';
}

async function loadWikiPage(name) {
  const data = await api('/wiki/page?project=' + encodeURIComponent(currentProject) + '&name=' + encodeURIComponent(name));
  const md = data.content || '';
  // Very lightweight markdown rendering (no library needed for basic structure)
  $('wiki-content').innerHTML = simpleMarkdown(md);
}

async function runWikiSearch() {
  const q = $('wiki-search-q').value.trim();
  const scope = $('wiki-scope').value;
  if (!q || !currentProject) return;
  const data = await api('/ask?project=' + encodeURIComponent(currentProject) + '&q=' + encodeURIComponent(q) + '&scope=' + scope);
  const results = data.results || [];
  $('wiki-search-results').innerHTML = results.length
    ? results.map(r => `<div class="result-item">
        <div class="path">${escHtml(r.path?.split('/').slice(-2).join('/') || '')}<span class="score">${(r.score||0).toFixed(3)}</span></div>
        <pre>${escHtml((r.content||'').slice(0,300))}</pre>
      </div>`).join('')
    : '<div style="color:#64748b;font-size:.82rem;padding:10px">No results.</div>';
}

// ── Search ───────────────────────────────────────────────────────────────────
async function runSearch() {
  const q = $('search-q').value.trim();
  const scope = $('search-scope').value;
  if (!q) return;
  const data = await api('/search?project=' + encodeURIComponent(currentProject) + '&q=' + encodeURIComponent(q) + '&scope=' + scope);
  const results = data.results || [];
  $('search-results').innerHTML = results.length
    ? results.map(r => `<div class="result-item">
        <div class="path">${escHtml(r.path?.split('/').slice(-3).join('/') || '')}:${r.start_line||0}–${r.end_line||0}
          <span class="score">${(r.score||0).toFixed(3)}</span>
        </div>
        <pre>${escHtml((r.content||'').slice(0,400))}</pre>
      </div>`).join('')
    : '<div style="color:#64748b;font-size:.82rem;padding:10px">No results.</div>';
}

// ── Status ───────────────────────────────────────────────────────────────────
async function loadStatus() {
  const [health, metrics] = await Promise.all([
    fetch('/healthz').then(r=>r.json()),
    api('/metrics'),
  ]);
  const snap = health;
  $('daemon-metrics').innerHTML = [
    {val: snap.connected_clients ?? '—', lbl:'Clients'},
    {val: snap.active_watchers ?? '—', lbl:'Watchers'},
    {val: snap.uptime_s != null ? snap.uptime_s.toFixed(0)+'s' : '—', lbl:'Uptime'},
  ].map(s => `<div class="stat-box"><div class="val">${s.val}</div><div class="lbl">${s.lbl}</div></div>`).join('');
  $('search-metrics').textContent = JSON.stringify(metrics, null, 2);
}

// ── Helpers ──────────────────────────────────────────────────────────────────
function escHtml(s) { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
function escAttr(s) { return String(s).replace(/'/g,"\\'"); }

function simpleMarkdown(md) {
  return md
    .replace(/^### (.+)$/gm, '<h3>$1</h3>')
    .replace(/^## (.+)$/gm, '<h2>$1</h2>')
    .replace(/^# (.+)$/gm, '<h1>$1</h1>')
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/^```[\s\S]*?```$/gm, m => `<pre>${escHtml(m.slice(3,-3).replace(/^[a-z]+\\n/,''))}</pre>`)
    .replace(/^- (.+)$/gm, '<li>$1</li>')
    .replace(/(<li>.*<\/li>)/s, '<ul>$1</ul>')
    .replace(/\\n{2,}/g, '</p><p>')
    .replace(/^(?!<[hup])(.+)$/gm, '<p>$1</p>');
}

// ── Boot ──────────────────────────────────────────────────────────────────────
(async () => {
  try {
    await loadProjects();
  } catch(e) {
    $('daemon-status').innerHTML = '<span style="color:#f87171">● error: ' + escHtml(e.message) + '</span>';
    $('projects-table').innerHTML = '<div style="color:#f87171;padding:10px">Failed to connect to daemon: ' + escHtml(e.message) + '</div>';
  }
})();
</script>
</body>
</html>
"""

# ---------------------------------------------------------------------------
# API route handlers
# ---------------------------------------------------------------------------


def register_dashboard_routes(mcp: "FastMCP") -> None:
    """Attach all dashboard routes to the FastMCP instance."""

    @mcp.custom_route("/dashboard", methods=["GET"], include_in_schema=False)
    async def dashboard(_request: Request) -> HTMLResponse:
        return HTMLResponse(_DASHBOARD_HTML)

    @mcp.custom_route("/api/projects", methods=["GET"], include_in_schema=False)
    async def api_projects(_request: Request) -> JSONResponse:
        from opencode_search.handlers import handle_list_indexed_projects, handle_project_status
        data = await handle_list_indexed_projects()
        # Enrich with chunk counts
        projects = []
        for p in data.get("projects", []):
            try:
                status = await handle_project_status(path=p["path"])
                p["chunks"] = status.get("chunks")
                p["watching"] = status.get("watching", False)
            except Exception:
                pass
            projects.append(p)
        return JSONResponse({"projects": projects})

    @mcp.custom_route("/api/overview", methods=["GET"], include_in_schema=False)
    async def api_overview(request: Request) -> JSONResponse:
        from opencode_search.handlers import handle_project_structure
        project = request.query_params.get("project", "")
        if not project:
            return JSONResponse({"error": "project param required"}, status_code=400)
        result = await handle_project_structure(project_path=project, max_depth=4)
        return JSONResponse(result)

    @mcp.custom_route("/api/communities", methods=["GET"], include_in_schema=False)
    async def api_communities(request: Request) -> JSONResponse:
        from opencode_search.handlers import handle_get_communities
        project = request.query_params.get("project", "")
        top_k = int(request.query_params.get("top_k", "50"))
        if not project:
            return JSONResponse({"error": "project param required"}, status_code=400)
        result = await handle_get_communities(project_path=project, top_k=top_k)
        return JSONResponse(result)

    @mcp.custom_route("/api/wiki", methods=["GET"], include_in_schema=False)
    async def api_wiki_list(request: Request) -> JSONResponse:
        from opencode_search.config import get_project_wiki_dir
        project = request.query_params.get("project", "")
        if not project:
            return JSONResponse({"error": "project param required"}, status_code=400)
        wiki_dir = get_project_wiki_dir(project)
        pages = sorted(p.stem for p in wiki_dir.glob("*.md")) if wiki_dir.exists() else []
        return JSONResponse({"project": project, "pages": pages, "total": len(pages)})

    @mcp.custom_route("/api/wiki/page", methods=["GET"], include_in_schema=False)
    async def api_wiki_page(request: Request) -> JSONResponse:
        from opencode_search.config import get_project_wiki_dir
        project = request.query_params.get("project", "")
        name = request.query_params.get("name", "")
        if not project or not name:
            return JSONResponse({"error": "project and name params required"}, status_code=400)
        wiki_dir = get_project_wiki_dir(project)
        page_path = wiki_dir / f"{name}.md"
        if not page_path.exists():
            return JSONResponse({"error": f"Page not found: {name}"}, status_code=404)
        return JSONResponse({"name": name, "content": page_path.read_text(errors="replace")})

    @mcp.custom_route("/api/ask", methods=["GET"], include_in_schema=False)
    async def api_ask(request: Request) -> JSONResponse:
        from opencode_search.handlers import handle_global_search
        from opencode_search.handlers._wiki import handle_wiki_query
        project = request.query_params.get("project", "")
        q = request.query_params.get("q", "")
        scope = request.query_params.get("scope", "all")
        if not project or not q:
            return JSONResponse({"error": "project and q params required"}, status_code=400)
        if scope == "wiki":
            result = await handle_wiki_query(query=q, project_path=project, top_k=10)
        else:
            result = await handle_global_search(query=q, project_path=project, top_k=10)
        return JSONResponse(result)

    @mcp.custom_route("/api/search", methods=["GET"], include_in_schema=False)
    async def api_search(request: Request) -> JSONResponse:
        from opencode_search.handlers import handle_search_code
        project = request.query_params.get("project", "")
        q = request.query_params.get("q", "")
        scope = request.query_params.get("scope", "code")
        top_k = int(request.query_params.get("top_k", "10"))
        if not q:
            return JSONResponse({"error": "q param required"}, status_code=400)
        paths = [project] if project else None
        result = await handle_search_code(query=q, project_paths=paths, top_k=top_k)
        if scope == "docs" and "results" in result:
            doc_langs = {"wiki", "knowledge_base", "markdown", "rst", "text"}
            result["results"] = [
                r for r in result["results"]
                if r.get("language", "") in doc_langs or r.get("path", "").endswith((".md", ".rst", ".txt"))
            ]
        return JSONResponse(result)

    @mcp.custom_route("/api/graph", methods=["GET"], include_in_schema=False)
    async def api_graph(request: Request) -> JSONResponse:
        from opencode_search.handlers import (
            handle_get_symbol, handle_get_callers, handle_get_callees,
            handle_detect_impact, handle_trace_path,
        )
        project = request.query_params.get("project", "")
        symbol = request.query_params.get("symbol", "")
        relation = request.query_params.get("relation", "definition")
        to_sym = request.query_params.get("to", "")
        depth = int(request.query_params.get("depth", "5"))
        if not project or not symbol:
            return JSONResponse({"error": "project and symbol params required"}, status_code=400)
        if relation == "definition":
            result = await handle_get_symbol(name=symbol, project_path=project)
        elif relation == "callers":
            result = await handle_get_callers(symbol=symbol, project_path=project, depth=depth)
        elif relation == "callees":
            result = await handle_get_callees(symbol=symbol, project_path=project, depth=depth)
        elif relation == "impact":
            result = await handle_detect_impact(symbol=symbol, project_path=project)
        elif relation == "path" and to_sym:
            result = await handle_trace_path(from_symbol=symbol, to_symbol=to_sym, project_path=project)
        else:
            return JSONResponse({"error": "Invalid relation or missing to param"}, status_code=400)
        return JSONResponse(result)

    @mcp.custom_route("/api/federation", methods=["GET"], include_in_schema=False)
    async def api_federation(request: Request) -> JSONResponse:
        from opencode_search.handlers import handle_list_federation
        project = request.query_params.get("project", "")
        if not project:
            return JSONResponse({"error": "project param required"}, status_code=400)
        result = await handle_list_federation(project_path=project)
        return JSONResponse(result)

    @mcp.custom_route("/api/metrics", methods=["GET"], include_in_schema=False)
    async def api_metrics(_request: Request) -> JSONResponse:
        from opencode_search.metrics import get_metrics
        return JSONResponse(get_metrics())

    @mcp.custom_route("/api/graph_export", methods=["GET"], include_in_schema=False)
    async def api_graph_export(request: Request):
        from opencode_search.handlers import handle_graph_export
        from starlette.responses import Response
        project = request.query_params.get("project", "")
        fmt = request.query_params.get("format", "json")
        max_nodes = int(request.query_params.get("max_nodes", "5000"))
        if not project:
            return JSONResponse({"error": "project param required"}, status_code=400)
        result = await handle_graph_export(project_path=project, format=fmt, max_nodes=max_nodes)
        if fmt == "graphml" and "graphml" in result:
            return Response(
                content=result["graphml"],
                media_type="application/xml",
                headers={"Content-Disposition": "attachment; filename=knowledge_graph.graphml"},
            )
        return JSONResponse(result)
