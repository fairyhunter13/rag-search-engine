"""Self-contained dashboard HTML — imported by dashboard.py.

Datadog DRUIDS design: 5-view top navbar (Pulse / Chat / Graph / Wiki / Admin).
No sidebar. No accordion. Bento KPI grid + inline SVG sparklines.
"""
from __future__ import annotations

_DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>opencode-search</title>
<style>
/* ── Design tokens (Datadog DRUIDS) ──────────────────────────────────────── */
:root{
  --bg:#0f1117;--surface:#161b22;--surface-2:#1c2130;--surface-3:#222840;
  --border:rgba(255,255,255,.07);--border-2:rgba(255,255,255,.13);
  --text:#e4e8f7;--text-2:#8891b8;--text-3:#4e5880;
  --purple:#7b61ff;--cyan:#00d4ff;
  --green:#00c28e;--amber:#f5a623;--red:#ff4060;
  --green-dim:rgba(0,194,142,.18);--amber-dim:rgba(245,166,35,.18);--red-dim:rgba(255,64,96,.18);
  --nav-h:48px;--radius:6px;--trans:140ms ease;
}
/* ── Reset ──────────────────────────────────────────────────────────────── */
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%;font-family:'Inter','Segoe UI',system-ui,sans-serif;background:var(--bg);color:var(--text);-webkit-font-smoothing:antialiased}
button,select,input,textarea{font-family:inherit;outline:none}
a{color:inherit;text-decoration:none}
/* ── Top navbar ─────────────────────────────────────────────────────────── */
.topnav{
  position:fixed;top:0;left:0;right:0;height:var(--nav-h);z-index:100;
  display:flex;align-items:center;gap:0;padding:0 16px;
  background:rgba(15,17,23,.92);backdrop-filter:blur(12px);
  border-bottom:1px solid var(--border);
}
.brand{font-size:.85rem;font-weight:800;letter-spacing:-.015em;
  background:linear-gradient(90deg,var(--purple) 0%,var(--cyan) 100%);
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;
  flex-shrink:0;margin-right:20px}
.nav-views{display:flex;gap:2px}
.vbtn{background:none;border:none;color:var(--text-2);padding:6px 14px;font-size:.8rem;
  border-radius:var(--radius);cursor:pointer;transition:color var(--trans),background var(--trans)}
.vbtn:hover{color:var(--text);background:var(--surface-2)}
.vbtn.active{color:var(--text);background:var(--surface-2);font-weight:600}
.nav-right{display:flex;align-items:center;gap:10px;margin-left:auto}
#project-sel{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
  color:var(--text);padding:5px 10px;font-size:.78rem;cursor:pointer;max-width:220px;
  transition:border-color var(--trans)}
#project-sel:focus{border-color:var(--purple)}
.sdot{width:8px;height:8px;border-radius:50%;background:var(--text-3);flex-shrink:0;transition:background var(--trans),box-shadow var(--trans)}
.sdot.ok{background:var(--green);box-shadow:0 0 7px var(--green)}
.sdot.err{background:var(--red);box-shadow:0 0 7px var(--red)}
.sdot.warn{background:var(--amber);box-shadow:0 0 7px var(--amber)}
.iBtn{background:none;border:none;color:var(--text-3);cursor:pointer;font-size:.85rem;
  padding:5px 8px;border-radius:var(--radius);transition:color var(--trans),background var(--trans)}
.iBtn:hover{color:var(--text);background:var(--surface-2)}
.kbdHint{font-size:.68rem;color:var(--text-3);background:var(--surface-2);border:1px solid var(--border);
  border-radius:4px;padding:2px 6px}
/* ── Views container ────────────────────────────────────────────────────── */
.views{margin-top:var(--nav-h);height:calc(100vh - var(--nav-h));overflow:hidden;display:flex;flex-direction:column}
.view{display:none;flex:1;overflow:hidden}
.view.active{display:flex;flex-direction:column}
/* ── Pulse view ─────────────────────────────────────────────────────────── */
#view-pulse{overflow-y:auto;padding:20px 22px 32px;gap:18px}
/* bento grid: 6 tiles, 3 per row */
.bento{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;flex-shrink:0}
@media(max-width:860px){.bento{grid-template-columns:repeat(2,1fr)}}
@media(max-width:560px){.bento{grid-template-columns:1fr}}
/* tiles */
.tile{
  background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
  padding:16px 18px 10px;display:flex;flex-direction:column;gap:4px;
  transition:border-color var(--trans),box-shadow var(--trans);position:relative;overflow:hidden;
  min-height:110px;
}
.tile:hover{border-color:var(--border-2)}
.tile.ok{border-color:rgba(0,194,142,.3);box-shadow:0 0 0 0 var(--green),inset 0 0 40px rgba(0,194,142,.04)}
.tile.warn{border-color:rgba(245,166,35,.3);box-shadow:0 0 0 0 var(--amber),inset 0 0 40px rgba(245,166,35,.04)}
.tile.err{border-color:rgba(255,64,96,.3);box-shadow:0 0 0 0 var(--red),inset 0 0 40px rgba(255,64,96,.04)}
.tile-top{display:flex;justify-content:space-between;align-items:center}
.tile-lbl{font-size:.68rem;font-weight:600;color:var(--text-3);text-transform:uppercase;letter-spacing:.1em}
.tile-badge{font-size:.62rem;padding:2px 6px;border-radius:10px;font-weight:600}
.tile-badge.ok{background:var(--green-dim);color:var(--green)}
.tile-badge.warn{background:var(--amber-dim);color:var(--amber)}
.tile-badge.err{background:var(--red-dim);color:var(--red)}
.tile-num{font-size:2.4rem;font-weight:800;line-height:1.1;color:var(--text);letter-spacing:-.03em;margin-top:4px}
.tile-sub{font-size:.72rem;color:var(--text-2);margin-top:2px}
.tile-spark{margin-top:auto;padding-top:8px}
.tile-spark svg{width:100%;height:32px;overflow:visible}
/* bottom section */
.pulse-bottom{display:grid;grid-template-columns:1fr 1fr;gap:12px;flex-shrink:0}
@media(max-width:700px){.pulse-bottom{grid-template-columns:1fr}}
.panel{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:14px 16px;display:flex;flex-direction:column;gap:8px}
.panel-hdr{font-size:.68rem;font-weight:700;color:var(--text-3);text-transform:uppercase;letter-spacing:.1em;flex-shrink:0}
.act-item{font-size:.76rem;color:var(--text-2);padding:5px 0;border-bottom:1px solid var(--border);display:flex;gap:8px;align-items:baseline}
.act-item:last-child{border-bottom:none}
.act-time{color:var(--text-3);font-size:.68rem;flex-shrink:0}
.act-msg{flex:1}
.sq-btn{display:block;width:100%;text-align:left;background:none;border:1px solid var(--border);
  border-radius:var(--radius);color:var(--text-2);padding:7px 10px;font-size:.76rem;
  cursor:pointer;margin-bottom:5px;transition:all var(--trans)}
.sq-btn:hover{border-color:var(--purple);color:var(--text);background:rgba(123,97,255,.07)}
/* ── Chat view ──────────────────────────────────────────────────────────── */
#view-chat{flex-direction:column}
.chat-history{flex:1;overflow-y:auto;padding:20px 22px;display:flex;flex-direction:column;gap:14px;scrollbar-width:thin;scrollbar-color:var(--border) transparent}
.msg{max-width:760px;display:flex;flex-direction:column;gap:6px}
.msg.user{align-self:flex-end;align-items:flex-end}
.msg.ai{align-self:flex-start}
.msg-bubble{padding:10px 14px;border-radius:var(--radius);font-size:.83rem;line-height:1.55;word-break:break-word;overflow-x:auto}
.msg.user .msg-bubble{background:rgba(123,97,255,.18);border:1px solid rgba(123,97,255,.3);color:var(--text);white-space:pre-wrap}
.msg.ai .msg-bubble{background:var(--surface);border:1px solid var(--border);color:var(--text)}
.msg.ai.thinking .msg-bubble{color:var(--text-3);font-style:italic}
/* Markdown rendered content inside AI bubbles */
.msg.ai .msg-bubble h1,.msg.ai .msg-bubble h2,.msg.ai .msg-bubble h3{color:var(--text);font-weight:600;margin:.7em 0 .3em;line-height:1.3}
.msg.ai .msg-bubble h1{font-size:1rem}.msg.ai .msg-bubble h2{font-size:.95rem}.msg.ai .msg-bubble h3{font-size:.88rem}
.msg.ai .msg-bubble p{margin:.35em 0}
.msg.ai .msg-bubble ul,.msg.ai .msg-bubble ol{padding-left:1.3em;margin:.35em 0}
.msg.ai .msg-bubble li{margin:.15em 0}
.msg.ai .msg-bubble code{font-family:'JetBrains Mono','Fira Code',monospace;font-size:.8rem;background:var(--surface-3);border:1px solid var(--border);border-radius:3px;padding:0 4px}
.msg.ai .msg-bubble pre{background:var(--surface-2);border:1px solid var(--border);border-radius:var(--radius);padding:10px 12px;overflow-x:auto;margin:.4em 0}
.msg.ai .msg-bubble pre code{background:none;border:none;padding:0;font-size:.78rem;line-height:1.5}
.msg.ai .msg-bubble strong{color:var(--text);font-weight:600}
.msg.ai .msg-bubble em{color:var(--text-2)}
.msg.ai .msg-bubble blockquote{border-left:3px solid var(--purple);padding-left:10px;color:var(--text-2);margin:.4em 0}
.msg.ai .msg-bubble a{color:var(--cyan);text-decoration:underline}
.msg-meta{display:flex;gap:6px;align-items:center;flex-wrap:wrap}
.intent-tag{font-size:.62rem;padding:2px 7px;border-radius:10px;background:rgba(123,97,255,.15);color:var(--purple);font-weight:600}
.src-chip{font-size:.62rem;padding:2px 7px;border-radius:10px;background:var(--surface-2);color:var(--text-2);cursor:default;border:1px solid var(--border)}
.elapsed{font-size:.62rem;color:var(--text-3)}
.chat-bar{flex-shrink:0;padding:12px 22px 16px;background:rgba(15,17,23,.7);backdrop-filter:blur(8px);border-top:1px solid var(--border);display:flex;gap:10px;align-items:flex-end}
#chat-in{flex:1;background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
  color:var(--text);padding:10px 14px;font-size:.83rem;resize:none;max-height:160px;
  transition:border-color var(--trans),box-shadow var(--trans);overflow-y:auto;line-height:1.4}
