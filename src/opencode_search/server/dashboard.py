"""5-view dashboard HTML (pulse/chat/admin/wiki/graph) — served from static file."""
from pathlib import Path

_STATIC = Path(__file__).parent / "static" / "dashboard.html"


def html() -> str:
    if _STATIC.exists():
        return _STATIC.read_text()
    return """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>opencode-search</title>
<style>
body{font-family:sans-serif;margin:0}
nav{background:#1a1a2e;padding:10px 16px;display:flex;gap:16px}
nav a{color:#a0aec0;text-decoration:none;padding:4px 8px;border-radius:4px;cursor:pointer}
nav a.active,nav a:hover{color:#fff;background:#2d3748}
.view{display:none;padding:24px}
.view.active{display:block}
h2{margin-top:0;color:#2d3748}
textarea{width:100%;height:80px;padding:8px;border:1px solid #ddd;border-radius:4px}
button{margin-top:8px;padding:6px 16px;cursor:pointer}
</style>
</head>
<body>
<nav id="nav">
  <a onclick="show('pulse')" class="active" id="tab-pulse">Pulse</a>
  <a onclick="show('chat')" id="tab-chat">Chat</a>
  <a onclick="show('admin')" id="tab-admin">Admin</a>
  <a onclick="show('wiki')" id="tab-wiki">Wiki</a>
  <a onclick="show('graph')" id="tab-graph">Graph</a>
</nav>
<div id="pulse" class="view active"><h2>Pulse</h2><p id="status">Loading...</p></div>
<div id="chat" class="view"><h2>Chat</h2>
  <textarea id="chat-input" placeholder="Ask about the codebase..."></textarea>
  <button onclick="sendChat()">Send</button>
  <div id="chat-output" style="margin-top:12px;white-space:pre-wrap"></div>
</div>
<div id="admin" class="view"><h2>Admin</h2><div id="projects-list">Loading projects...</div></div>
<div id="wiki" class="view"><h2>Wiki</h2><div id="wiki-content">Select a project to view the wiki.</div></div>
<div id="graph" class="view"><h2>Graph</h2><canvas id="graph-canvas" width="800" height="600"></canvas></div>
<script>
function show(id){
  document.querySelectorAll('.view').forEach(v=>v.classList.remove('active'));
  document.querySelectorAll('nav a').forEach(a=>a.classList.remove('active'));
  document.getElementById(id).classList.add('active');
  document.getElementById('tab-'+id).classList.add('active');
}
async function sendChat(){
  const q=document.getElementById('chat-input').value;
  if(!q)return;
  const out=document.getElementById('chat-output');
  out.textContent='Thinking...';
  const r=await fetch('/api/ask',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({query:q})});
  const d=await r.json();
  out.textContent=d.answer||JSON.stringify(d);
}
fetch('/api/projects').then(r=>r.json()).then(d=>{
  const el=document.getElementById('projects-list');
  if(!d.projects||!d.projects.length){el.textContent='No projects indexed.';return;}
  el.innerHTML=d.projects.map(p=>`<div><b>${p.path}</b></div>`).join('');
});
fetch('/healthz').then(r=>r.json()).then(d=>document.getElementById('status').textContent='Status: '+d.status);
</script>
</body>
</html>
"""
