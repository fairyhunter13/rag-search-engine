"""Self-contained dashboard HTML — imported by dashboard.py.

Datadog DRUIDS design: 3-view top navbar (Pulse / Chat / Admin).
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
.msg-bubble{padding:10px 14px;border-radius:var(--radius);font-size:.83rem;line-height:1.55;white-space:pre-wrap;word-break:break-word}
.msg.user .msg-bubble{background:rgba(123,97,255,.18);border:1px solid rgba(123,97,255,.3);color:var(--text)}
.msg.ai .msg-bubble{background:var(--surface);border:1px solid var(--border);color:var(--text)}
.msg.ai.thinking .msg-bubble{color:var(--text-3);font-style:italic}
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
/* ── Scrollbar ──────────────────────────────────────────────────────────── */
::-webkit-scrollbar{width:4px;height:4px}::-webkit-scrollbar-track{background:transparent}::-webkit-scrollbar-thumb{background:var(--border-2);border-radius:2px}
</style>
</head>
<body>

<!-- Top navbar -->
<nav class="topnav">
  <span class="brand">⬡ opencode-search</span>
  <div class="nav-views">
    <button class="vbtn active" id="vbtn-pulse" onclick="switchView('pulse')">Pulse</button>
    <button class="vbtn" id="vbtn-chat" onclick="switchView('chat')">Chat</button>
    <button class="vbtn" id="vbtn-admin" onclick="switchView('admin')">Admin</button>
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
        </div>
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

/* ── View switching ──────────────────────────────────────────────────────── */
function switchView(name){
  ['pulse','chat','admin'].forEach(v=>{
    $('view-'+v).classList.toggle('active',v===name);
    $('vbtn-'+v).classList.toggle('active',v===name);
  });
  if(name==='pulse')loadPulse();
  else if(name==='admin')loadAdmin();
  else if(name==='chat'&&$('chat-in'))$('chat-in').focus();
}

/* ── Project selector ────────────────────────────────────────────────────── */
async function loadProjects(){
  const r=await fetch('/api/projects');
  const d=await r.json();
  const sel=$('project-sel');
  const projs=d.projects||[];
  sel.innerHTML=projs.length
    ?projs.map(p=>`<option value="${esc(p.path)}">${esc(p.path.split('/').slice(-2).join('/'))}</option>`).join('')
    :'<option value="">No projects indexed</option>';
  if(!_proj&&projs.length)_proj=projs[0].path;
  if(_proj)sel.value=_proj;
  return projs;
}

function switchProject(path){
  _proj=path;
  _sparkHistory={};
  const active=document.querySelector('.view.active');
  if(active&&active.id==='view-pulse')loadPulse();
  else if(active&&active.id==='view-admin')loadAdmin();
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
      fetch(`/api/overview?project=${encodeURIComponent(_proj)}`).then(r=>r.json()),
      fetch(`/api/kb_health?project=${encodeURIComponent(_proj)}`).then(r=>r.json()),
      fetch('/api/metrics').then(r=>r.json()),
      fetch(`/api/suggested_questions?project=${encodeURIComponent(_proj)}`).then(r=>r.json()),
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
      ep>=80?'ok':ep>=40?'warn':'err',
      ep>=80?'ok':ep>=40?'warn':'err',
      `${kbD.enriched_communities||0} / ${kbD.total_communities||0} communities`
    );

    // Wiki tile
    const wikiCt=kbD.wiki_page_count;
    setTile('wiki',wikiCt,wikiCt>0?'ok':'warn',wikiCt>0?'ok':'warn',
      'knowledge base pages');

    // Requests + uptime tiles
    const metD=met.status==='fulfilled'?met.value:{};
    const reqs=metD.total_requests||metD.requests||null;
    const errors=metD.errors||0;
    const errRate=reqs?Math.round(errors/reqs*100):0;
    setTile('requests',reqs,errRate<5?'ok':errRate<20?'warn':'err',
      errRate<5?'ok':errRate<20?'warn':'err',
      `${errors} errors · ${metD.connected_clients||0} clients`
    );
    const uptS=metD.uptime_s;
    const uptStr=uptS!=null?fmtUptime(uptS):'—';
    const watchers=metD.active_watchers||metD.watchers||0;
    $('kpi-uptime').textContent=uptStr;
    $('ks-uptime').textContent=`active watchers: ${watchers}`;
    $('tile-uptime').className='tile ok';

    setDot('ok');

    // Sparklines (push current value into rolling history)
    pushSpark('files',fileCount);
    pushSpark('communities',comms);
    pushSpark('enrichment',ep);
    pushSpark('wiki',wikiCt);
    pushSpark('requests',reqs);
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
      ?qs.map(q=>`<button class="sq-btn" data-q="${esc(q)}" onclick="askQuestion(this.dataset.q)">${esc(q)}</button>`).join('')
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

/* ── Admin ───────────────────────────────────────────────────────────────── */
async function loadAdmin(){
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
    const r=await fetch(`/api/vacuum?project=${encodeURIComponent(_proj)}`,{method:'POST'});
    const d=await r.json();
    opLog(d.message||JSON.stringify(d),'ok');
    toast('Vacuum complete','info');
  }catch(e){opLog('Error: '+e.message,'err');}
}

async function runDedup(){
  if(!_proj){toast('Select a project first','err');return;}
  opLog('Running dedup…');
  try{
    const r=await fetch(`/api/dedup?project=${encodeURIComponent(_proj)}`,{method:'POST'});
    const d=await r.json();
    opLog(d.message||JSON.stringify(d),'ok');
    toast('Dedup complete','info');
  }catch(e){opLog('Error: '+e.message,'err');}
}

async function runReindex(){
  if(!_proj){toast('Select a project first','err');return;}
  opLog('Re-indexing (this may take a while)…');
  try{
    const r=await fetch(`/api/build_hierarchy?project=${encodeURIComponent(_proj)}`,{method:'POST'});
    const d=await r.json();
    opLog(d.message||'Job submitted','ok');
    toast('Re-index job started','info');
  }catch(e){opLog('Error: '+e.message,'err');}
}

async function runEnrich(){
  if(!_proj){toast('Select a project first','err');return;}
  opLog('Enriching hierarchy…');
  try{
    const r=await fetch(`/api/enrich_hierarchy?project=${encodeURIComponent(_proj)}`,{method:'POST'});
    const d=await r.json();
    opLog(d.message||'Job submitted','ok');
    toast('Enrich job started','info');
  }catch(e){opLog('Error: '+e.message,'err');}
}

async function runWiki(){
  if(!_proj){toast('Select a project first','err');return;}
  opLog('Generating wiki…');
  try{
    const r=await fetch(`/api/build_hierarchy?project=${encodeURIComponent(_proj)}&action=wiki`,{method:'POST'});
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
    `<div class="msg ai" id="${id}"><div class="msg-bubble" id="${id}-bubble">${esc(text)}</div></div>`);
  hist.scrollTop=hist.scrollHeight;
  return id;
}

function updateStreamMsg(id,text){
  const bubble=$(id+'-bubble');
  if(bubble){bubble.textContent=text;$('chat-history').scrollTop=$('chat-history').scrollHeight;}
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
  const cls=role==='user'?'user':extraClass.includes('thinking')?'ai thinking':'ai';
  let metaHtml='';
  if(meta&&role==='ai'&&!extraClass.includes('thinking')){
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
  hist.insertAdjacentHTML('beforeend', `<div class="msg ${cls}" id="${id}"><div class="msg-bubble">${esc(text)}</div>${metaHtml}</div>`);
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
    if(hi){const idx=Array.from(hi.parentNode.children).indexOf(hi);const txt=hi.querySelector('span')?.previousSibling?.textContent?.trim();const found=_CMD_ITEMS.find(i=>i.label===hi.childNodes[0].textContent.trim());if(found){found.action();hideCmdPalette();}}
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