#chat-in::placeholder{color:var(--text-3)}
#chat-in:focus{border-color:var(--purple);box-shadow:0 0 0 3px rgba(123,97,255,.15)}
.send-btn{background:var(--purple);border:none;color:#fff;width:38px;height:38px;border-radius:var(--radius);
  cursor:pointer;font-size:1rem;flex-shrink:0;transition:background var(--trans),transform var(--trans)}
.send-btn:hover{background:#6a50e0;transform:scale(1.05)}
.send-btn:disabled{background:var(--surface-2);color:var(--text-3);cursor:default;transform:none}
/* ── Admin view ─────────────────────────────────────────────────────────── */
#view-admin{overflow-y:auto;padding:20px 22px 32px;gap:16px}
.admin-grid{display:grid;grid-template-columns:1fr auto;gap:14px;align-items:start}
@media(max-width:700px){.admin-grid{grid-template-columns:1fr}}
.projects-table{width:100%;border-collapse:collapse;font-size:.78rem}
.projects-table th{text-align:left;padding:6px 10px;font-size:.65rem;color:var(--text-3);text-transform:uppercase;letter-spacing:.08em;border-bottom:1px solid var(--border);font-weight:600}
.projects-table td{padding:8px 10px;border-bottom:1px solid var(--border);color:var(--text-2);vertical-align:middle}
.projects-table tr:last-child td{border-bottom:none}
.projects-table tr.active-row td{color:var(--text);background:rgba(123,97,255,.05)}
.ops-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px;min-width:200px}
.op-btn{background:var(--surface-2);border:1px solid var(--border);border-radius:var(--radius);
  color:var(--text-2);padding:8px 12px;font-size:.77rem;cursor:pointer;
  transition:all var(--trans);text-align:center}
.op-btn:hover{border-color:var(--purple);color:var(--text);background:rgba(123,97,255,.1)}
.op-log{margin-top:10px;font-size:.73rem;color:var(--text-2);line-height:1.6;max-height:180px;overflow-y:auto;scrollbar-width:thin}
.op-log .ok{color:var(--green)}
.op-log .err{color:var(--red)}
/* ── Command palette ────────────────────────────────────────────────────── */
.cmd-overlay{position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:200;display:flex;align-items:flex-start;justify-content:center;padding-top:100px}
.cmd-overlay.hidden{display:none}
.cmd-card{background:var(--surface);border:1px solid var(--border-2);border-radius:var(--radius);
  width:100%;max-width:520px;box-shadow:0 20px 60px rgba(0,0,0,.7);overflow:hidden}
#cmd-input{width:100%;background:none;border:none;border-bottom:1px solid var(--border);
  color:var(--text);padding:14px 18px;font-size:.9rem}
#cmd-results{list-style:none;max-height:320px;overflow-y:auto}
#cmd-results li{padding:10px 18px;font-size:.8rem;color:var(--text-2);cursor:pointer;display:flex;gap:10px;align-items:center}
#cmd-results li:hover,#cmd-results li.hi{background:var(--surface-2);color:var(--text)}
#cmd-results li .cr-cat{font-size:.63rem;color:var(--text-3);margin-left:auto}
/* ── Toast ──────────────────────────────────────────────────────────────── */
#toast{position:fixed;bottom:24px;right:24px;z-index:300;pointer-events:none;display:flex;flex-direction:column;gap:6px}
.toast{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
  padding:10px 16px;font-size:.78rem;box-shadow:0 4px 20px rgba(0,0,0,.4);
  animation:slideIn .2s ease;max-width:320px}
.toast.ok{border-color:rgba(0,194,142,.4);color:var(--green)}
.toast.err{border-color:rgba(255,64,96,.4);color:var(--red)}
.toast.info{color:var(--text)}
@keyframes slideIn{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:translateY(0)}}
/* ── Graph view ─────────────────────────────────────────────────────────── */
#view-graph{flex-direction:column;overflow:hidden}
.graph-toolbar{flex-shrink:0;display:flex;align-items:center;gap:8px;padding:10px 16px;border-bottom:1px solid var(--border);background:var(--surface);flex-wrap:wrap}
#graph-search{background:var(--surface-2);border:1px solid var(--border);border-radius:var(--radius);color:var(--text);padding:6px 10px;font-size:.78rem;width:180px;transition:border-color var(--trans)}
#graph-search:focus{border-color:var(--purple)}
#graph-layout-sel,#graph-filter-sel{background:var(--surface-2);border:1px solid var(--border);border-radius:var(--radius);color:var(--text);padding:5px 8px;font-size:.75rem;cursor:pointer}
#graph-canvas{flex:1;background:var(--surface);position:relative;min-height:300px}
#graph-canvas canvas{width:100% !important;height:100% !important}
.graph-empty{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;color:var(--text-3);font-size:.83rem;pointer-events:none}
#graph-detail{flex-shrink:0;padding:12px 16px;border-top:1px solid var(--border);background:var(--surface);min-height:64px;max-height:180px;overflow-y:auto;font-size:.78rem;color:var(--text-2)}
.gd-name{font-weight:700;color:var(--text);margin-bottom:4px}
.gd-meta{color:var(--text-3);font-size:.7rem;margin-bottom:6px}
.gd-neighbours{display:flex;flex-wrap:wrap;gap:4px}
.gd-nb{background:var(--surface-2);border:1px solid var(--border);border-radius:10px;padding:2px 8px;font-size:.68rem;cursor:pointer;transition:border-color var(--trans)}
.gd-nb:hover{border-color:var(--purple);color:var(--text)}
/* ── Wiki view ──────────────────────────────────────────────────────────── */
#view-wiki{flex-direction:row;overflow:hidden}
.wiki-sidebar{width:220px;flex-shrink:0;display:flex;flex-direction:column;border-right:1px solid var(--border);background:var(--surface);overflow:hidden}
#wiki-search{background:var(--surface-2);border:none;border-bottom:1px solid var(--border);color:var(--text);padding:10px 14px;font-size:.78rem;width:100%;transition:background var(--trans)}
#wiki-search:focus{background:var(--surface-3);outline:none}
#wiki-pages{flex:1;overflow-y:auto;padding:6px 0;scrollbar-width:thin}
.wiki-page-link{display:block;width:100%;background:none;border:none;text-align:left;padding:7px 14px;font-size:.77rem;color:var(--text-2);cursor:pointer;transition:background var(--trans),color var(--trans)}
.wiki-page-link:hover,.wiki-page-link.active{background:rgba(123,97,255,.1);color:var(--text)}
.wiki-content-pane{flex:1;display:flex;flex-direction:column;overflow:hidden}
#wiki-content{flex:1;overflow-y:auto;padding:20px 24px;font-size:.83rem;line-height:1.65;color:var(--text);scrollbar-width:thin}
#wiki-content h1,#wiki-content h2,#wiki-content h3{color:var(--text);font-weight:600;margin:.8em 0 .4em;line-height:1.3}
#wiki-content h1{font-size:1.2rem}#wiki-content h2{font-size:1rem}#wiki-content h3{font-size:.9rem}
#wiki-content p{margin:.4em 0}
#wiki-content ul,#wiki-content ol{padding-left:1.4em;margin:.4em 0}
#wiki-content li{margin:.15em 0}
#wiki-content code{font-family:'JetBrains Mono','Fira Code',monospace;font-size:.8rem;background:var(--surface-3);border:1px solid var(--border);border-radius:3px;padding:0 4px}
#wiki-content pre{background:var(--surface-2);border:1px solid var(--border);border-radius:var(--radius);padding:10px 12px;overflow-x:auto;margin:.5em 0}
#wiki-content pre code{background:none;border:none;padding:0;font-size:.78rem}
#wiki-content blockquote{border-left:3px solid var(--purple);padding-left:10px;color:var(--text-2);margin:.5em 0}
#wiki-content a{color:var(--cyan);text-decoration:underline}
#wiki-content strong{color:var(--text);font-weight:600}
#wiki-content table{border-collapse:collapse;width:100%;margin:.5em 0;font-size:.8rem}
#wiki-content th,#wiki-content td{border:1px solid var(--border);padding:6px 10px;text-align:left}
#wiki-content th{background:var(--surface-2);color:var(--text-3);font-size:.7rem;text-transform:uppercase;letter-spacing:.05em}
.wiki-empty{display:flex;align-items:center;justify-content:center;flex:1;color:var(--text-3);font-size:.83rem}
.wiki-lint-panel{flex-shrink:0;border-top:1px solid var(--border);background:var(--surface);padding:8px 16px}
.wiki-lint-hdr{display:flex;align-items:center;gap:6px;cursor:pointer;font-size:.72rem;color:var(--text-3);padding:2px 0}
.wiki-lint-hdr:hover{color:var(--text)}
#wiki-lint-count{font-weight:700;color:var(--amber)}
.wiki-lint-items{font-size:.72rem;color:var(--text-2);max-height:100px;overflow-y:auto;display:none;margin-top:6px}
.wiki-lint-items.open{display:block}
.wiki-lint-item{padding:2px 0;border-bottom:1px solid var(--border)}
.wiki-lint-item:last-child{border-bottom:none}
/* ── mdSafe table (chat bubbles) */
.msg.ai .msg-bubble table{border-collapse:collapse;width:100%;margin:.4em 0;font-size:.78rem}
.msg.ai .msg-bubble th,.msg.ai .msg-bubble td{border:1px solid var(--border);padding:5px 8px;text-align:left}
.msg.ai .msg-bubble th{background:var(--surface-2);color:var(--text-3);font-size:.7rem;text-transform:uppercase}
/* ── Admin SSE chips + auto-pipeline ────────────────────────────────────── */
#admin-job-chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px;min-height:24px}
.admin-chip{display:inline-flex;align-items:center;gap:5px;padding:3px 8px 3px 10px;border-radius:12px;font-size:.7rem;font-weight:600;border:1px solid var(--border);background:var(--surface-2);color:var(--text-2);transition:all var(--trans)}
.admin-chip.queued{border-color:var(--border);color:var(--text-2)}
.admin-chip.running{border-color:rgba(245,166,35,.5);background:var(--amber-dim);color:var(--amber)}
.admin-chip.done{border-color:rgba(0,194,142,.4);background:var(--green-dim);color:var(--green)}
.admin-chip.error{border-color:rgba(255,64,96,.4);background:var(--red-dim);color:var(--red)}
.chip-dismiss{background:none;border:none;color:inherit;opacity:.6;cursor:pointer;padding:0;font-size:.75rem;line-height:1;margin-left:2px}
.chip-dismiss:hover{opacity:1}
#admin-autopipeline-log{margin-top:10px;font-size:.72rem;color:var(--text-2);max-height:120px;overflow-y:auto;scrollbar-width:thin}
.ap-entry{display:flex;gap:8px;padding:3px 0;border-bottom:1px solid var(--border)}
.ap-entry:last-child{border-bottom:none}
.ap-ts{color:var(--text-3);flex-shrink:0}
.ap-msg{flex:1;word-break:break-word}
/* ── Scrollbar ──────────────────────────────────────────────────────────── */
::-webkit-scrollbar{width:4px;height:4px}::-webkit-scrollbar-track{background:transparent}::-webkit-scrollbar-thumb{background:var(--border-2);border-radius:2px}
</style>
<script src="/static/sigma-graph.min.js" defer></script>
</head>
<body>

<!-- Top navbar -->
<nav class="topnav">
  <span class="brand">⬡ opencode-search</span>
  <div class="nav-views">
    <button class="vbtn active" id="vbtn-pulse" onclick="switchView('pulse')">Pulse</button>
    <button class="vbtn" id="vbtn-chat" onclick="switchView('chat')">Chat</button>
    <button class="vbtn" id="vbtn-admin" onclick="switchView('admin')">Admin</button>
    <button class="vbtn" id="vbtn-graph" onclick="switchView('graph')">Graph</button>
    <button class="vbtn" id="vbtn-wiki" onclick="switchView('wiki')">Wiki</button>
  </div>
  <div class="nav-right">
    <span class="sdot" id="daemon-dot" title="Daemon status"></span>
    <select id="project-sel" onchange="switchProject(this.value)" title="Active project"></select>
    <button class="iBtn" id="theme-btn" onclick="toggleTheme()" title="Toggle theme">☀</button>
    <button class="iBtn" onclick="showCmdPalette()" title="Command palette (Ctrl+K)"><span class="kbdHint">⌘K</span></button>
  </div>
</nav>

<!-- Views container -->
<div class="views">

  <!-- ── Pulse ─────────────────────────────────────────────────────────── -->
  <div id="view-pulse" class="view active">
    <!-- KPI bento grid -->
    <div class="bento" id="bento-grid">
      <div class="tile" id="tile-files">
        <div class="tile-top">
          <span class="tile-lbl">Files Indexed</span>
          <span class="tile-badge" id="tb-files"></span>
        </div>
        <div class="tile-num" id="kpi-files">—</div>
        <div class="tile-sub" id="ks-files">loading…</div>
        <div class="tile-spark"><svg id="sp-files"></svg></div>
      </div>
      <div class="tile" id="tile-communities">
        <div class="tile-top">
          <span class="tile-lbl">Communities</span>
          <span class="tile-badge" id="tb-communities"></span>
        </div>
        <div class="tile-num" id="kpi-communities">—</div>
        <div class="tile-sub" id="ks-communities">loading…</div>
        <div class="tile-spark"><svg id="sp-communities"></svg></div>
      </div>
      <div class="tile" id="tile-enrichment">
        <div class="tile-top">
          <span class="tile-lbl">KB Enrichment</span>
          <span class="tile-badge" id="tb-enrichment"></span>
        </div>
        <div class="tile-num" id="kpi-enrichment">—</div>
        <div class="tile-sub" id="ks-enrichment">loading…</div>
        <div class="tile-spark"><svg id="sp-enrichment"></svg></div>
      </div>
      <div class="tile" id="tile-wiki">
        <div class="tile-top">
          <span class="tile-lbl">Wiki Pages</span>
          <span class="tile-badge" id="tb-wiki"></span>
        </div>
        <div class="tile-num" id="kpi-wiki">—</div>
        <div class="tile-sub" id="ks-wiki">knowledge base</div>
        <div class="tile-spark"><svg id="sp-wiki"></svg></div>
      </div>
      <div class="tile" id="tile-requests">
        <div class="tile-top">
          <span class="tile-lbl">Requests Served</span>
          <span class="tile-badge" id="tb-requests"></span>
        </div>
        <div class="tile-num" id="kpi-requests">—</div>
        <div class="tile-sub" id="ks-requests">loading…</div>
        <div class="tile-spark"><svg id="sp-requests"></svg></div>
      </div>
      <div class="tile" id="tile-uptime">
        <div class="tile-top">
          <span class="tile-lbl">Daemon Uptime</span>
          <span class="tile-badge ok" id="tb-uptime">live</span>
        </div>
        <div class="tile-num" id="kpi-uptime">—</div>
        <div class="tile-sub" id="ks-uptime">active watchers: —</div>
        <div class="tile-spark"><svg id="sp-uptime"></svg></div>
      </div>
      <div class="tile" id="tile-stream">
        <div class="tile-top">
          <span class="tile-lbl">Stream Health</span>
          <span class="tile-badge" id="tb-stream"></span>
        </div>
        <div class="tile-num" id="kpi-stream">—</div>
        <div class="tile-sub" id="ks-stream">LLM stream calls</div>
        <div class="tile-spark"><svg id="sp-stream"></svg></div>
      </div>
    </div>

    <!-- Activity feed + suggested questions -->
    <div class="pulse-bottom">
      <div class="panel">
        <div class="panel-hdr">Live Activity</div>
        <div id="activity-list"></div>
      </div>
      <div class="panel">
        <div class="panel-hdr">Ask the Codebase</div>
        <div id="suggested-list"></div>
      </div>
    </div>
  </div>

  <!-- ── Chat ──────────────────────────────────────────────────────────── -->
  <div id="view-chat" class="view">
    <div class="chat-history" id="chat-history"></div>
    <div class="chat-bar">
      <textarea id="chat-in" rows="1"
        placeholder="Ask anything — how does X work? what calls Y? find the Z handler…"></textarea>
      <button class="send-btn" id="send-btn" onclick="sendChat()">↑</button>
    </div>
  </div>

  <!-- ── Admin ─────────────────────────────────────────────────────────── -->
  <div id="view-admin" class="view">
    <div class="admin-grid">
      <div class="panel">
        <div class="panel-hdr">Indexed Projects</div>
        <div id="projects-wrap">
          <table class="projects-table" id="projects-table">
            <thead><tr>
              <th>Path</th><th>Files</th><th>Status</th><th>Watching</th>
            </tr></thead>
            <tbody id="projects-body"></tbody>
          </table>
        </div>
      </div>
      <div>
        <div class="panel">
          <div class="panel-hdr">Operations</div>
          <div class="ops-grid">
            <button class="op-btn" onclick="runVacuum()">🧹 Vacuum</button>
            <button class="op-btn" onclick="runDedup()">🔗 Dedup</button>
            <button class="op-btn" onclick="runReindex()">⚡ Re-index</button>
            <button class="op-btn" onclick="runEnrich()">✨ Enrich</button>
            <button class="op-btn" onclick="runWiki()">📚 Wiki</button>
            <button class="op-btn" onclick="loadAdmin()">🔄 Refresh</button>
          </div>
          <div class="op-log" id="op-log"></div>
          <div id="admin-job-chips"></div>
          <div class="panel-hdr" style="margin-top:10px">Auto-Pipeline Events</div>
          <div id="admin-autopipeline-log"></div>
        </div>
      </div>
    </div>
  </div>

  <!-- ── Graph ─────────────────────────────────────────────────────────── -->
  <div id="view-graph" class="view">
    <div class="graph-toolbar">
      <input id="graph-search" placeholder="Search node…" oninput="searchGraphNode(this.value)" autocomplete="off"/>
      <select id="graph-layout-sel" onchange="applyGraphLayout(this.value)" title="Layout">
        <option value="fa2">Force-Directed</option>
        <option value="circular">Circular</option>
      </select>
      <select id="graph-filter-sel" onchange="applyGraphFilter(this.value)" title="Filter">
        <option value="all">All types</option>
        <option value="file">Files only</option>
        <option value="symbol">Symbols only</option>
      </select>
      <button class="iBtn" onclick="loadGraph()" title="Reload graph">⟳ Reload</button>
      <span id="graph-node-count" style="font-size:.72rem;color:var(--text-3)"></span>
    </div>
    <div id="graph-canvas"><div class="graph-empty" id="graph-empty">Select a project and click ⟳ Reload</div></div>
    <div id="graph-detail" style="color:var(--text-3);font-size:.78rem">Click a node to see details</div>
  </div>

  <!-- ── Wiki ──────────────────────────────────────────────────────────── -->
  <div id="view-wiki" class="view">
    <div class="wiki-sidebar">
      <input id="wiki-search" placeholder="Search pages…" oninput="searchWiki(this.value)" autocomplete="off"/>
      <div id="wiki-pages"><div style="padding:12px 14px;color:var(--text-3);font-size:.76rem">Loading…</div></div>
    </div>
    <div class="wiki-content-pane">
      <div id="wiki-content"><div class="wiki-empty">← Pick a page to read</div></div>
      <div class="wiki-lint-panel" id="wiki-lint-panel" style="display:none">
        <div class="wiki-lint-hdr" onclick="toggleWikiLint()">
          ⚠ Lint warnings: <span id="wiki-lint-count">0</span>
          <span id="wiki-lint-chevron" style="margin-left:auto">▾</span>
        </div>
        <div class="wiki-lint-items" id="wiki-lint-items"></div>
      </div>
    </div>
  </div>

</div><!-- /views -->

<!-- Command palette -->
<div class="cmd-overlay hidden" id="cmd-overlay" onclick="hideCmdPalette(event)">
  <div class="cmd-card" onclick="event.stopPropagation()">
    <input id="cmd-input" placeholder="Jump to view, ask a question, run an op…"
      oninput="filterCmd(this.value)" onkeydown="cmdKey(event)" autocomplete="off"/>
    <ul id="cmd-results"></ul>
  </div>
</div>

<!-- Toast container -->
<div id="toast"></div>

<script>
'use strict';
/* ── State ───────────────────────────────────────────────────────────────── */
let _proj='';
let _chatHistory=[];
let _chatInFlight=false;
let _cmdIdx=0;
let _sparkHistory={};
let _msgSeq=0;

/* ── Helpers ─────────────────────────────────────────────────────────────── */
const $=id=>document.getElementById(id);
const esc=s=>String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
// mdSafe: render markdown to safe HTML for AI messages (no external deps)
function mdSafe(s){
  if(!s)return '';
  // 1. Escape HTML first (XSS safety)
  let h=esc(String(s));
  // 2. Fenced code blocks  ```lang\ncode\n```
  h=h.replace(/```(?:[^\n]*)?\n([\s\S]*?)```/g,(_,c)=>'<pre><code>'+c+'</code></pre>');
  // 3. Inline code
  h=h.replace(/`([^`\n]+)`/g,(_,c)=>'<code>'+c+'</code>');
  // 4. Headings (process before bold/italic)
  h=h.replace(/^### (.+)$/gm,'<h3>$1</h3>');
  h=h.replace(/^## (.+)$/gm,'<h2>$1</h2>');
  h=h.replace(/^# (.+)$/gm,'<h1>$1</h1>');
  // 5. Bold + italic combinations
  h=h.replace(/\*\*\*(.+?)\*\*\*/g,'<strong><em>$1</em></strong>');
  h=h.replace(/\*\*(.+?)\*\*/g,'<strong>$1</strong>');
  h=h.replace(/__(.+?)__/g,'<strong>$1</strong>');
  h=h.replace(/\*([^*\n]+)\*/g,'<em>$1</em>');
  // 6. Blockquote (already escaped, so &gt;)
  h=h.replace(/^&gt; (.+)$/gm,'<blockquote>$1</blockquote>');
  // 6b. Links: [text](url) — only http/https/relative allowed (XSS guard via URL prefix check)
  h=h.replace(/\[([^\]]+)\]\(((?:https?:\/\/|\/)[^\s)]*)\)/g,'<a href="$2" target="_blank" rel="noopener">$1</a>');
  // 7a. Tables: | header | \n | --- | \n | body |
  h=h.replace(/((?:^\|.+\|[ \t]*$\n?)+)/gm,m=>{
    const rows=m.trim().split('\n');
    if(rows.length<2)return m;
    const isSep=r=>/^\|[\s\-\|:]+\|[ \t]*$/.test(r);
    if(!isSep(rows[1]))return m;
    const cells=r=>r.replace(/^[ \t]*\||\|[ \t]*$/g,'').split('|').map(c=>c.trim());
    const thead='<thead><tr>'+cells(rows[0]).map(c=>'<th>'+c+'</th>').join('')+'</tr></thead>';
    const tbody='<tbody>'+rows.slice(2).filter(r=>r.trim()&&!isSep(r)).map(r=>'<tr>'+cells(r).map(c=>'<td>'+c+'</td>').join('')+'</tr>').join('')+'</tbody>';
    return '<table>'+thead+tbody+'</table>';
  });
  // 7b. Unordered lists
  h=h.replace(/((?:^[*\-] .+$\n?)+)/gm,m=>'<ul>'+m.replace(/^[*\-] (.+)$/gm,'<li>$1</li>')+'</ul>');
  // 7c. Ordered lists
  h=h.replace(/((?:^\d+\. .+$\n?)+)/gm,m=>'<ol>'+m.replace(/^\d+\. (.+)$/gm,'<li>$1</li>')+'</ol>');
  // 8. Horizontal rule
  h=h.replace(/^---+$/gm,'<hr>');
  // 9. Paragraphs: split on blank lines
  const parts=h.split(/\n{2,}/);
  h=parts.map(p=>{
    p=p.trim();
    if(!p)return '';
    if(/^<(h[1-3]|pre|ul|ol|table|blockquote|hr)/.test(p))return p;
    return '<p>'+p.replace(/\n/g,'<br>')+'</p>';
  }).join('\n');
  return h;
}

function toast(msg,type='info'){
  const t=document.createElement('div');
  t.className=`toast ${type}`;t.textContent=msg;
  $('toast').appendChild(t);
  setTimeout(()=>t.remove(),4000);
}

function setDot(state){
  const d=$('daemon-dot');
  d.className='sdot '+(state==='ok'?'ok':state==='warn'?'warn':'err');
}

/* ── fetch with AbortController timeout ─────────────────────────────────── */
async function fetchWithTimeout(url,opts={},ms=30000){
  const ac=new AbortController();
  const tid=setTimeout(()=>ac.abort(),ms);
  try{
    const r=await fetch(url,{...opts,signal:ac.signal});
    clearTimeout(tid);
    return r;
  }catch(e){
    clearTimeout(tid);
    throw e;
  }
}

/* ── View switching ──────────────────────────────────────────────────────── */
function switchView(name){
  ['pulse','chat','admin','graph','wiki'].forEach(v=>{
    $('view-'+v).classList.toggle('active',v===name);
    $('vbtn-'+v).classList.toggle('active',v===name);
  });
  if(name==='pulse')loadPulse();
  else if(name==='admin')loadAdmin();
  else if(name==='chat'&&$('chat-in'))$('chat-in').focus();
  else if(name==='graph'&&!window.__graph)loadGraph();
  else if(name==='wiki')loadWiki();
}

/* ── Project selector ────────────────────────────────────────────────────── */
async function loadProjects(){
  try{
    const r=await fetchWithTimeout('/api/projects');
    if(!r.ok) throw new Error(`HTTP ${r.status}`);
    const d=await r.json();
    const sel=$('project-sel');
    const projs=d.projects||[];
    sel.innerHTML=projs.length
      ?projs.map(p=>`<option value="${esc(p.path)}">${esc(p.path.split('/').slice(-2).join('/'))}</option>`).join('')
      :'<option value="">No projects indexed</option>';
    if(!_proj&&projs.length)_proj=projs[0].path;
    if(_proj)sel.value=_proj;
    return projs;
  }catch(e){
    toast('Failed to load projects: '+e.message,'err');
    return [];
  }
}

function switchProject(path){
  _proj=path;
  _sparkHistory={};
  _chatHistory=[];
  _chatInFlight=false;
  _wikiPages=[];
  if(window.__graph){try{window.__graph.sigma.kill();}catch(_){}window.__graph=null;}
  const active=document.querySelector('.view.active');
  if(active&&active.id==='view-pulse')loadPulse();
  else if(active&&active.id==='view-admin')loadAdmin();
  else if(active&&active.id==='view-graph')loadGraph();
  else if(active&&active.id==='view-wiki')loadWiki();
}

/* ── Sparkline ───────────────────────────────────────────────────────────── */
function drawSparkline(svgEl,values,color='#7b61ff'){
  if(!values||values.length<2){svgEl.innerHTML='';return;}
  const W=svgEl.parentElement.offsetWidth||200,H=32;
  svgEl.setAttribute('viewBox',`0 0 ${W} ${H}`);
  const mn=Math.min(...values),mx=Math.max(...values);
  const range=mx-mn||1;
  const pts=values.map((v,i)=>{
    const x=i/(values.length-1)*(W-4)+2;
    const y=H-2-(v-mn)/range*(H-4);
    return `${x},${y}`;
  }).join(' ');
  const grad=`sp-grad-${svgEl.id}`;
  svgEl.innerHTML=`
    <defs>
      <linearGradient id="${grad}" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%" stop-color="${color}" stop-opacity="0.3"/>
        <stop offset="100%" stop-color="${color}" stop-opacity="0"/>
      </linearGradient>
    </defs>
    <polygon points="${pts} ${W-2},${H} 2,${H}" fill="url(#${grad})"/>
    <polyline points="${pts}" fill="none" stroke="${color}" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/>`;
}

/* ── Pulse data ──────────────────────────────────────────────────────────── */
async function loadPulse(){
  if(!_proj)return;
  try{
    const [ovr,kb,met,sug]=await Promise.allSettled([
      fetchWithTimeout(`/api/overview?project=${encodeURIComponent(_proj)}`).then(r=>r.json()),
      fetchWithTimeout(`/api/kb_health?project=${encodeURIComponent(_proj)}`).then(r=>r.json()),
      fetchWithTimeout('/api/metrics').then(r=>r.json()),
      fetchWithTimeout(`/api/suggested_questions?project=${encodeURIComponent(_proj)}`).then(r=>r.json()),
    ]);

    // Files tile
    const struct=ovr.status==='fulfilled'?ovr.value:{};
    const fileCount=struct.total_files||struct.file_count||null;
    setTile('files',fileCount,
      fileCount!=null?'indexed':'—',
      fileCount!=null?'ok':'warn',
      `${struct.language_breakdown?(Object.keys(struct.language_breakdown).slice(0,3).join(', '))||'—':'—'}`
    );

    // Communities + Enrichment tiles
    const kbD=kb.status==='fulfilled'?kb.value:{};
    const comms=kbD.total_communities;
    const enrichPct=kbD.enrichment_pct;
    setTile('communities',comms,comms!=null?'graph':'—',comms>0?'ok':'warn',
      enrichPct!=null?`${enrichPct}% enriched`:'enrichment unknown');
    const ep=enrichPct!=null?enrichPct:0;
    setTile('enrichment',enrichPct!=null?enrichPct+'%':null,
      'enriched',
      ep>=80?'ok':ep>=40?'warn':'err',
      `${kbD.enriched_communities||0} / ${kbD.total_communities||0} communities`
    );

    // Wiki tile
    const wikiCt=kbD.wiki_page_count;
    setTile('wiki',wikiCt,'pages',wikiCt>0?'ok':'warn',
      'knowledge base pages');

    // Requests + uptime tiles
    const metD=met.status==='fulfilled'?met.value:{};
    const reqs=metD.total_requests||metD.requests||null;
    const errors=metD.errors||0;
    const errRate=reqs?Math.round(errors/reqs*100):0;
    setTile('requests',reqs,'served',
      errRate<5?'ok':errRate<20?'warn':'err',
      `${errors} errors · ${metD.connected_clients||0} clients`
    );
    const uptS=metD.uptime_s;
    const uptStr=uptS!=null?fmtUptime(uptS):'—';
    const watchers=metD.active_watchers||metD.watchers||0;
    $('kpi-uptime').textContent=uptStr;
    $('ks-uptime').textContent=`active watchers: ${watchers}`;
    $('tile-uptime').className='tile '+(uptS!=null&&uptS>=60?'ok':'warn');

    const cs=metD.chat_stream||{};
    const csErr=cs.stream_error_count||0;
    const csSucc=cs.stream_success_count||0;
    const csTotal=csErr+csSucc;
    const csRate=csTotal>0?(csErr/csTotal):0;
    const csBadge=csErr===0?'ok':(csRate<0.05?'warn':'err');
    setTile('stream',csTotal,
      csErr===0?'✓':`${csErr} err`,
      csBadge,
      csTotal>0?`${csSucc} ok · ${csErr} err`:'no calls yet'
    );

    const _metOk=met.status==='fulfilled';
    const _secondaryOk=[ovr,kb,sug].every(s=>s.status==='fulfilled');
    setDot(!_metOk?'err':_secondaryOk?'ok':'warn');

    // Sparklines (push current value into rolling history)
    pushSpark('files',fileCount);
    pushSpark('communities',comms);
    pushSpark('enrichment',ep);
    pushSpark('wiki',wikiCt);
    pushSpark('requests',reqs);
    pushSpark('stream',csTotal);
    pushSpark('uptime',uptS);
    renderSparks();

    // Activity feed from pipeline events
    const pipeEvt=kbD.last_pipeline_event;
    const actList=$('activity-list');
    if(pipeEvt){
      const msg=pipeEvt.action||pipeEvt.event||JSON.stringify(pipeEvt);
      const ts=pipeEvt.ts||pipeEvt.timestamp||'';
      actList.innerHTML=`<div class="act-item"><span class="act-time">${esc(ts.slice(0,16))}</span><span class="act-msg">${esc(msg)}</span></div>`+actList.innerHTML;
    }else if(!actList.children.length){
      actList.innerHTML='<div class="act-item"><span class="act-msg" style="color:var(--text-3)">No recent pipeline events</span></div>';
    }

    // Suggested questions
    const sugD=sug.status==='fulfilled'?sug.value:{};
    const qs=(sugD.questions||[]).slice(0,6);
    $('suggested-list').innerHTML=qs.length
      ?qs.map(q=>`<button class="sq-btn" data-q="${esc(q.question||q)}" onclick="askQuestion(this.dataset.q)">${esc(q.question||q)}</button>`).join('')
      :'<div style="color:var(--text-3);font-size:.75rem">Run the full pipeline to generate questions</div>';

  }catch(e){
    setDot('err');
    toast('Pulse load error: '+e.message,'err');
  }
}

function setTile(id,value,badge,status,sub){
  const tile=$('tile-'+id);
  const kpi=$('kpi-'+id);
  const ks=$('ks-'+id);
  const tb=$('tb-'+id);
  tile.className='tile '+(status||'');
  kpi.textContent=value!=null?fmtNum(value):'—';
  if(ks)ks.textContent=sub||'';
  if(tb){
    tb.textContent=badge||'';
    tb.className='tile-badge '+(status||'');
  }
}

function fmtNum(n){
  if(n==null)return '—';
  const num=parseFloat(String(n).replace('%',''));
  if(String(n).includes('%'))return n;
  if(num>=1000000)return (num/1000000).toFixed(1)+'M';
  if(num>=1000)return (num/1000).toFixed(1)+'K';
  return String(n);
}

function fmtUptime(s){
  if(s<60)return s+'s';
  if(s<3600)return Math.floor(s/60)+'m';
  if(s<86400)return Math.floor(s/3600)+'h '+Math.floor((s%3600)/60)+'m';
  return Math.floor(s/86400)+'d '+Math.floor((s%86400)/3600)+'h';
}

function pushSpark(key,val){
  if(val==null)return;
  const num=parseFloat(String(val).replace('%',''));
  if(isNaN(num))return;
  if(!_sparkHistory[key])_sparkHistory[key]=[];
  _sparkHistory[key].push(num);
  if(_sparkHistory[key].length>20)_sparkHistory[key].shift();
}

function renderSparks(){
  const colorMap={
    files:getComputedStyle(document.documentElement).getPropertyValue('--purple').trim()||'#7b61ff',
    communities:'#00d4ff',enrichment:'#00c28e',wiki:'#f5a623',requests:'#7b61ff',uptime:'#00c28e'
  };
  for(const [k,vals] of Object.entries(_sparkHistory)){
    const svgEl=$('sp-'+k);
    if(svgEl)drawSparkline(svgEl,vals,colorMap[k]||'#7b61ff');
  }
}

/* ── Graph ───────────────────────────────────────────────────────────────── */
let _graphAllNodes=[];
let _graphHiddenKind=null;

async function loadGraph(){
  if(!_proj){toast('Select a project first','err');return;}
  const emptyEl=$('graph-empty');
  if(emptyEl)emptyEl.textContent='Loading graph…';
  if(window.__graph){
    try{window.__graph.sigma.kill();}catch(_){}
    window.__graph=null;
  }
  $('graph-detail').innerHTML='<span style="color:var(--text-3)">Click a node to see details</span>';
  try{
    const r=await fetchWithTimeout(`/api/graph_export?project=${encodeURIComponent(_proj)}&format=json&max_nodes=2000`);
    if(!r.ok)throw new Error('HTTP '+r.status);
    const data=await r.json();
    const nodes=data.nodes||[];
    const edges=data.edges||[];
    _graphAllNodes=nodes;
    if(emptyEl)emptyEl.style.display='none';
    $('graph-node-count').textContent=`${nodes.length} nodes · ${edges.length} edges`;
    _renderGraph(nodes,edges);
  }catch(e){
    if(emptyEl){emptyEl.style.display='flex';emptyEl.textContent='Failed to load graph: '+e.message;}
    toast('Graph load error: '+e.message,'err');
  }
}

function _renderGraph(nodes,edges){
  if(typeof window.Graph==='undefined'||typeof window.Sigma==='undefined'){
    const el=$('graph-empty');
    if(el){el.style.display='flex';el.textContent='sigma-graph.min.js not loaded';}
    return;
  }
  const graph=new window.Graph({multi:false,allowSelfLoops:false});
  const kindColors={file:'#7b61ff',symbol:'#00d4ff',community:'#00c28e',default:'#8891b8'};
  nodes.forEach(n=>{
    const kind=(n.attributes&&n.attributes.kind)||n.kind||'default';
    graph.addNode(n.id||n.key||String(n),{
      label:n.label||n.id||String(n),
      kind:kind,
      size:4,
      color:kindColors[kind]||kindColors.default,
      x:Math.random()*200-100,
      y:Math.random()*200-100,
    });
  });
  edges.forEach((e,i)=>{
    try{graph.addEdge(e.source||e.from,e.target||e.to,{color:'rgba(255,255,255,.08)',size:1});}catch(_){}
  });
  const container=$('graph-canvas');
  const sigma=new window.Sigma(graph,container,{
    renderEdgeLabels:false,
    defaultEdgeColor:'rgba(255,255,255,.08)',
    labelColor:{color:'#8891b8'},
    labelSize:10,
    labelWeight:'normal',
    stagePadding:20,
  });
  window.__graph={sigma,graph};
  sigma.on('clickNode',({node})=>{_showNodeDetail(node);});
  applyGraphLayout($('graph-layout-sel').value);
}

function applyGraphLayout(name){
  if(!window.__graph)return;
  const {sigma,graph}=window.__graph;
  if(name==='circular'&&window.circularLayout){
    const pos=window.circularLayout(graph);
    graph.forEachNode(n=>{const p=pos[n];if(p){graph.setNodeAttribute(n,'x',p.x);graph.setNodeAttribute(n,'y',p.y);}});
    sigma.refresh();
  }else if(name==='fa2'&&window.FA2Layout){
    if(window.__graph.layout){try{window.__graph.layout.stop();}catch(_){}}
    const layout=new window.FA2Layout(graph,{settings:{gravity:1,scalingRatio:2,slowDown:5,barnesHutOptimize:true}});
    window.__graph.layout=layout;
    layout.start();
    setTimeout(()=>{try{layout.stop();}catch(_){}},1500);
  }
}

function applyGraphFilter(kind){
  if(!window.__graph)return;
  const {sigma,graph}=window.__graph;
  _graphHiddenKind=kind==='all'?null:kind;
  graph.forEachNode(n=>{
    const nKind=graph.getNodeAttribute(n,'kind')||'default';
    const hidden=_graphHiddenKind&&nKind!==_graphHiddenKind;
    graph.setNodeAttribute(n,'hidden',hidden);
  });
  sigma.refresh();
}

function searchGraphNode(query){
  if(!window.__graph)return;
  const {sigma,graph}=window.__graph;
  const q=query.trim().toLowerCase();
  graph.forEachNode(n=>{
    const lbl=(graph.getNodeAttribute(n,'label')||'').toLowerCase();
    const hidden=_graphHiddenKind&&(graph.getNodeAttribute(n,'kind')||'default')!==_graphHiddenKind;
    if(hidden){graph.setNodeAttribute(n,'hidden',true);return;}
    if(!q){graph.setNodeAttribute(n,'hidden',false);graph.setNodeAttribute(n,'color',_nodeColor(n));return;}
    const match=lbl.includes(q);
    graph.setNodeAttribute(n,'hidden',false);
    graph.setNodeAttribute(n,'color',match?'#fff':'rgba(255,255,255,.15)');
  });
  sigma.refresh();
}

function _nodeColor(nodeId){
  if(!window.__graph)return '#8891b8';
  const kind=window.__graph.graph.getNodeAttribute(nodeId,'kind')||'default';
  return {file:'#7b61ff',symbol:'#00d4ff',community:'#00c28e',default:'#8891b8'}[kind]||'#8891b8';
}

function _showNodeDetail(nodeId){
  if(!window.__graph)return;
  const {graph}=window.__graph;
  const label=graph.getNodeAttribute(nodeId,'label')||nodeId;
  const kind=graph.getNodeAttribute(nodeId,'kind')||'node';
  const degree=graph.degree(nodeId);
  const neighbours=[];
  graph.forEachNeighbor(nodeId,nb=>neighbours.push({id:nb,label:graph.getNodeAttribute(nb,'label')||nb}));
  const nbHtml=neighbours.slice(0,10).map(nb=>`<span class="gd-nb" onclick="_showNodeDetail(${JSON.stringify(nb.id)})">${esc(nb.label)}</span>`).join('');
  $('graph-detail').innerHTML=`<div class="gd-name">${esc(label)}</div><div class="gd-meta">${esc(kind)} · degree ${degree}</div><div class="gd-neighbours">${nbHtml||'<span style="color:var(--text-3)">No neighbours</span>'}</div>`;
}

/* ── Wiki ────────────────────────────────────────────────────────────────── */
let _wikiPages=[];

async function loadWiki(){
  if(!_proj)return;
  const pagesEl=$('wiki-pages');
  pagesEl.innerHTML='<div style="padding:12px 14px;color:var(--text-3);font-size:.76rem">Loading…</div>';
  $('wiki-content').innerHTML='<div class="wiki-empty">← Pick a page to read</div>';
  $('wiki-lint-panel').style.display='none';
  try{
    const r=await fetchWithTimeout(`/api/wiki?project=${encodeURIComponent(_proj)}`);
    if(!r.ok)throw new Error('HTTP '+r.status);
    const d=await r.json();
    _wikiPages=d.pages||[];
    _renderWikiPages(_wikiPages,null);
    if(_wikiPages.length)loadWikiLint();
  }catch(e){
    pagesEl.innerHTML='<div style="padding:12px 14px;color:var(--red);font-size:.76rem">'+esc(e.message)+'</div>';
  }
}

function _renderWikiPages(pages,activeSlug){
  const el=$('wiki-pages');
  if(!pages.length){el.innerHTML='<div style="padding:12px 14px;color:var(--text-3);font-size:.76rem">No wiki pages. Run the Wiki op to generate.</div>';return;}
  el.innerHTML=pages.map(p=>`<button class="wiki-page-link${p===activeSlug?' active':''}" onclick="openWikiPage(${JSON.stringify(p)})">${esc(p)}</button>`).join('');
}

async function openWikiPage(name){
  if(!_proj)return;
  _renderWikiPages(_wikiPages,name);
  const content=$('wiki-content');
  content.innerHTML='<div class="wiki-empty" style="color:var(--text-3)">Loading…</div>';
  try{
    const r=await fetchWithTimeout(`/api/wiki/page?project=${encodeURIComponent(_proj)}&name=${encodeURIComponent(name)}`);
    if(!r.ok)throw new Error('HTTP '+r.status);
    const d=await r.json();
    content.innerHTML=mdSafe(d.content||'');
  }catch(e){
    content.innerHTML='<div class="wiki-empty" style="color:var(--red)">'+esc(e.message)+'</div>';
  }
}

function searchWiki(q){
  const lower=q.trim().toLowerCase();
  const filtered=lower?_wikiPages.filter(p=>p.toLowerCase().includes(lower)):_wikiPages;
  _renderWikiPages(filtered,null);
}

async function loadWikiLint(){
  if(!_proj)return;
  try{
    const r=await fetchWithTimeout(`/api/wiki_lint?project=${encodeURIComponent(_proj)}`);
    if(!r.ok)return;
    const d=await r.json();
    const warns=(d.warnings||d.issues||d.errors||[]);
    const count=warns.length||(d.warning_count||0);
    if(count>0){
      $('wiki-lint-count').textContent=count;
      $('wiki-lint-items').innerHTML=warns.slice(0,20).map(w=>`<div class="wiki-lint-item">${esc(typeof w==='string'?w:JSON.stringify(w))}</div>`).join('');
      $('wiki-lint-panel').style.display='block';
    }
  }catch(_){}
}

function toggleWikiLint(){
  const items=$('wiki-lint-items');
  const open=items.classList.toggle('open');
  $('wiki-lint-chevron').textContent=open?'▴':'▾';
}

/* ── Admin ───────────────────────────────────────────────────────────────── */
let _adminSSE=null;
function _setupAdminSSE(){
  if(_adminSSE)return;
  _adminSSE=new EventSource('/api/events/stream');
  _adminSSE.onmessage=e=>{
    let evt;try{evt=JSON.parse(e.data);}catch{return;}
    if(evt.type==='job')_upsertJobChip(evt);
  };
  _adminSSE.onerror=()=>{_adminSSE=null;};
}

function _upsertJobChip(evt){
  const id='chip-'+evt.job_id;
  const label=evt.action||evt.job_id;
  const status=evt.status||'queued';
  const icon={queued:'⏸',running:'⟳',done:'✓',error:'✗'}[status]||'';
  let chip=document.getElementById(id);
  if(!chip){
    chip=document.createElement('span');
    chip.id=id;
    chip.className='admin-chip';
    chip.dataset.jobId=evt.job_id;
    $('admin-job-chips').appendChild(chip);
  }
  chip.className='admin-chip '+status;
  chip.innerHTML=esc(label)+' '+icon+' <button class="chip-dismiss" onclick="this.parentElement.remove()" title="Dismiss">✕</button>';
}

async function loadAutoPipeline(){
  try{
    const r=await fetchWithTimeout('/api/auto_pipeline_status');
    if(!r.ok)return;
    const d=await r.json();
    const events=(d.events||[]).slice(-20).reverse();
    const log=$('admin-autopipeline-log');
    if(!events.length){log.innerHTML='<div style="color:var(--text-3)">No auto-pipeline events yet</div>';return;}
    log.innerHTML=events.map(ev=>{
      const ts=(ev.scheduled_at||ev.ts||'').slice(0,16);
      const msg=ev.project?ev.project.split('/').slice(-2).join('/')+' → '+(ev.status||'?'):'event';
      return `<div class="ap-entry"><span class="ap-ts">${esc(ts)}</span><span class="ap-msg">${esc(msg)}</span></div>`;
    }).join('');
  }catch(_){}
}

async function loadAdmin(){
  _setupAdminSSE();
  loadAutoPipeline();
  const projs=await loadProjects();
  const tbody=$('projects-body');
  tbody.innerHTML=projs.map(p=>{
    const name=p.path.split('/').slice(-2).join('/');
    const active=p.path===_proj;
    const chunks=p.chunks!=null?fmtNum(p.chunks):'—';
    const w=p.watching?'<span style="color:var(--green)">●</span>':'<span style="color:var(--text-3)">○</span>';
    return `<tr class="${active?'active-row':''}">
      <td><a style="cursor:pointer;color:var(--purple)" onclick="switchProject(${JSON.stringify(p.path)})">${esc(name)}</a></td>
      <td>${chunks}</td>
      <td><span style="color:var(--green);font-size:.7rem">${active?'active':''}</span></td>
      <td>${w}</td>
    </tr>`;
  }).join('');
  if(!projs.length)tbody.innerHTML='<tr><td colspan="4" style="color:var(--text-3);padding:12px">No projects indexed</td></tr>';
}

function opLog(msg,cls=''){
  const el=$('op-log');
  el.insertAdjacentHTML('beforeend', `<div class="${cls}">${esc(msg)}</div>`);
  el.scrollTop=el.scrollHeight;
}

async function runVacuum(){
  if(!_proj){toast('Select a project first','err');return;}
  opLog('Running vacuum…');
  try{
    const r=await fetchWithTimeout(`/api/vacuum?project=${encodeURIComponent(_proj)}`,{method:'POST'});
    const d=await r.json();
    opLog(d.message||JSON.stringify(d),'ok');
    toast('Vacuum complete','info');
  }catch(e){opLog('Error: '+e.message,'err');}
}

async function runDedup(){
  if(!_proj){toast('Select a project first','err');return;}
  opLog('Running dedup…');
  try{
    const r=await fetchWithTimeout(`/api/dedup?project=${encodeURIComponent(_proj)}`,{method:'POST'});
    const d=await r.json();
    opLog(d.message||JSON.stringify(d),'ok');
    toast('Dedup complete','info');
  }catch(e){opLog('Error: '+e.message,'err');}
}

async function runReindex(){
  if(!_proj){toast('Select a project first','err');return;}
  opLog('Re-indexing (this may take a while)…');
  try{
    const r=await fetchWithTimeout(`/api/build_hierarchy?project=${encodeURIComponent(_proj)}`,{method:'POST'});
    const d=await r.json();
    opLog(d.message||'Job submitted','ok');
    toast('Re-index job started','info');
  }catch(e){opLog('Error: '+e.message,'err');}
}

async function runEnrich(){
  if(!_proj){toast('Select a project first','err');return;}
  opLog('Enriching hierarchy…');
  try{
    const r=await fetchWithTimeout(`/api/enrich_hierarchy?project=${encodeURIComponent(_proj)}`,{method:'POST'});
    const d=await r.json();
    opLog(d.message||'Job submitted','ok');
    toast('Enrich job started','info');
  }catch(e){opLog('Error: '+e.message,'err');}
}

async function runWiki(){
  if(!_proj){toast('Select a project first','err');return;}
  opLog('Generating wiki…');
  try{
    const r=await fetchWithTimeout(`/api/build_hierarchy?project=${encodeURIComponent(_proj)}&action=wiki`,{method:'POST'});
    const d=await r.json();
    opLog(d.message||'Job submitted','ok');
    toast('Wiki generation started','info');
  }catch(e){opLog('Error: '+e.message,'err');}
}

/* ── Chat ────────────────────────────────────────────────────────────────── */
function askQuestion(q){
  switchView('chat');
  $('chat-in').value=q;
  sendChat();
}

async function sendChat(){
  if(_chatInFlight)return;
  if(!_proj){toast('Select a project first','err');return;}
  const inp=$('chat-in');
  const query=inp.value.trim();
  if(!query)return;
  _chatInFlight=true;
  inp.value='';
  inp.style.height='auto';
  $('send-btn').disabled=true;

  appendMsg('user',query);
  const thinkId=appendMsg('ai','Thinking…','thinking');
  const thinkStart=Date.now();

  try{
    const r=await fetch('/api/chat_stream',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({project:_proj,query,history:_chatHistory.slice(-8)}),
    });
    if(!r.ok||!r.body){
      const d=await r.json().catch(()=>({}));
      removeMsg(thinkId);
      appendMsg('ai','Error: '+(d.error||r.statusText),'ai-err');
      return;
    }

    const reader=r.body.getReader();
    const decoder=new TextDecoder();
    let buf='';
    let streamMsgId=null;
    let accumulated='';

    // Parse SSE format: events are delimited by \n\n; each line is "data: <json>"
    const processEvent=(raw)=>{
      const dataLine=raw.split('\n').find(l=>l.startsWith('data:'));
      if(!dataLine)return;
      let evt;
      try{evt=JSON.parse(dataLine.slice(5).trim());}catch{return;}
      if(evt.type==='thinking'){
        const el=$(thinkId);
        if(el){const s=Math.round((Date.now()-thinkStart)/1000);el.querySelector('.msg-bubble').textContent='Thinking… ('+s+'s)';}
      }else if(evt.type==='token'){
        accumulated+=String(evt.text||'');
        if(!streamMsgId){
          removeMsg(thinkId);
          streamMsgId=appendStreamMsg(accumulated);
        }else{
          updateStreamMsg(streamMsgId,accumulated);
        }
      }else if(evt.type==='error'){
        removeMsg(thinkId);
        const errMsg=evt.message||'An error occurred';
        if(streamMsgId){finalizeStreamMsg(streamMsgId,{intent:evt.intent,sources:[],elapsed:0,model:''});}
        appendMsg('ai','Error: '+errMsg,'ai-err');
        toast(errMsg,'err');
      }else if(evt.type==='done'){
        const meta={intent:evt.intent,sources:evt.sources,elapsed:evt.elapsed_ms,model:evt.model};
        if(streamMsgId){finalizeStreamMsg(streamMsgId,meta);}
        else{removeMsg(thinkId);appendMsg('ai',accumulated||'(no response)','',meta);}
        _chatHistory.push({role:'user',content:query});
        _chatHistory.push({role:'assistant',content:String(accumulated)});
      }
    };

    const loop=async()=>{
      while(true){
        const {done,value}=await reader.read();
        if(done)break;
        buf+=decoder.decode(value,{stream:true});
        // Split on double-newline (SSE event boundary)
        const events=buf.split('\n\n');
        buf=events.pop()||'';
        for(const ev of events){if(ev.trim())processEvent(ev);}
      }
      // Flush any remaining buffer
      if(buf.trim())processEvent(buf);
    };
    await loop();
    if(!streamMsgId){removeMsg(thinkId);appendMsg('ai',accumulated||'(no response)');}
  }catch(e){
    removeMsg(thinkId);
    appendMsg('ai','Network error: '+e.message,'ai-err');
  }finally{
    $('send-btn').disabled=false;
    _chatInFlight=false;
  }
}

function appendStreamMsg(text){
  const id='msg-'+(++_msgSeq);
  const hist=$('chat-history');
  hist.insertAdjacentHTML('beforeend',
    `<div class="msg ai" id="${id}"><div class="msg-bubble" id="${id}-bubble">${mdSafe(text)}</div></div>`);
  hist.scrollTop=hist.scrollHeight;
  return id;
}

function updateStreamMsg(id,text){
  const bubble=$(id+'-bubble');
  if(bubble){bubble.innerHTML=mdSafe(text);$('chat-history').scrollTop=$('chat-history').scrollHeight;}
}

function finalizeStreamMsg(id,meta){
  const el=$(id);
  if(!el)return;
  const tags=[];
  if(meta.intent)tags.push(`<span class="intent-tag">${esc(meta.intent)}</span>`);
  if(meta.elapsed)tags.push(`<span class="elapsed">${meta.elapsed}ms</span>`);
  if(meta.model)tags.push(`<span class="elapsed">${esc(meta.model)}</span>`);
  (meta.sources||[]).slice(0,4).forEach(s=>{
    const base=s.split('/').pop();
    tags.push(`<span class="src-chip" title="${esc(s)}">${esc(base)}</span>`);
  });
  if(tags.length)el.insertAdjacentHTML('beforeend',`<div class="msg-meta">${tags.join('')}</div>`);
  $('chat-history').scrollTop=$('chat-history').scrollHeight;
}

function appendMsg(role,text,extraClass='',meta=null){
  const id='msg-'+(++_msgSeq);
  const hist=$('chat-history');
  const cls=role==='user'?'user':extraClass?'ai '+extraClass:'ai';
  let metaHtml='';
  if(meta&&role==='ai'&&extraClass!=='thinking'){
    const tags=[];
    if(meta.intent)tags.push(`<span class="intent-tag">${esc(meta.intent)}</span>`);
    if(meta.elapsed)tags.push(`<span class="elapsed">${meta.elapsed}ms</span>`);
    if(meta.model)tags.push(`<span class="elapsed">${esc(meta.model)}</span>`);
    const srcs=(meta.sources||[]).slice(0,4);
    srcs.forEach(s=>{
      const base=s.split('/').pop();
      tags.push(`<span class="src-chip" title="${esc(s)}">${esc(base)}</span>`);
    });
    if(tags.length)metaHtml=`<div class="msg-meta">${tags.join('')}</div>`;
  }
  const bubbleContent=cls==='user'?esc(text):mdSafe(text);
  hist.insertAdjacentHTML('beforeend', `<div class="msg ${cls}" id="${id}"><div class="msg-bubble">${bubbleContent}</div>${metaHtml}</div>`);
  hist.scrollTop=hist.scrollHeight;
  return id;
}

function removeMsg(id){
  const el=$(id);
  if(el)el.remove();
}

/* ── Command palette ─────────────────────────────────────────────────────── */
const _CMD_ITEMS=[
  {label:'Pulse — KPI dashboard',action:()=>switchView('pulse'),cat:'view'},
  {label:'Chat — Ask the codebase',action:()=>switchView('chat'),cat:'view'},
  {label:'Admin — Projects & ops',action:()=>switchView('admin'),cat:'view'},
  {label:'Graph — Knowledge graph',action:()=>switchView('graph'),cat:'view'},
  {label:'Wiki — Knowledge base pages',action:()=>switchView('wiki'),cat:'view'},
  {label:'Run Vacuum',action:runVacuum,cat:'op'},
  {label:'Run Dedup',action:runDedup,cat:'op'},
  {label:'Re-index project',action:runReindex,cat:'op'},
  {label:'Enrich hierarchy',action:runEnrich,cat:'op'},
  {label:'Generate wiki',action:runWiki,cat:'op'},
  {label:'Refresh Admin',action:loadAdmin,cat:'op'},
  {label:'Refresh Pulse',action:loadPulse,cat:'op'},
];

function showCmdPalette(){
  $('cmd-overlay').classList.remove('hidden');
  $('cmd-input').value='';
  filterCmd('');
  $('cmd-input').focus();
}
function hideCmdPalette(e){
  if(!e||e.target===$('cmd-overlay'))$('cmd-overlay').classList.add('hidden');
}

function filterCmd(q){
  const lower=q.toLowerCase();
  const items=_CMD_ITEMS.filter(i=>i.label.toLowerCase().includes(lower));
  _cmdIdx=0;
  $('cmd-results').innerHTML=items.map((i,n)=>`
    <li class="${n===0?'hi':''}" onclick="runCmd(${_CMD_ITEMS.indexOf(i)})">
      ${esc(i.label)}<span class="cr-cat">${esc(i.cat)}</span>
    </li>`).join('');
}

function runCmd(idx){
  const item=_CMD_ITEMS[idx];
  if(item){item.action();hideCmdPalette();}
}

function cmdKey(e){
  const lis=$('cmd-results').querySelectorAll('li');
  if(e.key==='ArrowDown'){e.preventDefault();_cmdIdx=Math.min(_cmdIdx+1,lis.length-1);}
  else if(e.key==='ArrowUp'){e.preventDefault();_cmdIdx=Math.max(_cmdIdx-1,0);}
  else if(e.key==='Enter'){
    const hi=$('cmd-results').querySelector('.hi');
    if(hi){const found=_CMD_ITEMS.find(i=>i.label===hi.childNodes[0].textContent.trim());if(found){found.action();hideCmdPalette();}}
    return;
  }
  else if(e.key==='Escape'){hideCmdPalette();return;}
  lis.forEach((l,i)=>l.classList.toggle('hi',i===_cmdIdx));
}

/* ── Theme ────────────────────────────────────────────────────────────────── */
const _LIGHT={
  '--bg':'#f5f7ff','--surface':'#fff','--surface-2':'#f0f2fa','--surface-3':'#e8ecf8',
  '--border':'rgba(0,0,0,.08)','--border-2':'rgba(0,0,0,.14)',
  '--text':'#1a1f3c','--text-2':'#4a5280','--text-3':'#8891b8',
  '--purple':'#6a4ddb','--cyan':'#0098b8',
};
let _dark=true;
function toggleTheme(){
  _dark=!_dark;
  const r=document.documentElement;
  if(_dark){Object.keys(_LIGHT).forEach(k=>r.style.removeProperty(k));}
  else{Object.entries(_LIGHT).forEach(([k,v])=>r.style.setProperty(k,v));}
  $('theme-btn').textContent=_dark?'☀':'🌙';
}

/* ── Auto-grow textarea ────────────────────────────────────────────────────── */
$('chat-in').addEventListener('input',function(){
  this.style.height='auto';
  this.style.height=Math.min(this.scrollHeight,160)+'px';
});
$('chat-in').addEventListener('keydown',e=>{
  if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();sendChat();}
});

/* ── Keyboard shortcuts ────────────────────────────────────────────────────── */
document.addEventListener('keydown',e=>{
  if((e.metaKey||e.ctrlKey)&&e.key==='k'){e.preventDefault();showCmdPalette();}
  if(e.key==='Escape')hideCmdPalette();
});

/* ── Boot ────────────────────────────────────────────────────────────────── */
(async()=>{
  try{
    await loadProjects();
    await loadPulse();
  }catch(err){
    setDot('err');
    toast('Failed to connect to daemon: '+err.message,'err');
  }
  // Auto-refresh Pulse every 20s
  setInterval(()=>{
    if(document.getElementById('view-pulse').classList.contains('active'))loadPulse();
  },20000);
})();
</script>
</body>
</html>"""


def get_dashboard_html() -> str:
    return _DASHBOARD_HTML
