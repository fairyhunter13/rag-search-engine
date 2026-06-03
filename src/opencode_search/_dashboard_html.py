"""Self-contained dashboard HTML — imported by dashboard.py.

Single file, no CDN, no build step.  All CSS and JS are inline.
"""
from __future__ import annotations

_DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en" data-theme="light">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>opencode-search</title>
<script src="/static/chart.min.js"></script>
<script src="/static/sigma-graph.min.js"></script>
<style>
/* ── Design tokens — Datadog-style ─────────────────────────────────────────── */
:root{
  --bg:#0b0e1a;--sidebar-bg:#10131f;--surface:#151929;--surface-2:#1b1f32;--surface-3:#0b0e1a;
  --border:#222844;--border-2:#343d6a;
  --text:#e4e8f7;--text-2:#8891b8;--text-3:#4e5880;
  --accent:#7b61ff;--accent-2:#5742d4;
  --green:#00c28e;--green-bg:#002318;
  --amber:#ffb800;--amber-bg:#271f00;
  --red:#ff4060;--red-bg:#250010;
  --cyan:#00d4ff;--purple:#9b6dff;
  --trans:150ms ease;--radius:6px;--radius-lg:8px;--sidebar-w:224px;
}
[data-theme="light"]{
  --bg:#f5f7ff;--sidebar-bg:#fff;--surface:#fff;--surface-2:#f0f2fa;--surface-3:#e8ecf8;
  --border:#d8ddf0;--border-2:#b0b8d8;
  --text:#1a1f3c;--text-2:#4a5280;--text-3:#8891b8;
  --accent:#5b3de0;--accent-2:#4230b0;
  --green:#008060;--green-bg:#d0f5e8;
  --amber:#b07800;--amber-bg:#fff5cc;
  --red:#d4003a;--red-bg:#ffe0e8;
}
/* ── Reset ─────────────────────────────────────────────────────────────────── */
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%}
body{font-family:'Inter','Segoe UI',system-ui,-apple-system,sans-serif;background:var(--bg);color:var(--text);overflow:hidden;-webkit-font-smoothing:antialiased}
/* ── App shell ──────────────────────────────────────────────────────────────── */
.app{display:flex;height:100vh}
/* ── Sidebar ────────────────────────────────────────────────────────────────── */
.sidebar{width:var(--sidebar-w);flex-shrink:0;background:var(--sidebar-bg);border-right:1px solid var(--border);display:flex;flex-direction:column;overflow:hidden;transition:width 200ms ease}
.sidebar.collapsed{width:48px}
.sidebar.collapsed .brand-name,.sidebar.collapsed .nav-group,.sidebar.collapsed .nav-label,.sidebar.collapsed .sb-project{display:none}
.sb-header{display:flex;align-items:center;padding:12px 14px;border-bottom:1px solid var(--border);gap:8px}
.brand{display:flex;align-items:center;gap:9px;color:var(--accent);font-weight:800;font-size:.88rem;text-decoration:none;letter-spacing:-.01em}
.brand-icon{font-size:1.1rem;flex-shrink:0;filter:drop-shadow(0 0 6px var(--accent))}
.brand-name{white-space:nowrap;overflow:hidden;flex:1;background:linear-gradient(90deg,var(--accent) 0%,var(--cyan) 100%);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
#sidebar-toggle{background:none;border:none;color:var(--text-3);cursor:pointer;font-size:1rem;padding:2px 4px;border-radius:4px;transition:color var(--trans);flex-shrink:0;margin-left:auto}
#sidebar-toggle:hover{color:var(--accent)}
.sb-project{padding:8px 10px;border-bottom:1px solid var(--border)}
.sb-project select{width:100%;background:var(--surface-2);border:1px solid var(--border);border-radius:var(--radius);color:var(--text);padding:5px 8px;font-size:.79rem;cursor:pointer;outline:none;transition:border-color var(--trans)}
.sb-project select:focus{border-color:var(--accent)}
.sb-nav{flex:1;overflow-y:auto;padding:6px 0;scrollbar-width:thin;scrollbar-color:var(--border) transparent}
.nav-group{font-size:.63rem;color:var(--text-3);text-transform:uppercase;letter-spacing:.12em;padding:12px 14px 3px;white-space:nowrap;font-weight:600}
.nav-btn{display:flex;align-items:center;gap:8px;width:100%;background:none;border:none;color:var(--text-2);padding:7px 14px;font-size:.81rem;cursor:pointer;border-radius:0;transition:background var(--trans),color var(--trans);text-align:left;white-space:nowrap;position:relative}
.nav-btn:hover{background:var(--surface-2);color:var(--text)}
.nav-btn.active{background:rgba(123,97,255,.12);color:var(--accent);font-weight:500}
.nav-btn.active::before{content:'';position:absolute;left:0;top:50%;transform:translateY(-50%);width:3px;height:18px;background:var(--accent);border-radius:0 2px 2px 0;box-shadow:0 0 8px var(--accent)}
.nav-icon{font-size:.9rem;flex-shrink:0;opacity:.8}
/* ── Main wrapper ───────────────────────────────────────────────────────────── */
.main-wrap{flex:1;display:flex;flex-direction:column;overflow:hidden;min-width:0}
/* ── Topbar ─────────────────────────────────────────────────────────────────── */
.topbar{height:52px;background:var(--surface);border-bottom:1px solid var(--border);display:flex;align-items:center;gap:10px;padding:0 16px;flex-shrink:0;box-shadow:0 1px 0 var(--border)}
.menu-btn{background:none;border:none;color:var(--text-3);cursor:pointer;font-size:1.1rem;padding:4px 6px;border-radius:var(--radius);transition:color var(--trans),background var(--trans);flex-shrink:0}
.menu-btn:hover{color:var(--text);background:var(--surface-2)}
.top-search{display:flex;gap:6px;flex:1;max-width:520px}
.top-search input{flex:1;background:var(--surface-2);border:1px solid var(--border);border-radius:var(--radius);color:var(--text);padding:7px 14px;font-size:.83rem;outline:none;transition:border-color var(--trans),box-shadow var(--trans)}
.top-search input:focus{border-color:var(--accent);box-shadow:0 0 0 3px rgba(123,97,255,.15)}
.top-search input::placeholder{color:var(--text-3)}
.top-search select{background:var(--surface-2);border:1px solid var(--border);border-radius:var(--radius);color:var(--text);padding:7px 10px;font-size:.79rem;cursor:pointer;flex-shrink:0;outline:none}
.top-right{display:flex;align-items:center;gap:10px;margin-left:auto}
.daemon-dot{width:9px;height:9px;border-radius:50%;background:var(--text-3);display:inline-block;flex-shrink:0;transition:background var(--trans)}
.daemon-dot.ok{background:var(--green);box-shadow:0 0 6px var(--green)}.daemon-dot.err{background:var(--red);box-shadow:0 0 6px var(--red)}
#daemon-status{font-size:.79rem;color:var(--text-3)}
.icon-btn{background:none;border:none;color:var(--text-3);cursor:pointer;font-size:1rem;padding:4px 8px;border-radius:var(--radius);transition:color var(--trans),background var(--trans)}
.icon-btn:hover{color:var(--text);background:var(--surface-2)}
/* ── Metric strip ───────────────────────────────────────────────────────────── */
.metric-strip{background:rgba(10,13,26,.6);border-bottom:1px solid var(--border);padding:4px 16px;display:flex;gap:8px;flex-wrap:wrap;flex-shrink:0;align-items:center}
.metric-pill{display:flex;align-items:center;gap:6px;background:var(--surface);border:1px solid var(--border);border-radius:20px;padding:3px 12px;font-size:.73rem;cursor:default;user-select:none;white-space:nowrap;transition:border-color var(--trans)}
.metric-pill.ok{border-color:rgba(0,194,142,.35);background:rgba(0,194,142,.06)}.metric-pill.ok .pill-val{color:var(--green)}
.metric-pill.warn{border-color:rgba(255,184,0,.3);background:rgba(255,184,0,.06)}.metric-pill.warn .pill-val{color:var(--amber)}
.metric-pill.err{border-color:rgba(255,64,96,.3);background:rgba(255,64,96,.06)}.metric-pill.err .pill-val{color:var(--red)}
.pill-val{font-weight:600;font-size:.74rem}.pill-lbl{color:var(--text-3);font-size:.69rem}
/* ── Content ────────────────────────────────────────────────────────────────── */
.content{flex:1;overflow-y:auto;padding:20px 22px;scrollbar-width:thin;scrollbar-color:var(--border) transparent}
.page{display:none}.page.active{display:block}
.page-title{font-size:1.1rem;font-weight:700;color:var(--text);margin-bottom:18px;display:flex;align-items:center;gap:10px;letter-spacing:-.01em}
.page-title::before{content:'';display:inline-block;width:3px;height:18px;background:var(--accent);border-radius:2px;box-shadow:0 0 8px var(--accent)}
/* ── Cards ──────────────────────────────────────────────────────────────────── */
.card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius-lg);padding:16px 18px;margin-bottom:14px;transition:border-color var(--trans),box-shadow var(--trans)}
.card:hover{border-color:var(--border-2);box-shadow:0 4px 24px rgba(0,0,0,.25)}
.card-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px}
.card-title{font-size:.71rem;font-weight:700;color:var(--text-3);text-transform:uppercase;letter-spacing:.1em}
/* ── Two-column layout ──────────────────────────────────────────────────────── */
.two-col{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media(max-width:900px){.two-col{grid-template-columns:1fr}}
/* ── KPI cards ──────────────────────────────────────────────────────────────── */
.kpi-row{display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:12px;margin-bottom:18px}
.kpi-card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius-lg);padding:16px 18px;position:relative;overflow:hidden;transition:border-color var(--trans),transform var(--trans),box-shadow var(--trans);border-top:3px solid var(--border-2)}
.kpi-card:hover{border-color:var(--border-2);border-top-color:var(--accent);transform:translateY(-2px);box-shadow:0 8px 24px rgba(0,0,0,.3)}
.kpi-val{font-size:2.4rem;font-weight:700;color:var(--text);line-height:1;margin:4px 0 6px;letter-spacing:-.02em}
.kpi-label{font-size:.7rem;color:var(--text-3);text-transform:uppercase;letter-spacing:.09em;font-weight:600}
.kpi-icon{position:absolute;right:14px;top:14px;font-size:1.4rem;opacity:.15}
.kpi-card.ok{border-top-color:var(--green);box-shadow:0 0 0 1px rgba(0,194,142,.12),0 4px 16px rgba(0,194,142,.06)}.kpi-card.ok .kpi-val{color:var(--green)}
.kpi-card.warn{border-top-color:var(--amber);box-shadow:0 0 0 1px rgba(255,184,0,.12),0 4px 16px rgba(255,184,0,.06)}.kpi-card.warn .kpi-val{color:var(--amber)}
.kpi-card.crit{border-top-color:var(--red);box-shadow:0 0 0 1px rgba(255,64,96,.18),0 4px 16px rgba(255,64,96,.1)}.kpi-card.crit .kpi-val{color:var(--red)}
.kpi-sparkline{margin-top:8px;height:30px;opacity:.7}
.kpi-trend{font-size:.69rem;color:var(--text-3);margin-top:2px}
.kpi-trend.up{color:var(--green)}.kpi-trend.down{color:var(--red)}
/* ── Tables ─────────────────────────────────────────────────────────────────── */
table{width:100%;border-collapse:collapse;font-size:.81rem}
th{text-align:left;padding:7px 12px;color:var(--text-3);border-bottom:1px solid var(--border);font-weight:700;font-size:.69rem;text-transform:uppercase;letter-spacing:.09em;background:rgba(0,0,0,.15)}
td{padding:7px 12px;border-bottom:1px solid var(--border)}
tr:hover td{background:var(--surface-2)}
/* ── Badges ─────────────────────────────────────────────────────────────────── */
.badge{display:inline-flex;align-items:center;padding:2px 9px;border-radius:99px;font-size:.69rem;font-weight:600;letter-spacing:.02em}
.badge.ok{background:rgba(0,194,142,.15);color:var(--green);border:1px solid rgba(0,194,142,.25)}
.badge.warn{background:rgba(255,184,0,.15);color:var(--amber);border:1px solid rgba(255,184,0,.25)}
.badge.err{background:rgba(255,64,96,.15);color:var(--red);border:1px solid rgba(255,64,96,.25)}
.badge.none{background:var(--surface-2);color:var(--text-3);border:1px solid var(--border)}
.badge.info{background:rgba(123,97,255,.15);color:var(--accent);border:1px solid rgba(123,97,255,.25)}
.badge.go{background:rgba(0,194,142,.15);color:var(--green);font-size:.95rem;padding:5px 16px;border:1px solid rgba(0,194,142,.35)}
.badge.nogo{background:rgba(255,64,96,.15);color:var(--red);font-size:.95rem;padding:5px 16px;border:1px solid rgba(255,64,96,.35)}
.badge.warn-lg{background:rgba(255,184,0,.15);color:var(--amber);font-size:.95rem;padding:5px 16px;border:1px solid rgba(255,184,0,.35)}
/* ── Buttons & inputs ───────────────────────────────────────────────────────── */
.search-row{display:flex;gap:8px;margin-bottom:14px;flex-wrap:wrap}
.search-row input,.search-row select{flex:1;background:var(--surface-2);border:1px solid var(--border);border-radius:var(--radius);color:var(--text);padding:8px 14px;font-size:.83rem;outline:none;transition:border-color var(--trans),box-shadow var(--trans);min-width:0}
.search-row input:focus{border-color:var(--accent);box-shadow:0 0 0 3px rgba(123,97,255,.15)}
.search-row input::placeholder{color:var(--text-3)}
.search-row select{flex:0 0 auto;cursor:pointer}
.btn{background:var(--accent-2);color:#fff;border:none;border-radius:var(--radius);padding:8px 18px;cursor:pointer;font-size:.82rem;font-weight:500;transition:filter var(--trans),box-shadow var(--trans);white-space:nowrap;flex-shrink:0;letter-spacing:.01em}
.btn:hover{filter:brightness(1.2);box-shadow:0 4px 12px rgba(87,66,212,.4)}.btn:disabled{opacity:.45;cursor:default}
.btn.secondary{background:var(--surface-2);color:var(--text-2);border:1px solid var(--border)}
.btn.secondary:hover{background:var(--border);color:var(--text);box-shadow:none}
.btn.danger{background:rgba(255,64,96,.15);color:var(--red);border:1px solid rgba(255,64,96,.3)}
.btn.danger:hover{background:rgba(255,64,96,.25);box-shadow:none}
/* ── Search / results ───────────────────────────────────────────────────────── */
.result-item{margin-bottom:10px;border-left:3px solid var(--border);padding-left:14px;padding-top:4px;padding-bottom:4px;transition:border-color var(--trans),background var(--trans);border-radius:0 var(--radius) var(--radius) 0}
.result-item:hover{border-left-color:var(--accent);background:rgba(123,97,255,.05)}
.result-item .path{font-size:.72rem;color:var(--text-3);margin-bottom:5px;display:flex;justify-content:space-between;font-family:'Cascadia Code','Fira Code',monospace}
.result-item .score{color:var(--green);font-size:.71rem;font-weight:600}
.result-item pre{max-height:100px;overflow:hidden}
/* ── Pre / Code ─────────────────────────────────────────────────────────────── */
pre{background:var(--surface-2);border:1px solid var(--border);border-radius:var(--radius);padding:12px 14px;font-size:.77rem;overflow:auto;max-height:400px;white-space:pre-wrap;color:var(--text-2);font-family:'Cascadia Code','Fira Code','JetBrains Mono',monospace;line-height:1.6}
code{background:var(--surface-2);padding:1px 6px;border-radius:3px;font-size:.84em;font-family:'Cascadia Code','Fira Code',monospace;color:var(--accent)}
/* ── Language bars ──────────────────────────────────────────────────────────── */
.lang-bar{display:flex;align-items:center;gap:8px;margin-bottom:6px;font-size:.8rem}
.lang-bar .name{width:90px;color:var(--text-2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.lang-bar .bar{flex:1;height:6px;background:var(--surface-2);border-radius:3px;overflow:hidden}
.lang-bar .fill{height:100%;background:var(--accent-2);border-radius:3px}
.lang-bar .count{color:var(--text-3);font-size:.73rem;min-width:44px;text-align:right}
/* ── Stat grid ──────────────────────────────────────────────────────────────── */
.stat-row{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:16px}
.stat-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(130px,1fr));gap:10px}
.stat-box{background:var(--surface-2);border:1px solid var(--border);border-radius:var(--radius);padding:12px 14px;text-align:center;transition:border-color var(--trans)}
.stat-box:hover{border-color:var(--border-2)}
.stat-box .val{font-size:1.3rem;font-weight:700;color:var(--text);line-height:1;letter-spacing:-.01em}
.stat-box .lbl{font-size:.69rem;color:var(--text-3);margin-top:5px;text-transform:uppercase;letter-spacing:.06em;font-weight:600}
/* ── Progress bar ───────────────────────────────────────────────────────────── */
.progress-bar{height:5px;background:var(--surface-2);border-radius:3px;overflow:hidden;margin-top:6px}
.progress-fill{height:100%;background:linear-gradient(90deg,var(--accent) 0%,var(--cyan) 100%);border-radius:3px;transition:width .6s ease;box-shadow:0 0 8px var(--accent)}
/* ── Activity ───────────────────────────────────────────────────────────────── */
.activity-list{display:flex;flex-direction:column;gap:4px}
.activity-item{display:flex;align-items:center;gap:10px;padding:7px 12px;background:var(--surface-2);border:1px solid var(--border);border-radius:var(--radius);font-size:.79rem;transition:border-color var(--trans)}
.activity-item:hover{border-color:var(--border-2)}
.activity-dot{width:8px;height:8px;border-radius:50%;background:var(--text-3);flex-shrink:0;box-shadow:0 0 4px currentColor}
.activity-dot.ok{background:var(--green);color:var(--green)}.activity-dot.error{background:var(--red);color:var(--red)}.activity-dot.scheduled{background:var(--accent);color:var(--accent)}
.activity-text{flex:1;color:var(--text-2)}.activity-time{color:var(--text-3);font-size:.72rem;white-space:nowrap}
/* ── Live feed ticker ───────────────────────────────────────────────────────── */
.live-feed{height:120px;overflow-y:auto;display:flex;flex-direction:column-reverse;gap:3px;padding:4px 0;scrollbar-width:thin;scrollbar-color:var(--border) transparent}
.live-feed-item{display:flex;align-items:center;gap:8px;padding:4px 8px;background:var(--surface-2);border-radius:4px;font-size:.76rem;animation:feedIn .25s ease-out;flex-shrink:0}
@keyframes feedIn{from{opacity:0;transform:translateY(-6px)}to{opacity:1;transform:translateY(0)}}
.live-dot{width:6px;height:6px;border-radius:50%;flex-shrink:0}
.live-dot.green{background:var(--green)}.live-dot.red{background:var(--red)}.live-dot.blue{background:var(--accent)}.live-dot.gray{background:var(--text-3)}
/* ── Wiki ───────────────────────────────────────────────────────────────────── */
.wiki-layout{display:grid;grid-template-columns:220px 1fr;gap:14px;height:calc(100vh - 290px);min-height:380px}
.wiki-sidebar{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius-lg);overflow-y:auto;padding:10px}
.wiki-list{list-style:none}
.wiki-list li a{display:block;padding:5px 8px;font-size:.79rem;color:var(--text-2);cursor:pointer;border-radius:var(--radius);transition:background var(--trans),color var(--trans)}
.wiki-list li a:hover{background:var(--surface-2);color:var(--text)}
.wiki-content{overflow-y:auto;background:var(--surface);border:1px solid var(--border);border-radius:var(--radius-lg);padding:16px;font-size:.83rem;line-height:1.7;color:var(--text-2)}
.wiki-content h1{font-size:1.2rem;color:var(--text);margin:0 0 12px;padding-bottom:8px;border-bottom:1px solid var(--border)}
.wiki-content h2{font-size:1rem;color:var(--text);margin:16px 0 8px}
.wiki-content h3{font-size:.88rem;color:var(--text-2);margin:12px 0 6px}
.wiki-content p{margin-bottom:10px}.wiki-content ul,.wiki-content ol{margin:8px 0 10px 20px}
/* ── Graph ──────────────────────────────────────────────────────────────────── */
.graph-layout{display:grid;grid-template-columns:1fr 1fr;gap:14px;height:500px}
.graph-panel{display:flex;flex-direction:column;gap:8px}
.graph-canvas-wrap{flex:1;position:relative;background:var(--surface-3);border:1px solid var(--border);border-radius:var(--radius-lg);overflow:hidden}
canvas{width:100%;height:100%;cursor:grab;display:block}
.graph-legend{position:absolute;top:8px;right:8px;background:rgba(13,17,23,.9);border:1px solid var(--border);border-radius:var(--radius);padding:8px;font-size:.69rem;max-height:180px;overflow-y:auto}
.legend-item{display:flex;align-items:center;gap:5px;margin-bottom:3px}
.legend-dot{width:7px;height:7px;border-radius:50%;flex-shrink:0}
.graph-tooltip{position:fixed;background:var(--surface-2);border:1px solid var(--border);border-radius:var(--radius);padding:6px 10px;font-size:.75rem;pointer-events:none;display:none;max-width:260px;z-index:1000;box-shadow:0 4px 12px rgba(0,0,0,.4)}
/* ── Verify ─────────────────────────────────────────────────────────────────── */
.verify-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:8px}
.verify-cat{padding:8px 10px;border-radius:var(--radius);border:1px solid var(--border);display:flex;align-items:center;gap:8px;font-size:.79rem;transition:border-color var(--trans)}
.verify-cat.pass{border-color:var(--green-bg);background:rgba(13,42,22,.3)}
.vc-icon{font-size:.88rem;flex-shrink:0}.vc-name{color:var(--text-2);flex:1;font-size:.78rem}.vc-count{font-size:.71rem;color:var(--text-3);white-space:nowrap}
/* ── Integrations ───────────────────────────────────────────────────────────── */
.integrations-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:10px}
.integ-card{background:var(--surface-2);border:1px solid var(--border);border-top:3px solid var(--border);border-radius:var(--radius-lg);padding:12px 14px;transition:border-color var(--trans)}
.integ-card:hover{border-color:var(--border-2)}
.integ-card.ok{border-color:rgba(0,194,142,.25);border-top-color:var(--green)}.integ-card.err{border-color:rgba(255,64,96,.25);border-top-color:var(--red)}
.integ-title{font-size:.84rem;font-weight:600;color:var(--text);margin-bottom:4px}
.integ-status{font-size:.78rem}.integ-status.ok{color:var(--green)}.integ-status.err{color:var(--red)}.integ-status.warn{color:var(--amber)}
/* ── Community cards ────────────────────────────────────────────────────────── */
.community-card{background:var(--surface);border:1px solid var(--border);border-left:3px solid transparent;border-radius:var(--radius-lg);padding:12px 14px;margin-bottom:8px;transition:border-color var(--trans),border-left-color var(--trans)}
.community-card:hover{border-color:var(--border-2);border-left-color:var(--accent)}
/* ── Pipeline events ────────────────────────────────────────────────────────── */
.event-item{display:flex;gap:10px;padding:5px 8px;border-radius:var(--radius);background:var(--surface-2);margin-bottom:5px;font-size:.77rem}
.event-status{font-weight:600;min-width:40px}
.event-status.ok{color:var(--green)}.event-status.error{color:var(--red)}
.event-info{color:var(--text-2);flex:1}.event-time{color:var(--text-3);font-size:.71rem;white-space:nowrap}
/* ── Tree ───────────────────────────────────────────────────────────────────── */
.tree{font-family:'Cascadia Code','Fira Code',monospace;font-size:.77rem;line-height:1.6;color:var(--text-2);background:var(--surface-2);border:1px solid var(--border);border-radius:var(--radius);padding:12px;max-height:60vh;overflow:auto}
/* ── Loader / skeleton ──────────────────────────────────────────────────────── */
.loader{color:var(--text-3);font-size:.8rem;padding:20px;text-align:center;display:flex;align-items:center;justify-content:center;gap:8px}
.loader::before{content:'';display:inline-block;width:14px;height:14px;border:2px solid var(--border-2);border-top-color:var(--accent);border-radius:50%;animation:spin .7s linear infinite;flex-shrink:0}
@keyframes spin{to{transform:rotate(360deg)}}
.skeleton{background:linear-gradient(90deg,var(--surface) 25%,var(--surface-2) 50%,var(--surface) 75%);background-size:200% 100%;animation:shimmer 1.5s infinite;border-radius:var(--radius);height:16px;margin-bottom:6px}
@keyframes shimmer{0%{background-position:200% 0}100%{background-position:-200% 0}}
/* ── Toast ──────────────────────────────────────────────────────────────────── */
#toast-container{position:fixed;bottom:20px;right:20px;z-index:9999;display:flex;flex-direction:column;gap:8px;pointer-events:none}
.toast{background:var(--surface-2);border:1px solid var(--border-2);border-radius:var(--radius-lg);padding:11px 18px;font-size:.81rem;min-width:220px;max-width:380px;box-shadow:0 8px 32px rgba(0,0,0,.5);animation:slideIn .2s cubic-bezier(.4,0,.2,1);pointer-events:auto;backdrop-filter:blur(8px)}
.toast.success{border-left:3px solid var(--green)}.toast.error{border-left:3px solid var(--red)}.toast.warn{border-left:3px solid var(--amber)}.toast.info{border-left:3px solid var(--accent)}
@keyframes slideIn{from{transform:translateX(20px);opacity:0}to{transform:translateX(0);opacity:1}}
/* ── More nav drawer ────────────────────────────────────────────────────────── */
.more-toggle{display:flex;align-items:center;gap:8px;width:100%;background:none;border:none;
  color:var(--text-3);padding:7px 14px;font-size:.78rem;cursor:pointer;border-radius:0;
  transition:background var(--trans),color var(--trans);text-align:left;white-space:nowrap;
  border-top:1px solid var(--border);margin-top:4px;justify-content:space-between}
.more-toggle:hover{background:var(--surface-2);color:var(--text)}
.more-toggle .more-label{display:flex;align-items:center;gap:8px;font-size:.78rem;letter-spacing:.02em}
.more-toggle .more-caret{font-size:.65rem;transition:transform 200ms ease;opacity:.6}
.more-toggle.open .more-caret{transform:rotate(180deg)}
.more-drawer{overflow:hidden;max-height:0;transition:max-height 300ms cubic-bezier(.4,0,.2,1)}
.more-drawer.open{max-height:600px}
.more-drawer .nav-btn{padding-left:24px;font-size:.78rem;color:var(--text-3)}
.more-drawer .nav-btn:hover{color:var(--text-2)}
.more-drawer .nav-group{padding-left:24px;font-size:.6rem}
/* ── Alert dot on nav ───────────────────────────────────────────────────────── */
.nav-alert-dot{width:7px;height:7px;border-radius:50%;background:var(--red);margin-left:auto;
  flex-shrink:0;box-shadow:0 0 5px var(--red);animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.5}}
/* ── Topbar alert badge ─────────────────────────────────────────────────────── */
#alert-badge{display:none;align-items:center;gap:5px;background:rgba(255,64,96,.12);
  border:1px solid rgba(255,64,96,.35);border-radius:99px;padding:3px 10px;font-size:.73rem;
  color:var(--red);cursor:pointer;transition:background var(--trans)}
#alert-badge:hover{background:rgba(255,64,96,.22)}
#alert-badge.visible{display:flex}
/* ── Responsive ─────────────────────────────────────────────────────────────── */
@media(max-width:768px){
  .sidebar{width:48px}.brand-name,.nav-group,.sb-project,.more-toggle .more-label span:last-child{display:none}
  .two-col,.graph-layout,.wiki-layout{grid-template-columns:1fr;height:auto}
  .kpi-row{grid-template-columns:repeat(2,1fr)}
}
</style>
</head>
<body>
<div class="app">

<!-- ── SIDEBAR ─────────────────────────────────────────────────────────────── -->
<aside id="sidebar" class="sidebar">
  <div class="sb-header">
    <span class="brand"><span class="brand-icon">🔍</span><span class="brand-name">opencode-search</span></span>
    <button id="sidebar-toggle" onclick="toggleSidebar()" title="Toggle sidebar">‹</button>
  </div>
  <div class="sb-project">
    <select id="project-select" onchange="switchProject(this.value)"><option value="">Loading projects…</option></select>
  </div>
  <nav class="sb-nav">
    <!-- Primary: always visible (11 items) -->
    <button class="nav-btn active" id="nav-overview" onclick="showPage('overview')"><span class="nav-icon">⬡</span><span class="nav-label">Overview</span></button>

    <div class="nav-group">Explore</div>
    <button class="nav-btn" id="nav-search"  onclick="showPage('search')"><span class="nav-icon">⚡</span><span class="nav-label">Search</span></button>
    <button class="nav-btn" id="nav-ask"     onclick="showPage('ask')"><span class="nav-icon">💬</span><span class="nav-label">Ask</span></button>
    <button class="nav-btn" id="nav-feature" onclick="showPage('feature')"><span class="nav-icon">🧩</span><span class="nav-label">Feature</span></button>
    <button class="nav-btn" id="nav-graph"   onclick="showPage('graph')"><span class="nav-icon">🕸</span><span class="nav-label">Graph</span></button>

    <div class="nav-group">Intelligence</div>
    <button class="nav-btn" id="nav-arch-map"     onclick="showPage('arch-map')"><span class="nav-icon">🏛</span><span class="nav-label">Architecture</span></button>
    <button class="nav-btn" id="nav-communities"  onclick="showPage('communities')"><span class="nav-icon">🏘</span><span class="nav-label">Communities</span></button>
    <button class="nav-btn" id="nav-service-mesh" onclick="showPage('service-mesh')"><span class="nav-icon">🕷</span><span class="nav-label">Service Mesh</span></button>
    <button class="nav-btn" id="nav-impact"       onclick="showPage('impact')"><span class="nav-icon">💥</span><span class="nav-label">Impact</span></button>

    <div class="nav-group">Monitor</div>
    <button class="nav-btn" id="nav-health"  onclick="showPage('health')" title="Health &amp; Alerts"><span class="nav-icon">💓</span><span class="nav-label">Health</span><span id="nav-health-alert" class="nav-alert-dot" style="display:none"></span></button>
    <button class="nav-btn" id="nav-wiki"    onclick="showPage('wiki')"><span class="nav-icon">📖</span><span class="nav-label">Wiki</span></button>
    <button class="nav-btn" id="nav-patterns" onclick="showPage('patterns')"><span class="nav-icon">🎯</span><span class="nav-label">Patterns</span></button>

    <!-- More drawer: secondary tools (collapsed by default) -->
    <button class="more-toggle" id="more-toggle" onclick="toggleMoreNav()">
      <span class="more-label"><span class="nav-icon" style="font-size:.8rem">⋯</span><span class="nav-label">More tools</span></span>
      <span class="more-caret">▼</span>
    </button>
    <div class="more-drawer" id="more-drawer">
      <div class="nav-group">Knowledge</div>
      <button class="nav-btn" id="nav-structure"     onclick="showPage('structure')"><span class="nav-icon">📁</span><span class="nav-label">Structure</span></button>
      <button class="nav-btn" id="nav-saved-queries" onclick="showPage('saved-queries')"><span class="nav-icon">🔖</span><span class="nav-label">Saved Queries</span></button>
      <div class="nav-group">Analysis</div>
      <button class="nav-btn" id="nav-trace"        onclick="showPage('trace')"><span class="nav-icon">🔎</span><span class="nav-label">Trace</span></button>
      <button class="nav-btn" id="nav-import-cycles" onclick="showPage('import-cycles')"><span class="nav-icon">🔄</span><span class="nav-label">Import Cycles</span></button>
      <button class="nav-btn" id="nav-callflow"     onclick="showPage('callflow')"><span class="nav-icon">📊</span><span class="nav-label">Callflow</span></button>
      <button class="nav-btn" id="nav-fed-map"      onclick="showPage('fed-map')"><span class="nav-icon">🗺</span><span class="nav-label">Fed Map</span></button>
      <button class="nav-btn" id="nav-pr-impact"    onclick="showPage('pr-impact')"><span class="nav-icon">🎯</span><span class="nav-label">PR Impact</span></button>
      <div class="nav-group">Admin</div>
      <button class="nav-btn" id="nav-projects"     onclick="showPage('projects')"><span class="nav-icon">📋</span><span class="nav-label">Projects</span></button>
      <button class="nav-btn" id="nav-integrations" onclick="showPage('integrations')"><span class="nav-icon">🔌</span><span class="nav-label">Integrations</span></button>
      <button class="nav-btn" id="nav-jobs"         onclick="showPage('jobs')"><span class="nav-icon">⚙</span><span class="nav-label">Jobs</span></button>
      <div class="nav-group">Quality</div>
      <button class="nav-btn" id="nav-verify"  onclick="showPage('verify')"><span class="nav-icon">✅</span><span class="nav-label">Verify</span></button>
      <button class="nav-btn" id="nav-release" onclick="showPage('release')"><span class="nav-icon">🚀</span><span class="nav-label">Release</span></button>
      <button class="nav-btn" id="nav-qa"      onclick="showPage('qa')"><span class="nav-icon">🔬</span><span class="nav-label">QA Gate</span></button>
      <button class="nav-btn" id="nav-sysstat" onclick="showPage('sysstat')"><span class="nav-icon">📊</span><span class="nav-label">Coverage</span></button>
      <div class="nav-group">Maintenance</div>
      <button class="nav-btn" id="nav-dedup"     onclick="showPage('dedup')"><span class="nav-icon">🧹</span><span class="nav-label">Dedup</span></button>
      <button class="nav-btn" id="nav-file-tree" onclick="showPage('file-tree')"><span class="nav-icon">🌲</span><span class="nav-label">File Tree</span></button>
      <button class="nav-btn" id="nav-vacuum"    onclick="showPage('vacuum')"><span class="nav-icon">💾</span><span class="nav-label">Vacuum</span></button>
    </div>
  </nav>
</aside>

<!-- ── MAIN WRAPPER ─────────────────────────────────────────────────────────── -->
<div class="main-wrap">

  <!-- TOPBAR -->
  <header class="topbar">
    <button class="menu-btn" onclick="toggleSidebar()" title="Toggle sidebar">☰</button>
    <div class="top-search">
      <input id="global-q" type="text" placeholder="Quick search…" onkeydown="if(event.key==='Enter')quickSearch()"/>
      <select id="global-scope"><option value="code">Code</option><option value="docs">Docs</option><option value="all">All</option></select>
      <button class="btn" onclick="quickSearch()">Search</button>
    </div>
    <div class="top-right">
      <div id="alert-badge" onclick="showPage('health')" title="Active alert violations">
        <span>⚠</span><span id="alert-badge-count">0</span><span style="font-size:.68rem">alerts</span>
      </div>
      <span id="daemon-dot" class="daemon-dot" title="Daemon status">●</span>
      <span id="daemon-status" style="font-size:.8rem;color:var(--text-3)">connecting…</span>
      <button class="icon-btn" id="theme-btn" onclick="toggleTheme()" title="Toggle theme">🌙</button>
    </div>
  </header>

  <!-- METRIC STRIP -->
  <div class="metric-strip" id="metric-strip">
    <span style="font-size:.73rem;color:var(--text-3)">Loading…</span>
  </div>

  <!-- PAGES -->
  <main class="content">

  <!-- PAGE: OVERVIEW -->
  <div id="page-overview" class="page active">
    <div class="page-title">Overview <span id="overview-last-updated" style="font-size:.72rem;color:var(--text-3);font-weight:400;margin-left:8px"></span></div>
    <div id="overview-kpi" class="kpi-row"></div>
    <div class="two-col">
      <div>
        <div class="card">
          <div class="card-header"><span class="card-title">System Health</span><span id="health-badge" class="badge none">—</span></div>
          <div id="overview-health"><div class="loader">Loading…</div></div>
        </div>
        <div class="card">
          <div class="card-header"><span class="card-title">System Load</span><span id="load-badge" class="badge none">—</span></div>
          <div id="overview-load"><div class="loader">Loading…</div></div>
        </div>
        <div class="card">
          <div class="card-header"><span class="card-title">KB Completeness</span></div>
          <div id="overview-kb"><div class="loader">Loading…</div></div>
        </div>
      </div>
      <div>
        <div class="card">
          <div class="card-header">
            <span class="card-title">Recent Pipeline Events</span>
            <button class="btn secondary" style="font-size:.75rem;padding:3px 8px" onclick="loadOverview()">↺</button>
          </div>
          <div id="overview-events" class="activity-list"><div class="loader">Loading…</div></div>
        </div>
        <div class="card">
          <div class="card-header"><span class="card-title">Active Alerts</span></div>
          <div id="overview-alerts"><div class="loader">Loading…</div></div>
        </div>
      </div>
    </div>
    <div class="card" style="margin-top:14px">
      <div class="card-header">
        <span class="card-title">Live Event Feed</span>
        <span id="live-feed-badge" class="badge info" style="font-size:.67rem">SSE</span>
      </div>
      <div id="live-feed" class="live-feed"><div style="font-size:.76rem;color:var(--text-3);padding:4px">Waiting for events…</div></div>
    </div>
  </div>

  <!-- PAGE: SEARCH -->
  <div id="page-search" class="page">
    <div class="page-title">Code Search</div>
    <div class="card">
      <div class="search-row">
        <input id="search-q" placeholder="Search code, functions, patterns…" onkeydown="if(event.key==='Enter')runSearch()"/>
        <select id="search-scope"><option value="code">Code</option><option value="docs">Docs</option><option value="all">All</option></select>
        <button class="btn" onclick="runSearch()">Search</button>
      </div>
      <div id="search-results"></div>
    </div>
  </div>

  <!-- PAGE: ASK -->
  <div id="page-ask" class="page">
    <div class="page-title">Ask</div>
    <div class="card">
      <div class="search-row">
        <input id="ask-q" placeholder="How does X work? What calls Y? Which layer handles Z?" onkeydown="if(event.key==='Enter')runAsk()"/>
        <select id="ask-scope"><option value="all">All</option><option value="wiki">Wiki</option><option value="architecture">Architecture</option></select>
        <button class="btn" onclick="runAsk()">Ask</button>
      </div>
      <div id="ask-results"></div>
    </div>
    <div class="card">
      <div class="card-title" style="margin-bottom:10px">Suggested questions</div>
      <div id="ask-suggestions" style="display:flex;flex-wrap:wrap;gap:6px"></div>
    </div>
  </div>

  <!-- PAGE: GRAPH -->
  <div id="page-graph" class="page">
    <div class="page-title">Call Graph</div>
    <div class="card">
      <div class="card-title" style="margin-bottom:10px">Symbol Lookup</div>
      <div class="search-row">
        <input id="graph-symbol" placeholder="Symbol name (e.g. http.Run)"/>
        <select id="graph-relation">
          <option value="definition">definition</option>
          <option value="callers">callers</option>
          <option value="callees">callees</option>
          <option value="impact">impact</option>
          <option value="path">path →</option>
        </select>
        <input id="graph-to" placeholder="to_symbol (path only)" style="max-width:200px"/>
        <button class="btn" onclick="runGraph()">Run</button>
      </div>
      <div class="graph-layout">
        <div class="graph-panel">
          <pre id="graph-result" style="height:100%;overflow:auto;margin:0">Enter a symbol above…</pre>
        </div>
        <div class="graph-panel">
          <div class="graph-canvas-wrap" id="sigma-wrap" style="position:relative">
            <div id="sigma-container" style="width:100%;height:100%"></div>
            <div id="graph-legend" class="graph-legend"></div>
            <div id="sigma-tooltip" style="position:fixed;display:none;background:rgba(13,17,23,.95);border:1px solid var(--border);border-radius:6px;padding:8px 12px;font-size:.76rem;pointer-events:none;z-index:9999;max-width:260px"></div>
          </div>
          <div id="graph-canvas-info" style="font-size:.71rem;color:var(--text-3);padding:3px 0"></div>
          <div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center">
            <button class="btn secondary" style="font-size:.77rem;padding:5px 12px" onclick="visualizeFullGraph(500)">Visualize 500</button>
            <button class="btn secondary" style="font-size:.77rem;padding:5px 12px" onclick="visualizeFullGraph(2000)">Visualize 2K</button>
            <button class="btn secondary" style="font-size:.77rem;padding:5px 12px" onclick="visualizeFullGraph(5000)">Visualize 5K</button>
            <button class="btn danger" style="font-size:.77rem;padding:5px 12px" onclick="stopGraph()">Stop</button>
            <select id="graph-layout" onchange="switchGraphLayout()" style="font-size:.75rem;padding:2px 6px;background:var(--bg-1);color:var(--text-1);border:1px solid var(--border);border-radius:4px">
              <option value="fa2">ForceAtlas2</option>
              <option value="circular">Circular</option>
            </select>
          </div>
        </div>
      </div>
    </div>
    <div class="card">
      <div class="card-title" style="margin-bottom:8px">Export Knowledge Graph</div>
      <p style="font-size:.79rem;color:var(--text-3);margin-bottom:10px">Export for Gephi, Cytoscape, NetworkX — up to 5,000 nodes</p>
      <div style="display:flex;gap:8px">
        <button class="btn" style="font-size:.81rem" onclick="exportGraph('json')">⬇ JSON</button>
        <button class="btn secondary" style="font-size:.81rem" onclick="exportGraph('graphml')">⬇ GraphML</button>
      </div>
      <div id="graph-export-info" style="margin-top:8px;font-size:.74rem;color:var(--text-3)"></div>
    </div>
    <div id="graph-canvas-tooltip" class="graph-tooltip"></div>
  </div>

  <!-- PAGE: STRUCTURE -->
  <div id="page-structure" class="page">
    <div class="page-title">Project Structure</div>
    <div class="two-col">
      <div>
        <div class="card"><div class="card-title" style="margin-bottom:8px">Directory Tree</div><pre id="structure-tree" class="tree">Select a project…</pre></div>
      </div>
      <div>
        <div class="card"><div class="card-title" style="margin-bottom:8px">Language Breakdown</div><div id="lang-breakdown"></div></div>
        <div class="card"><div class="card-title" style="margin-bottom:8px">Graph Stats</div><div id="graph-stats" class="stat-grid"></div></div>
      </div>
    </div>
  </div>

  <!-- PAGE: PATTERNS -->
  <div id="page-patterns" class="page">
    <div class="page-title">Patterns &amp; Architecture</div>
    <div class="card">
      <div class="card-title" style="margin-bottom:10px">Architecture &amp; Module Structure</div>
      <div id="patterns-arch" class="stat-grid" style="margin-bottom:12px"></div>
      <div id="patterns-frameworks"></div>
    </div>
    <div class="two-col">
      <div class="card"><div class="card-title" style="margin-bottom:8px">Languages</div><div id="patterns-langs"></div></div>
      <div class="card"><div class="card-title" style="margin-bottom:8px">Code Conventions</div><div id="patterns-conventions" class="stat-grid"></div></div>
    </div>
    <div class="card">
      <div class="card-title" style="margin-bottom:8px">LLM Deep Analysis</div>
      <div id="patterns-llm-meta" style="margin-bottom:10px;font-size:.77rem;color:var(--text-3)">No LLM analysis cached.</div>
      <div id="patterns-llm-result"></div>
      <div style="margin-top:12px;display:flex;gap:8px">
        <button class="btn" style="font-size:.81rem" onclick="runLLMAnalysis(false)">Analyse with LLM</button>
        <button class="btn secondary" style="font-size:.81rem" onclick="runLLMAnalysis(true)">Force Re-analyse</button>
      </div>
    </div>
    <div class="card">
      <div class="card-title" style="margin-bottom:8px">Dependencies &amp; Versions</div>
      <div id="patterns-dep-meta" style="margin-bottom:10px;font-size:.77rem;color:var(--text-3)"></div>
      <div id="patterns-deps" style="max-height:400px;overflow-y:auto"><div class="loader">Loading…</div></div>
    </div>
  </div>

  <!-- PAGE: WIKI -->
  <div id="page-wiki" class="page">
    <div class="page-title">Wiki / KB</div>
    <div class="card">
      <div class="search-row">
        <input id="wiki-search-q" placeholder="Ask an architectural question…" onkeydown="if(event.key==='Enter')runWikiSearch()"/>
        <select id="wiki-scope"><option value="all">All</option><option value="wiki">Wiki only</option><option value="architecture">Architecture only</option></select>
        <button class="btn" onclick="runWikiSearch()">Ask</button>
      </div>
      <div id="wiki-search-results"></div>
    </div>
    <div class="wiki-layout">
      <div class="wiki-sidebar">
        <div style="font-size:.71rem;color:var(--text-3);padding:2px 0 8px;font-weight:600;text-transform:uppercase;letter-spacing:.07em">Pages</div>
        <ul id="wiki-page-list" class="wiki-list"><li style="color:var(--text-3);font-size:.81rem">Loading…</li></ul>
      </div>
      <div class="wiki-content" id="wiki-content">Click a page to view it.</div>
    </div>
  </div>

  <!-- PAGE: COMMUNITIES -->
  <div id="page-communities" class="page">
    <div class="page-title">Architecture &amp; Communities</div>
    <div class="card" id="arch-synthesis-card">
      <div class="card-title" style="margin-bottom:8px">Project Architecture Synthesis</div>
      <div id="arch-synthesis-content"><div class="loader">Loading synthesis…</div></div>
    </div>
    <div class="card">
      <div class="card-title" style="margin-bottom:8px">Knowledge Semantics — Top Communities</div>
      <div id="enrichment-progress" style="margin-bottom:14px"></div>
      <div id="communities-list"></div>
    </div>
    <div class="card" id="god-nodes-panel" style="display:none">
      <div class="card-title" style="margin-bottom:8px">God Nodes — Top Hub Symbols</div>
      <p style="font-size:.79rem;color:var(--text-3);margin-bottom:10px">Symbols with the most connections (in + out degree). These are architectural pivot points — changing them has the widest blast radius.</p>
      <div id="god-nodes-list"></div>
    </div>
    <div class="card" id="bridges-panel" style="display:none">
      <div class="card-title" style="margin-bottom:8px">Surprising Cross-Community Connections</div>
      <p style="font-size:.79rem;color:var(--text-3);margin-bottom:10px">High-confidence edges that span different architectural communities — potential hidden coupling between domains.</p>
      <div id="bridges-list"></div>
    </div>
  </div>

  <!-- PAGE: HEALTH -->
  <div id="page-health" class="page">
    <div class="page-title">Health &amp; Monitoring</div>
    <div class="card"><div class="card-title" style="margin-bottom:8px">Daemon Status</div><div id="daemon-metrics" class="stat-grid"></div></div>
    <div class="card">
      <div style="display:flex;align-items:center;gap:10px;margin-bottom:10px;flex-wrap:wrap">
        <div class="card-title" style="flex:1">Knowledge Base Health</div>
        <button class="btn secondary" id="enrich-hier-btn" onclick="triggerEnrichHierarchy()" style="font-size:.74rem;padding:4px 12px">⚡ Re-enrich Hierarchy</button>
      </div>
      <div id="kb-health-grid" class="stat-grid" style="margin-bottom:14px"></div>
      <div id="kb-enrich-level-detail" style="margin-bottom:10px"></div>
      <div id="kb-enrich-job-status" style="font-size:.76rem;color:var(--text-3);display:none"></div>
      <div id="kb-health-detail" style="font-size:.77rem;color:var(--text-3)"></div>
    </div>
    <div class="card">
      <div class="card-title" style="margin-bottom:8px">Auto-Pipeline Events</div>
      <div id="pipeline-events-meta" style="font-size:.74rem;color:var(--text-3);margin-bottom:8px"></div>
      <div id="pipeline-events-list" style="font-size:.74rem"></div>
    </div>
    <div class="card">
      <div style="display:flex;align-items:center;gap:12px;margin-bottom:10px;flex-wrap:wrap">
        <div class="card-title">Search Metrics</div>
        <select id="metrics-hours" onchange="loadMetricsCharts()" style="font-size:.76rem;padding:2px 6px;background:var(--bg-1);color:var(--text-1);border:1px solid var(--border);border-radius:4px">
          <option value="1">Last 1h</option>
          <option value="6">Last 6h</option>
          <option value="24" selected>Last 24h</option>
          <option value="168">Last 7d</option>
        </select>
        <button class="btn secondary" onclick="loadMetricsCharts()" style="font-size:.74rem;padding:3px 10px">↺</button>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px">
        <div><div style="font-size:.74rem;color:var(--text-3);margin-bottom:4px">Latency (ms)</div><canvas id="chart-latency" height="120"></canvas></div>
        <div><div style="font-size:.74rem;color:var(--text-3);margin-bottom:4px">Zero-result Rate (%)</div><canvas id="chart-zeroresult" height="120"></canvas></div>
      </div>
      <div style="margin-top:12px;font-size:.74rem;color:var(--text-3)">Current snapshot: <span id="metrics-snapshot">—</span></div>
    </div>
    <div class="card" id="alerts-card">
      <div style="display:flex;align-items:center;gap:12px;margin-bottom:10px">
        <div class="card-title">Alert Rules</div>
        <button class="btn secondary" onclick="loadAlerts()" style="font-size:.74rem;padding:3px 10px">↺</button>
      </div>
      <div id="alerts-violations"></div>
      <div id="alerts-rules-list" style="margin-top:8px;font-size:.77rem"></div>
    </div>
  </div>

  <!-- PAGE: VERIFY -->
  <div id="page-verify" class="page">
    <div class="page-title">Verification</div>
    <div class="card">
      <div style="display:flex;align-items:center;gap:16px;margin-bottom:18px;flex-wrap:wrap">
        <div id="verify-badge" style="font-size:1.8rem;font-weight:700;min-width:80px">—</div>
        <div style="flex:1;min-width:180px">
          <div id="verify-meta" style="font-size:.81rem;color:var(--text-2)">No verification data yet</div>
          <div id="verify-job-status" style="font-size:.77rem;color:var(--text-3);margin-top:2px"></div>
        </div>
        <div style="display:flex;gap:8px;flex-wrap:wrap">
          <button class="btn" id="verify-run-btn" onclick="runVerification()">▶ Run Verification</button>
          <button class="btn secondary" id="verify-fix-btn" onclick="triggerAutoFix()">🔧 Auto-Fix</button>
        </div>
      </div>
      <div class="card-title" style="margin-bottom:8px">Category Results</div>
      <div id="verify-category-grid" class="verify-grid"><div class="loader">Loading…</div></div>
      <div class="card-title" style="margin:16px 0 6px">History (last runs)</div>
      <div id="verify-sparkline" style="height:52px"></div>
    </div>
    <div class="card" id="verify-failures-card" style="display:none">
      <div class="card-title" style="margin-bottom:8px">Failures</div>
      <div id="verify-failures"></div>
    </div>
  </div>

  <!-- PAGE: COVERAGE STATUS -->
  <div id="page-sysstat" class="page">
    <div class="page-title">Coverage Status</div>
    <div class="card" style="margin-bottom:14px">
      <div style="display:flex;align-items:center;gap:12px;margin-bottom:12px;flex-wrap:wrap">
        <div class="card-title">System Check</div>
        <button class="btn" onclick="runSysstat()" id="sysstat-run-btn">▶ Run Full Check</button>
        <span id="sysstat-spinner" style="display:none;color:var(--text-3);font-size:.81rem">Running… (10-30s)</span>
        <span id="sysstat-ts" style="font-size:.74rem;color:var(--text-3);margin-left:auto"></span>
      </div>
      <div id="sysstat-summary" style="font-size:.83rem;color:var(--text-2);margin-bottom:10px">Loading…</div>
      <div id="sysstat-categories" style="display:flex;flex-direction:column;gap:8px"></div>
    </div>
    <div class="card" style="margin-bottom:14px">
      <div class="card-title" style="margin-bottom:10px">Feature Coverage Matrix</div>
      <div id="sysstat-coverage" style="overflow-x:auto"></div>
    </div>
    <div class="card">
      <div class="card-title" style="margin-bottom:10px">Dashboard Completeness</div>
      <div id="sysstat-dashboard-checklist" style="font-size:.81rem;line-height:1.9"></div>
    </div>
  </div>

  <!-- PAGE: RELEASE -->
  <div id="page-release" class="page">
    <div class="page-title">Pre-Release Readiness</div>
    <div class="card" style="border-color:#1f2d3d">
      <div id="release-verdict" style="margin-bottom:14px"></div>
      <div style="display:flex;gap:8px;margin-bottom:14px;align-items:center">
        <button class="btn" onclick="runPrerelease()">▶ Run Pre-Release Check</button>
        <span id="release-spinner" style="display:none;color:var(--text-3);font-size:.81rem">Running… (1-3 min)</span>
      </div>
      <div id="release-stages"></div>
    </div>
    <div class="card" id="release-screenshots-card" style="display:none">
      <div class="card-title" style="margin-bottom:8px">Screenshots</div>
      <div id="release-screenshots" style="display:flex;flex-wrap:wrap;gap:8px"></div>
    </div>
    <div class="card" id="release-anomalies-card" style="display:none">
      <div class="card-title" style="margin-bottom:8px">Anomalies</div>
      <div id="release-anomalies" style="font-size:.8rem"></div>
    </div>
  </div>

  <!-- PAGE: QA GATE -->
  <div id="page-qa" class="page">
    <div class="page-title">MVP Quality Gate</div>
    <div class="card" style="border-color:#1f2d3d">
      <div id="qa-verdict" style="margin-bottom:14px"></div>
      <div style="display:flex;gap:8px;margin-bottom:14px;align-items:center">
        <button class="btn" onclick="runQaGate()">▶ Run Full QA Gate</button>
        <span id="qa-spinner" style="display:none;color:var(--text-3);font-size:.81rem">Running… (5-8 min)</span>
      </div>
      <div id="qa-pillars"></div>
    </div>
    <div class="card" id="qa-failures-card" style="display:none">
      <div class="card-title" style="margin-bottom:8px">Failures &amp; Warnings</div>
      <div id="qa-failures" style="font-size:.8rem"></div>
    </div>
  </div>

  <!-- PAGE: PROJECTS -->
  <div id="page-projects" class="page">
    <div class="page-title">Indexed Projects</div>
    <div class="card"><div id="projects-table"><div class="loader">Loading…</div></div></div>
  </div>

  <!-- PAGE: INTEGRATIONS -->
  <div id="page-integrations" class="page">
    <div class="page-title">Integrations</div>
    <div class="card">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px">
        <div id="integrations-meta" style="font-size:.81rem;color:var(--text-2)"></div>
        <button class="btn secondary" style="font-size:.81rem" onclick="loadIntegrations()">↺ Refresh</button>
      </div>
      <div id="integrations-cards" class="integrations-grid"><div class="loader">Loading…</div></div>
    </div>
  </div>

  <!-- PAGE: ARCHITECTURE MAP -->
  <div id="page-arch-map" class="page">
    <div class="page-title">Architecture Map</div>
    <div class="card">
      <div class="card-header">
        <span class="card-title">Community Hierarchy</span>
        <span id="arch-map-levels" style="font-size:.78rem;color:var(--text-3)"></span>
      </div>
      <p style="font-size:.79rem;color:var(--text-3);margin-bottom:12px">Top-level architecture domains from the recursive Leiden hierarchy. Run <code>build(action='hierarchy')</code> then <code>build(action='enrich_hierarchy')</code> to populate.</p>
      <div style="display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap">
        <button class="btn" style="font-size:.81rem" onclick="buildHierarchy()">Build Hierarchy</button>
        <button class="btn secondary" style="font-size:.81rem" onclick="loadArchMap()">↺ Refresh</button>
        <select id="arch-map-level" onchange="loadArchMap()" style="background:var(--bg);border:1px solid var(--border);border-radius:var(--radius);color:var(--text);padding:5px 8px;font-size:.8rem;cursor:pointer">
          <option value="top">Top Level (Domains)</option>
          <option value="all">All Levels</option>
        </select>
      </div>
      <div id="arch-map-content"><div class="loader">Loading hierarchy…</div></div>
    </div>
  </div>

  <!-- PAGE: SERVICE MESH -->
  <div id="page-service-mesh" class="page">
    <div class="page-title">Service Mesh</div>
    <div class="card">
      <div class="card-header">
        <span class="card-title">Inter-Service Communication</span>
        <div style="display:flex;gap:6px;align-items:center">
          <select id="mesh-view" onchange="renderMeshView()" style="font-size:.76rem;padding:2px 6px;background:var(--bg-1);color:var(--text-1);border:1px solid var(--border);border-radius:4px">
            <option value="graph">Graph</option>
            <option value="list">List</option>
          </select>
          <button class="btn secondary" style="font-size:.81rem" onclick="loadServiceMesh()">↺ Scan</button>
        </div>
      </div>
      <p style="font-size:.79rem;color:var(--text-3);margin-bottom:12px">Detected gRPC, HTTP, message queue, and database connections across federation members.</p>
      <div id="service-mesh-description" style="font-size:.82rem;color:var(--text-2);margin-bottom:12px;padding:8px;background:var(--surface-2);border-radius:var(--radius);display:none"></div>
      <div id="mesh-graph-wrap" style="display:none;height:420px;position:relative;background:var(--surface-3);border:1px solid var(--border);border-radius:var(--radius-lg);overflow:hidden">
        <div id="mesh-sigma-container" style="width:100%;height:100%"></div>
        <div id="mesh-canvas-info" style="position:absolute;bottom:6px;left:8px;font-size:.7rem;color:var(--text-3);z-index:10;pointer-events:none"></div>
      </div>
      <div id="service-mesh-content"><div class="loader">Loading service mesh…</div></div>
    </div>
  </div>

  <!-- PAGE: FEDERATION MAP -->
  <div id="page-fed-map" class="page">
    <div class="page-title">Federation Map</div>
    <div class="card">
      <div class="card-header">
        <span class="card-title">Repository Federation</span>
        <button class="btn secondary" style="font-size:.81rem" onclick="loadFedMap()">↺ Refresh</button>
      </div>
      <p style="font-size:.79rem;color:var(--text-3);margin-bottom:12px">Sub-repositories in the federation. Click a member to see its structure.</p>
      <div id="fed-map-wrap" style="height:380px;position:relative;background:var(--surface-3);border:1px solid var(--border);border-radius:var(--radius-lg);overflow:hidden;margin-bottom:12px">
        <div id="fed-sigma-container" style="width:100%;height:100%"></div>
        <div id="fed-canvas-info" style="position:absolute;bottom:6px;left:8px;font-size:.7rem;color:var(--text-3);z-index:10;pointer-events:none">Click Refresh to load federation</div>
      </div>
      <div id="fed-members-list" style="font-size:.81rem"></div>
    </div>
  </div>

  <!-- PAGE: IMPACT ANALYSIS -->
  <div id="page-impact" class="page">
    <div class="page-title">Impact Analysis</div>
    <div class="card">
      <div class="card-title" style="margin-bottom:10px">Analyze Change Impact</div>
      <div class="search-row">
        <input id="impact-symbol" placeholder="Symbol name (e.g. ProcessOrder, http.HandleFunc)" onkeydown="if(event.key==='Enter')runImpactAnalysis()"/>
        <button class="btn" onclick="runImpactAnalysis()">Analyze</button>
      </div>
      <div id="impact-result"><div class="loader">Loading top impactful symbols…</div></div>
    </div>
  </div>

  <!-- PAGE: SEMANTIC TRACE -->
  <div id="page-trace" class="page">
    <div class="page-title">Semantic Trace</div>
    <div class="card">
      <div class="card-title" style="margin-bottom:10px">Trace a Code Flow</div>
      <p style="font-size:.79rem;color:var(--text-3);margin-bottom:12px">Describe the entry and exit points in natural language to trace a call path.</p>
      <div class="search-row" style="margin-bottom:8px">
        <input id="trace-from" placeholder="Entry point (e.g. HTTP request handler, auth middleware)"/>
      </div>
      <div class="search-row">
        <input id="trace-to" placeholder="Exit point (e.g. database write, kafka publish)"/>
        <button class="btn" onclick="runSemanticTrace()">Trace</button>
      </div>
      <div id="trace-result"><div style="color:var(--text-3);font-size:.82rem">Describe the start and end of a code flow to trace it.</div></div>
    </div>
  </div>

  <!-- PAGE: FEATURE TRACE -->
  <div id="page-feature" class="page">
    <div class="page-title">Feature Trace</div>
    <div class="card">
      <div class="card-title" style="margin-bottom:6px">Understand a Feature or Functionality</div>
      <p style="font-size:.79rem;color:var(--text-3);margin-bottom:12px">Ask <em>how</em> a feature works and <em>why</em> it was built that way. Returns entry points, call chain, algorithm overview, and design rationale.</p>
      <div class="search-row">
        <input id="feature-q" placeholder="e.g. How does cart checkout work? Why does auth use JWT?" onkeydown="if(event.key==='Enter')runFeatureTrace()"/>
        <button class="btn" onclick="runFeatureTrace()">Trace</button>
      </div>
      <div id="feature-result"><div style="color:var(--text-3);font-size:.82rem">Ask a feature question to see entry points, call chain, algorithm, and design rationale.</div></div>
    </div>
    <div class="card" id="feature-suggestions-card">
      <div class="card-title" style="margin-bottom:8px">Example questions</div>
      <div id="feature-suggestions" style="display:flex;flex-wrap:wrap;gap:6px">
        <button class="btn secondary" style="font-size:.77rem;padding:4px 10px" onclick="$('feature-q').value='How does user authentication work?';runFeatureTrace()">How does user authentication work?</button>
        <button class="btn secondary" style="font-size:.77rem;padding:4px 10px" onclick="$('feature-q').value='How does the payment flow work?';runFeatureTrace()">How does the payment flow work?</button>
        <button class="btn secondary" style="font-size:.77rem;padding:4px 10px" onclick="$('feature-q').value='How is data indexed and searched?';runFeatureTrace()">How is data indexed and searched?</button>
        <button class="btn secondary" style="font-size:.77rem;padding:4px 10px" onclick="$('feature-q').value='Why is the caching layer designed this way?';runFeatureTrace()">Why is the caching layer designed this way?</button>
      </div>
    </div>
  </div>

  <!-- PAGE: IMPORT CYCLES -->
  <div id="page-import-cycles" class="page">
    <div class="page-title">Import Cycles</div>
    <div class="card">
      <div class="card-header">
        <span class="card-title">Circular Import Dependencies</span>
        <button class="btn secondary" style="font-size:.81rem" onclick="loadImportCycles()">↺ Refresh</button>
      </div>
      <p style="font-size:.79rem;color:var(--text-3);margin-bottom:12px">Circular import chains detected by Tarjan SCC on the file-level import graph. High-severity cycles have length ≤ 3.</p>
      <div id="import-cycles-result"><div class="loader">Scanning for circular imports…</div></div>
    </div>
  </div>

  <!-- PAGE: CALLFLOW -->
  <div id="page-callflow" class="page">
    <div class="page-title">Callflow</div>
    <div class="card">
      <div class="card-title" style="margin-bottom:10px">Visualize Call Chain</div>
      <p style="font-size:.79rem;color:var(--text-3);margin-bottom:12px">Render a symbol's call chain as an interactive Mermaid diagram.</p>
      <div class="search-row" style="margin-bottom:8px">
        <input id="callflow-symbol" placeholder="Symbol name (e.g. ProcessOrder, handleAuth)" onkeydown="if(event.key==='Enter')runCallflow()"/>
        <select id="callflow-direction" style="flex-shrink:0;width:120px">
          <option value="callees">Callees (down)</option>
          <option value="callers">Callers (up)</option>
        </select>
        <button class="btn" onclick="runCallflow()">Render</button>
      </div>
      <div id="callflow-result"><div style="color:var(--text-3);font-size:.82rem">Enter a symbol name to render its call chain.</div></div>
    </div>
  </div>

  <!-- PAGE: DEDUP -->
  <div id="page-dedup" class="page">
    <div class="page-title">Graph Deduplication</div>
    <div class="card">
      <div class="card-title" style="margin-bottom:10px">Deduplicate Graph Nodes</div>
      <p style="font-size:.79rem;color:var(--text-3);margin-bottom:12px">Find and merge duplicate nodes using MinHash/LSH + Jaro-Winkler similarity. Run dry-run first to preview.</p>
      <div class="search-row" style="margin-bottom:8px">
        <label style="font-size:.81rem;white-space:nowrap;color:var(--text-2)">Threshold:</label>
        <input id="dedup-threshold" type="number" value="0.88" min="0.5" max="1.0" step="0.01" style="width:80px"/>
        <button class="btn secondary" onclick="runDedup(true)">Dry Run (Preview)</button>
        <button class="btn" style="background:var(--red)" onclick="runDedup(false)">Apply Merge</button>
      </div>
      <div id="dedup-result"><div style="color:var(--text-3);font-size:.82rem">Click "Dry Run" to preview what would be merged.</div></div>
    </div>
  </div>

  <div id="page-file-tree" class="page">
    <div class="page-title">File Tree</div>
    <div class="card">
      <div class="card-title" style="margin-bottom:10px">Interactive File Tree</div>
      <p style="font-size:.79rem;color:var(--text-3);margin-bottom:12px">Collapsible file tree generated from the graph's indexed file nodes.</p>
      <div id="file-tree-wrap" style="margin-top:4px;border:1px solid var(--border);border-radius:var(--radius);overflow:hidden;min-height:60px"><div class="loader">Loading file tree…</div></div>
    </div>
  </div>

  <div id="page-pr-impact" class="page">
    <div class="page-title">PR Impact Analysis</div>
    <div class="card">
      <div class="card-title" style="margin-bottom:10px">Changed Files → Graph Impact</div>
      <p style="font-size:.79rem;color:var(--text-3);margin-bottom:12px">Paste changed files (one per line) or leave empty to auto-detect from <code>git diff main...HEAD</code>.</p>
      <div style="margin-bottom:8px">
        <label style="font-size:.81rem;color:var(--text-2)">Base branch:</label>
        <input id="pr-base-branch" type="text" value="main" style="width:100px;margin-left:6px"/>
      </div>
      <textarea id="pr-files-input" placeholder="Paste changed file paths here (optional)" rows="5" style="width:100%;box-sizing:border-box;background:var(--surface-2);color:var(--text-1);border:1px solid var(--border);border-radius:var(--radius);padding:8px;font-family:monospace;font-size:.82rem;resize:vertical"></textarea>
      <button class="btn" style="margin-top:8px" onclick="runPrImpact()">Analyze Impact</button>
      <div id="pr-impact-result" style="margin-top:12px"></div>
    </div>
  </div>

  <div id="page-vacuum" class="page">
    <div class="page-title">Storage Vacuum</div>
    <div class="card">
      <div class="card-title" style="margin-bottom:10px">Orphan Index Cleanup</div>
      <p style="font-size:.79rem;color:var(--text-3);margin-bottom:12px">Remove orphan <code>index_budget/</code>, <code>index_balanced/</code> directories left from embedding tier upgrades. These can waste tens of GB.</p>
      <button class="btn secondary" onclick="runVacuum(true)">Dry Run (Preview)</button>
      <button class="btn" style="background:var(--red);margin-left:8px" onclick="runVacuum(false)">Apply Vacuum</button>
      <div id="vacuum-result" style="margin-top:12px"></div>
    </div>
  </div>

  <!-- PAGE: JOBS -->
  <div id="page-jobs" class="page">
    <div class="page-title">Background Jobs</div>
    <div class="card" style="margin-bottom:12px">
      <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
        <span style="font-size:.81rem;color:var(--text-3)">Filter:</span>
        <select id="jobs-filter-status" onchange="loadJobs()" style="font-size:.79rem;padding:4px 8px;border:1px solid var(--border);background:var(--surface-2);color:var(--text);border-radius:var(--radius)">
          <option value="">All statuses</option>
          <option value="queued">Queued</option>
          <option value="running">Running</option>
          <option value="ok">Completed</option>
          <option value="error">Error</option>
          <option value="cancelled">Cancelled</option>
        </select>
        <button class="btn secondary" onclick="loadJobs()" style="font-size:.78rem;padding:4px 10px">↺ Refresh</button>
        <span id="jobs-count" style="font-size:.79rem;color:var(--text-3)"></span>
      </div>
    </div>
    <div id="jobs-table" class="card">
      <div class="loader">Loading jobs…</div>
    </div>
  </div>

  <!-- PAGE: SAVED QUERIES -->
  <div id="page-saved-queries" class="page">
    <div class="page-title">Saved Queries</div>
    <div class="card" style="margin-bottom:14px">
      <div class="card-title" style="margin-bottom:10px">Save a Query</div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:8px">
        <div>
          <label style="font-size:.75rem;color:var(--text-3);display:block;margin-bottom:3px">Query *</label>
          <input id="sq-query" type="text" placeholder="e.g. payment handler gRPC" style="width:100%;font-size:.81rem;padding:6px 10px;border:1px solid var(--border-2);background:var(--surface-2);color:var(--text);border-radius:var(--radius)" onkeydown="if(event.key==='Enter')saveQuery()"/>
        </div>
        <div>
          <label style="font-size:.75rem;color:var(--text-3);display:block;margin-bottom:3px">Name (optional)</label>
          <input id="sq-name" type="text" placeholder="e.g. Payment gRPC handler" style="width:100%;font-size:.81rem;padding:6px 10px;border:1px solid var(--border-2);background:var(--surface-2);color:var(--text);border-radius:var(--radius)"/>
        </div>
        <div>
          <label style="font-size:.75rem;color:var(--text-3);display:block;margin-bottom:3px">Scope</label>
          <select id="sq-scope" style="width:100%;font-size:.81rem;padding:6px 10px;border:1px solid var(--border-2);background:var(--surface-2);color:var(--text);border-radius:var(--radius)">
            <option value="code">Code</option>
            <option value="docs">Docs</option>
            <option value="all">All</option>
          </select>
        </div>
        <div>
          <label style="font-size:.75rem;color:var(--text-3);display:block;margin-bottom:3px">Note (optional)</label>
          <input id="sq-note" type="text" placeholder="e.g. for auth debugging" style="width:100%;font-size:.81rem;padding:6px 10px;border:1px solid var(--border-2);background:var(--surface-2);color:var(--text);border-radius:var(--radius)"/>
        </div>
      </div>
      <button class="btn" onclick="saveQuery()">Save Query</button>
    </div>
    <div id="sq-list">
      <div class="loader">No saved queries yet.</div>
    </div>
  </div>

  </main>
</div><!-- end .main-wrap -->
</div><!-- end .app -->

<!-- Toast container -->
<div id="toast-container"></div>

<script>
/* ── State ─────────────────────────────────────────────────────────────────── */
let currentProject = '';
let sidebarCollapsed = false;
let _graphAnim = null;
const $ = id => document.getElementById(id);

/* ── Helpers ────────────────────────────────────────────────────────────────── */
function escHtml(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;')}
function escAttr(s){return String(s).replace(/'/g,"\\'")}
async function api(path){const r=await fetch('/api'+path);if(!r.ok)throw new Error(await r.text());return r.json()}

function toast(msg,type='info'){
  const t=document.createElement('div');t.className='toast '+type;t.textContent=msg;
  $('toast-container').appendChild(t);
  setTimeout(()=>{t.style.opacity='0';t.style.transform='translateX(20px)';setTimeout(()=>t.remove(),200)},3000);
}

function simpleMarkdown(md){
  return md
    .replace(/^### (.+)$/gm,'<h3>$1</h3>')
    .replace(/^## (.+)$/gm,'<h2>$1</h2>')
    .replace(/^# (.+)$/gm,'<h1>$1</h1>')
    .replace(/\*\*(.+?)\*\*/g,'<strong>$1</strong>')
    .replace(/`([^`]+)`/g,'<code>$1</code>')
    .replace(/^- (.+)$/gm,'<li>$1</li>')
    .replace(/\n{2,}/g,'</p><p>')
    .replace(/^(?!<[hup])(.+)$/gm,'<p>$1</p>');
}

/* ── Layout ──────────────────────────────────────────────────────────────────── */
function toggleSidebar(){
  sidebarCollapsed=!sidebarCollapsed;
  const sb=$('sidebar');
  sb.classList.toggle('collapsed',sidebarCollapsed);
  $('sidebar-toggle').textContent=sidebarCollapsed?'›':'‹';
}

function toggleTheme(){
  const html=document.documentElement;
  const isLight=html.dataset.theme==='light';
  html.dataset.theme=isLight?'dark':'light';
  $('theme-btn').textContent=isLight?'☀':'🌙';
}

function toggleMoreNav(){
  const drawer=$('more-drawer');
  const toggle=$('more-toggle');
  const open=drawer.classList.toggle('open');
  toggle.classList.toggle('open',open);
}

/* ── Navigation ──────────────────────────────────────────────────────────────── */
const _PAGE_LOAD={
  overview:loadOverview, search:()=>{}, ask:loadAskSuggestions, graph:()=>{},
  'saved-queries':loadSavedQueries,
  structure:loadStructure, patterns:loadPatterns, wiki:loadWikiList,
  communities:loadCommunities, health:()=>{loadStatus();loadMetricsCharts();loadAlerts();}, verify:loadVerify,
  release:loadRelease, qa:loadQaGate, projects:()=>{}, integrations:loadIntegrations,
  jobs:loadJobs,
  'arch-map':loadArchMap, 'service-mesh':loadServiceMesh, 'fed-map':loadFedMap,
  impact:loadTopImpact, trace:()=>{},
  sysstat:loadSysstat,
  'file-tree':loadFileTree, 'pr-impact':loadPrImpactAuto, vacuum:loadVacuumStatus,
  'import-cycles':loadImportCycles, callflow:()=>{}, dedup:loadDedupStatus,
  feature:()=>{},
};

function showPage(name){
  document.querySelectorAll('.page').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.nav-btn').forEach(b=>b.classList.remove('active'));
  const page=$('page-'+name);if(page)page.classList.add('active');
  const btn=$('nav-'+name);if(btn){btn.classList.add('active');
    // Auto-expand More drawer if the target page lives inside it
    if(btn.closest('#more-drawer')){
      $('more-drawer').classList.add('open');
      $('more-toggle').classList.add('open');
    }
  }
  if(_PAGE_LOAD[name])_PAGE_LOAD[name]();
}

function showTab(name){showPage(name)}

function switchProject(p){
  currentProject=p;
  try{localStorage.setItem('opencode_selected_project',p);}catch(e){}
  const active=document.querySelector('.nav-btn.active');
  if(active){const m=active.id.match(/^nav-(.+)/);if(m&&_PAGE_LOAD[m[1]])_PAGE_LOAD[m[1]]();}
}

async function quickSearch(){
  const q=$('global-q').value.trim();if(!q)return;
  const scope=$('global-scope').value;
  $('search-q').value=q;$('search-scope').value=scope;
  showPage('search');await runSearch();
}

/* ── Projects ────────────────────────────────────────────────────────────────── */
async function loadProjects(){
  const data=await api('/projects');
  const projects=data.projects||[];
  const sel=$('project-select');
  sel.innerHTML=projects.map(p=>`<option value="${escAttr(p.path)}">${escHtml(p.path.split('/').slice(-2).join('/'))}</option>`).join('');
  if(projects.length){
    let saved=null;try{saved=localStorage.getItem('opencode_selected_project');}catch(e){}
    const match=saved&&projects.find(p=>p.path===saved||p.path.split('/').pop()===saved.split('/').pop());
    currentProject=(match?match.path:projects[0].path);sel.value=currentProject;
  }

  const rows=projects.map(p=>`<tr>
    <td style="max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${escAttr(p.path)}">${escHtml(p.path)}</td>
    <td>${p.indexed_at?'<span class="badge ok">indexed</span>':'<span class="badge none">—</span>'}</td>
    <td>${(p.file_count||0).toLocaleString()}</td>
    <td>${p.chunks!=null?p.chunks.toLocaleString():'—'}</td>
    <td>${p.watching?'<span class="badge ok">watching</span>':'—'}</td>
  </tr>`).join('');
  $('projects-table').innerHTML=`<table><thead><tr><th>Path</th><th>Status</th><th>Files</th><th>Chunks</th><th>Watching</th></tr></thead><tbody>${rows}</tbody></table>`;
  $('daemon-dot').className='daemon-dot ok';
  $('daemon-status').textContent='connected';
}

/* ── Overview ────────────────────────────────────────────────────────────────── */
async function loadOverview(){
  if(!currentProject)return;
  try{
    const[health,kbh,aps,over,alerts]=await Promise.all([
      fetch('/healthz').then(r=>r.ok?r.json():{}),
      api('/kb_health?project='+encodeURIComponent(currentProject)),
      api('/auto_pipeline_status'),
      api('/overview?project='+encodeURIComponent(currentProject)),
      fetch('/api/alerts').then(r=>r.ok?r.json():{violations:[],rules:[]}),
    ]);
    const gs=over.graph_stats||{};
    const pctRaw=kbh.enrichment_pct!=null?kbh.enrichment_pct:null;
    const pct=pctRaw!=null?pctRaw.toFixed(0):'—';
    const load=health.load_avg||{};
    const load1=load['1m']??null;
    const cpus=health.cpu_count||1;

    // Last updated timestamp
    const lu=$('overview-last-updated');
    if(lu)lu.textContent='Updated '+new Date().toLocaleTimeString();

    // KPI cards — include load indicator
    const loadCls=load1===null?'':load1>18?'err':load1>cpus*2?'warn':'ok';
    const kpis=[
      {val:(gs.node_count||0).toLocaleString(),lbl:'Graph Nodes',icon:'🕸',cls:gs.node_count>0?'ok':''},
      {val:(gs.edge_count||0).toLocaleString(),lbl:'Graph Edges',icon:'➜',cls:''},
      {val:(kbh.wiki_page_count||0).toLocaleString(),lbl:'Wiki Pages',icon:'📖',cls:kbh.wiki_page_count>0?'ok':''},
      {val:(gs.total_communities||0).toLocaleString(),lbl:'Communities',icon:'🏘',cls:''},
      {val:pct+'%',lbl:'Enriched',icon:'✨',cls:pctRaw>=90?'ok':pctRaw>=50?'warn':''},
      {val:load1!==null?load1.toFixed(2):'—',lbl:'Load avg 1m',icon:'⚡',cls:loadCls},
      {val:health.active_clients!=null?String(health.active_clients):'—',lbl:'Active Clients',icon:'👁',cls:''},
    ];
    $('overview-kpi').innerHTML=kpis.map((k,i)=>`<div class="kpi-card ${k.cls}" id="kpi-card-${i}"><div class="kpi-val">${escHtml(k.val)}</div><div class="kpi-label">${k.lbl}</div><div class="kpi-icon">${k.icon}</div><canvas class="kpi-sparkline" id="kpi-spark-${i}" height="30"></canvas></div>`).join('');
    _drawKpiSparklines(kpis);

    updateMetricStrip(kbh,gs,health);

    // System Health card
    const daemonOk=health.ok===true||health.healthy===true;
    const kbOk=pctRaw!=null&&pctRaw>80&&kbh.wiki_page_count>0;
    const hb=$('health-badge');
    hb.className='badge '+(daemonOk&&kbOk?'ok':'warn');
    hb.textContent=daemonOk&&kbOk?'Healthy':'Degraded';
    const uptimeStr=health.uptime_s!=null?_fmtUptime(health.uptime_s):'—';
    $('overview-health').innerHTML=[
      {label:'Daemon',val:daemonOk?'● Running':'○ Down',cls:daemonOk?'ok':'err'},
      {label:'Uptime',val:uptimeStr,cls:''},
      {label:'Active clients',val:health.active_clients??'—',cls:''},
      {label:'Port',val:health.port||8765,cls:''},
    ].map(r=>`<div style="display:flex;justify-content:space-between;align-items:center;padding:6px 0;font-size:.81rem;border-bottom:1px solid var(--surface-2)">
      <span style="color:var(--text-3)">${r.label}</span>
      <span style="color:${r.cls==='ok'?'var(--green)':r.cls==='err'?'var(--red)':'var(--text-2)'};font-weight:${r.cls?'600':'400'}">${escHtml(String(r.val))}</span></div>`).join('');

    // System Load card
    const lb=$('load-badge');
    if(lb){
      lb.className='badge '+(load1===null?'none':load1>18?'err':load1>cpus*2?'warn':'ok');
      lb.textContent=load1===null?'—':load1>18?'Critical':load1>cpus*2?'High':'Normal';
    }
    if($('overview-load')){
      const loadBar=(v,max)=>{
        const pctV=Math.min(100,Math.round((v/max)*100));
        const barCls=v>18?'var(--red)':v>cpus*2?'var(--amber)':'var(--green)';
        return `<div class="progress-bar" style="margin-bottom:2px"><div class="progress-fill" style="width:${pctV}%;background:${barCls}"></div></div>`;
      };
      const maxLoad=Math.max(cpus*3,load1||0,load['5m']||0,load['15m']||0,20);
      $('overview-load').innerHTML=`
        <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;margin-bottom:12px">
          ${[['1m',load['1m']],['5m',load['5m']],['15m',load['15m']]].map(([lbl,v])=>{
            const vv=v!=null?v.toFixed(2):'—';
            const vCls=v>18?'var(--red)':v!=null&&v>cpus*2?'var(--amber)':'var(--text-1)';
            return `<div style="text-align:center;background:var(--surface-2);border-radius:var(--radius);padding:10px 6px">
              <div style="font-size:1.3rem;font-weight:700;color:${vCls};font-variant-numeric:tabular-nums">${vv}</div>
              <div style="font-size:.72rem;color:var(--text-3);margin-top:2px">Load ${lbl}</div>
              ${v!=null?loadBar(v,maxLoad):''}
            </div>`;
          }).join('')}
        </div>
        <div style="font-size:.77rem;color:var(--text-3)">${cpus} CPU core${cpus!==1?'s':''} · Threshold: >18 = Critical, >${(cpus*2).toFixed(0)} = High</div>
        ${load1>18?`<div style="margin-top:8px;padding:6px 10px;background:var(--red-bg,rgba(255,64,96,.12));border:1px solid var(--red);border-radius:var(--radius);font-size:.79rem;color:var(--red)">⚠ Load 1m (${load1.toFixed(2)}) exceeds critical threshold (18). System may be overloaded.</div>`:''}`;
    }

    // KB completeness
    const byLevel=kbh.enrichment_by_level||{};
    const levelKeys=Object.keys(byLevel).sort((a,b)=>+a-+b);
    const levelRows=levelKeys.map(lvl=>{
      const d=byLevel[lvl];
      const lpct=d.pct||0;
      const cls=lpct>=90?'var(--green)':lpct>=50?'var(--orange)':'var(--red)';
      return `<tr style="font-size:.76rem">
        <td style="color:var(--text-3);padding:2px 6px 2px 0">L${lvl}</td>
        <td style="color:var(--text-2);padding:2px 4px">${d.enriched}/${d.total}</td>
        <td><div style="width:80px;height:6px;background:var(--surface-3);border-radius:3px;overflow:hidden"><div style="height:100%;width:${lpct}%;background:${cls}"></div></div></td>
        <td style="color:${cls};padding:2px 0 2px 6px;font-weight:600">${lpct.toFixed(0)}%</td>
      </tr>`;
    }).join('');
    $('overview-kb').innerHTML=`
      <div style="display:flex;justify-content:space-between;font-size:.79rem;color:var(--text-3);margin-bottom:4px">
        <span>${kbh.enriched_communities||0}/${kbh.total_communities||0} enriched</span>
        <span style="font-weight:700;color:${pctRaw>=90?'var(--green)':pctRaw>=50?'var(--amber)':'var(--red)'}">${pct}%</span>
      </div>
      <div class="progress-bar" style="margin-bottom:8px"><div class="progress-fill" style="width:${pctRaw||0}%;background:${pctRaw>=90?'var(--green)':pctRaw>=50?'var(--amber)':'var(--red)'}"></div></div>
      ${levelKeys.length>1?`<table style="border-collapse:collapse;width:100%">${levelRows}</table>`:''}
      <div style="margin-top:8px;font-size:.77rem;color:var(--text-3);display:flex;gap:12px;flex-wrap:wrap">
        <span>📖 ${kbh.wiki_page_count||0} wiki pages</span>
        <span>${kbh.patterns_cached?'✓ Patterns cached':'✗ No patterns'}</span>
      </div>`;

    // Recent events
    const events=(aps.events||[]).slice(-10).reverse();
    $('overview-events').innerHTML=events.length
      ?events.map(e=>{
        const dotCls=e.status==='ok'?'ok':e.status==='error'?'error':'scheduled';
        const at=e.at?new Date(e.at).toLocaleTimeString():'';
        const proj=(e.project||'').split('/').pop();
        return `<div class="activity-item"><div class="activity-dot ${dotCls}"></div>
          <div class="activity-text"><span style="color:var(--text-2)">${escHtml(proj)}</span> <span style="color:var(--text-3)">${escHtml(e.step||e.action||'')} ${escHtml(e.status)}</span></div>
          <div class="activity-time">${at}</div></div>`;
      }).join('')
      :'<div style="color:var(--text-3);font-size:.81rem;padding:10px">No events yet this session.</div>';

    // Active alerts panel on overview
    const viols=(alerts&&alerts.violations)||[];
    if($('overview-alerts')){
      $('overview-alerts').innerHTML=viols.length
        ?viols.map(v=>`<div style="display:flex;align-items:center;gap:8px;padding:7px 10px;background:rgba(255,64,96,.08);border:1px solid rgba(255,64,96,.3);border-radius:var(--radius);margin-bottom:6px">
            <span style="color:var(--red);font-size:.9rem">⚠</span>
            <div style="flex:1">
              <div style="font-size:.81rem;font-weight:600;color:var(--red)">${escHtml(v.name)}</div>
              <div style="font-size:.76rem;color:var(--text-3)">${escHtml(v.message||'')}</div>
            </div>
          </div>`).join('')
        :`<div style="display:flex;align-items:center;gap:8px;padding:8px 10px;color:var(--green);font-size:.81rem"><span>✓</span><span>All clear — no active violations</span></div>`;
    }
  }catch(e){
    $('overview-kpi').innerHTML=`<div style="color:var(--red);font-size:.81rem;padding:10px">Failed: ${escHtml(e.message)}</div>`;
  }
}

function _fmtUptime(s){
  if(s<60)return s.toFixed(0)+'s';
  if(s<3600)return Math.floor(s/60)+'m '+Math.floor(s%60)+'s';
  return Math.floor(s/3600)+'h '+Math.floor((s%3600)/60)+'m';
}

let _kpiSparkCharts=[];
async function _drawKpiSparklines(kpis){
  if(typeof Chart==='undefined')return;
  // Fetch last 1h bucketed metrics for sparklines
  let hist;
  try{hist=await fetch('/api/metrics/history?hours=1&bucket_m=2').then(r=>r.ok?r.json():null);}
  catch(e){return;}
  if(!hist||!hist.timestamps||!hist.timestamps.length)return;
  // Destroy old sparkline charts
  _kpiSparkCharts.forEach(c=>{try{c.destroy();}catch(e){}});
  _kpiSparkCharts=[];
  // Map: kpi index → dataset. We only draw latency+search count sparklines.
  const sparkData={
    0:hist.search_count||[], // nodes → use search count as proxy activity
    4:hist.latency_p50||[],  // enriched% → show latency trend as gauge of activity
    5:hist.latency_p95||[],  // load avg → p95 latency
  };
  const labels=hist.timestamps;
  kpis.forEach((_,i)=>{
    const canvas=$('kpi-spark-'+i);
    if(!canvas||!sparkData[i])return;
    const data=sparkData[i];
    if(!data.some(v=>v>0))return;
    const color=kpis[i].cls==='ok'?'#00c28e':kpis[i].cls==='warn'?'#ffb800':kpis[i].cls==='crit'?'#ff4060':'#7b61ff';
    try{
      const c=new Chart(canvas,{type:'line',data:{labels,datasets:[{data,borderColor:color,backgroundColor:color+'22',borderWidth:1.2,pointRadius:0,fill:true,tension:.4}]},
        options:{animation:false,responsive:true,plugins:{legend:{display:false},tooltip:{enabled:false}},
          scales:{x:{display:false},y:{display:false}}}});
      _kpiSparkCharts.push(c);
    }catch(e){}
  });
}

function updateMetricStrip(kbh,gs,health){
  const daemonOk=health&&(health.ok===true||health.healthy===true);
  const pct=kbh&&kbh.enrichment_pct!=null?kbh.enrichment_pct:null;
  const load=health&&health.load_avg;
  const load1=load?load['1m']:null;
  const loadCls=load1===null?'':load1>18?'err':load1>((health&&health.cpu_count||1)*2)?'warn':'ok';
  const pills=[
    {val:daemonOk?'● Daemon up':'○ Daemon down',cls:daemonOk?'ok':'err'},
    {val:(gs&&gs.node_count?gs.node_count.toLocaleString():'0')+' nodes',cls:gs&&gs.node_count>0?'ok':''},
    {val:(kbh&&kbh.wiki_page_count?kbh.wiki_page_count.toLocaleString():'0')+' wiki',cls:kbh&&kbh.wiki_page_count>0?'ok':''},
    {val:(pct!=null?pct.toFixed(0):'—')+'% enriched',cls:pct>=90?'ok':pct>=50?'warn':''},
    {val:'Load '+( load1!==null?load1.toFixed(2):'—'),cls:loadCls},
  ];
  $('metric-strip').innerHTML=pills.map(p=>`<div class="metric-pill ${p.cls}"><span class="pill-val">${escHtml(p.val)}</span></div>`).join('');
}

/* ── Structure ───────────────────────────────────────────────────────────────── */
async function loadStructure(){
  if(!currentProject)return;
  $('structure-tree').textContent='Loading…';
  const data=await api('/overview?project='+encodeURIComponent(currentProject));
  $('structure-tree').textContent=data.directory_tree||'';
  const langs=data.language_breakdown||[];
  const maxCount=langs[0]?.count||1;
  $('lang-breakdown').innerHTML=langs.slice(0,20).map(l=>`
    <div class="lang-bar">
      <span class="name">${escHtml(l.extension)}</span>
      <div class="bar"><div class="fill" style="width:${(l.count/maxCount*100).toFixed(1)}%"></div></div>
      <span class="count">${l.count.toLocaleString()}</span>
    </div>`).join('');
  const gs=data.graph_stats||{};
  const enriched=gs.enriched_communities||0;const total=gs.total_communities||0;
  $('graph-stats').innerHTML=[
    {val:data.file_count?.toLocaleString()||'—',lbl:'Files'},
    {val:gs.total_communities?.toLocaleString()||'—',lbl:'Communities'},
    {val:gs.enriched_communities?.toLocaleString()||'—',lbl:'Enriched'},
    {val:total?(enriched/total*100).toFixed(0)+'%':'—',lbl:'Enriched %'},
  ].map(s=>`<div class="stat-box"><div class="val">${s.val}</div><div class="lbl">${s.lbl}</div></div>`).join('');
}

/* ── Patterns ────────────────────────────────────────────────────────────────── */
async function loadPatterns(){
  if(!currentProject)return;
  $('patterns-arch').innerHTML='<div class="loader">Detecting patterns…</div>';
  $('patterns-langs').innerHTML='';$('patterns-conventions').innerHTML='';
  $('patterns-deps').innerHTML='<div class="loader">Loading…</div>';
  let data;
  try{data=await api('/patterns?project='+encodeURIComponent(currentProject));}
  catch(e){$('patterns-arch').innerHTML=`<div style="color:var(--red);padding:10px">${escHtml(e.message)}</div>`;return;}

  const arch=data.architecture||'unknown';const ms=data.module_structure||{};
  $('patterns-arch').innerHTML=[
    {val:escHtml(arch),lbl:'Architecture'},
    {val:escHtml(ms.type||'unknown'),lbl:'Module Layout'},
    {val:(data.version_summary?.total||0).toLocaleString(),lbl:'Total Deps'},
    {val:(data.version_summary?.pinned||0).toLocaleString(),lbl:'Pinned Deps'},
  ].map(s=>`<div class="stat-box"><div class="val" style="font-size:.95rem">${s.val}</div><div class="lbl">${s.lbl}</div></div>`).join('');
  const fws=data.key_frameworks||[];
  $('patterns-frameworks').innerHTML=fws.length
    ?'<div style="margin-top:8px">'+fws.map(f=>`<span class="badge ok" style="margin:2px 4px 2px 0">${escHtml(f)}</span>`).join('')+'</div>':'';
  const langs=data.languages||[];const maxFiles=langs[0]?.files||1;
  $('patterns-langs').innerHTML=langs.slice(0,15).map(l=>`
    <div class="lang-bar">
      <span class="name" style="width:90px">${escHtml(l.name)}</span>
      <div class="bar"><div class="fill" style="width:${(l.files/maxFiles*100).toFixed(1)}%"></div></div>
      <span class="count">${l.files?.toLocaleString()} <span style="color:var(--text-3)">(${l.percentage}%)</span></span>
    </div>`).join('')||'<div style="color:var(--text-3);font-size:.81rem">No language data.</div>';
  const conv=data.conventions||{};
  $('patterns-conventions').innerHTML=[
    {val:escHtml(conv.language||'—'),lbl:'Primary Lang'},
    {val:escHtml(conv.naming||'—'),lbl:'Naming'},
    {val:escHtml(conv.test_style||'—'),lbl:'Test Style'},
    {val:escHtml(conv.error_handling||'—'),lbl:'Error Handling'},
    {val:escHtml(conv.logging_lib||'—'),lbl:'Logging'},
    {val:(conv.common_struct_tags||[]).join(', ')||'—',lbl:'Struct Tags'},
  ].map(s=>`<div class="stat-box"><div class="val" style="font-size:.88rem">${s.val}</div><div class="lbl">${s.lbl}</div></div>`).join('');
  const dep=data.dependencies||{};const pkgs=dep.packages||[];const manifests=dep.manifest_files||[];
  const llm=data.llm_analysis;const llmAt=data.llm_cached_at;
  if(llm&&typeof llm==='object'&&!llm.raw_response){
    $('patterns-llm-meta').textContent=`LLM analysis cached at: ${llmAt||'unknown'} · Confidence: ${llm.confidence||'—'}`;
    const llmItems=[
      llm.primary_language&&{label:'Primary Language',val:llm.primary_language},
      llm.architecture_description&&{label:'Architecture',val:llm.architecture_description},
      llm.naming_conventions&&{label:'Naming',val:llm.naming_conventions},
      llm.error_handling_style&&{label:'Error Handling',val:llm.error_handling_style},
      llm.test_approach&&{label:'Test Approach',val:llm.test_approach},
    ].filter(Boolean);
    const patterns=(llm.coding_patterns||[]).map(p=>`<span class="badge ok" style="margin:2px">${escHtml(p)}</span>`).join('');
    const abstractions=(llm.key_abstractions||[]).map(a=>`<li style="font-size:.79rem;color:var(--text-2)">${escHtml(a)}</li>`).join('');
    $('patterns-llm-result').innerHTML=llmItems.map(i=>
      `<div style="margin-bottom:8px"><div style="font-size:.71rem;color:var(--text-3);text-transform:uppercase;letter-spacing:.04em">${i.label}</div><div style="font-size:.81rem;color:var(--text);margin-top:2px">${escHtml(i.val)}</div></div>`
    ).join('')+(patterns?`<div style="margin-top:8px"><div style="font-size:.71rem;color:var(--text-3);text-transform:uppercase;letter-spacing:.04em">Coding Patterns</div><div style="margin-top:4px">${patterns}</div></div>`:'')+
    (abstractions?`<div style="margin-top:8px"><div style="font-size:.71rem;color:var(--text-3);text-transform:uppercase;letter-spacing:.04em">Key Abstractions</div><ul style="margin-left:16px;margin-top:4px">${abstractions}</ul></div>`:'');
  }else if(llm&&llm.raw_response){
    $('patterns-llm-meta').textContent=`LLM analysis cached (raw) at: ${llmAt||'unknown'}`;
    $('patterns-llm-result').innerHTML=`<pre style="font-size:.71rem">${escHtml(llm.raw_response.slice(0,500))}</pre>`;
  }
  $('patterns-dep-meta').textContent=`Manager: ${dep.manager||'—'} · Manifests: ${manifests.join(', ')||'—'} · Packages: ${pkgs.length}`;
  $('patterns-deps').innerHTML=pkgs.length
    ?`<table><thead><tr><th>Package</th><th>Version</th><th>Type</th></tr></thead><tbody>${
      pkgs.slice(0,150).map(p=>`<tr><td style="font-family:monospace;font-size:.77rem">${escHtml(p.name)}</td><td style="font-family:monospace;font-size:.77rem;color:var(--green)">${escHtml(p.version)}</td><td>${p.direct?'<span class="badge ok">direct</span>':'<span class="badge none">indirect</span>'}</td></tr>`
      ).join('')}</tbody></table>`
    :'<div style="color:var(--text-3);font-size:.81rem;padding:10px">No dependency manifests found.</div>';
}

async function runLLMAnalysis(force){
  if(!currentProject)return;
  $('patterns-llm-meta').textContent='Running LLM analysis… (30-120s)';
  $('patterns-llm-result').innerHTML='<div class="loader">Calling LLM…</div>';
  try{
    const url=`/api/analyze_patterns?project=${encodeURIComponent(currentProject)}${force?'&force=true':''}`;
    const r=await fetch(url,{method:'POST'});const data=await r.json();
    if(data.error){$('patterns-llm-meta').textContent='LLM analysis failed: '+data.error;$('patterns-llm-result').innerHTML='';return;}
    await loadPatterns();
  }catch(e){$('patterns-llm-meta').textContent='LLM error: '+escHtml(e.message);$('patterns-llm-result').innerHTML='';}
}

/* ── Architecture / Communities ──────────────────────────────────────────────── */
async function loadArchitectureSynthesis(){
  if(!currentProject)return;
  try{
    const data=await api('/patterns?project='+encodeURIComponent(currentProject));
    const llm=data.llm_analysis;const arch=data.architecture||'unknown';
    if(llm&&llm.architecture_description){
      const conf=llm.confidence?`<span class="badge ${llm.confidence==='high'?'ok':'none'}" style="margin-left:8px">${llm.confidence}</span>`:'';
      const abs=(llm.key_abstractions||[]).slice(0,6).map(a=>`<span class="badge ok" style="margin:2px 4px 2px 0">${escHtml(a)}</span>`).join('');
      $('arch-synthesis-content').innerHTML=
        `<div style="font-size:.84rem;color:var(--text);line-height:1.6;margin-bottom:10px">${escHtml(llm.architecture_description)}${conf}</div>`+
        (llm.primary_language?`<div style="font-size:.74rem;color:var(--text-3);margin-bottom:6px">Primary: <strong style="color:var(--accent)">${escHtml(llm.primary_language)}</strong> &nbsp;·&nbsp; Style: <strong style="color:var(--accent)">${escHtml(arch)}</strong></div>`:'')+
        (abs?`<div style="margin-top:8px"><span style="font-size:.71rem;color:var(--text-3);text-transform:uppercase;letter-spacing:.04em">Key Abstractions</span><div style="margin-top:4px">${abs}</div></div>`:'');
    }else{
      $('arch-synthesis-content').innerHTML=`<div style="color:var(--text-3);font-size:.81rem">Architecture: <strong style="color:var(--text-2)">${escHtml(arch)}</strong>. Run "Analyse with LLM" on Patterns tab for full synthesis.</div>`;
    }
  }catch(e){$('arch-synthesis-content').innerHTML=`<div style="color:var(--text-3);font-size:.81rem">Unavailable: ${escHtml(e.message)}</div>`;}
}

async function loadCommunities(){
  if(!currentProject)return;
  loadArchitectureSynthesis();
  $('communities-list').innerHTML='<div class="loader">Loading…</div>';
  const data=await api('/communities?project='+encodeURIComponent(currentProject)+'&top_k=50');
  const cs=data.communities||[];
  const enriched=cs.filter(c=>c.title&&c.title!==`Community ${c.id}`).length;
  $('enrichment-progress').innerHTML=`
    <div style="display:flex;justify-content:space-between;font-size:.74rem;color:var(--text-3)">
      <span>Enriched ${enriched} of ${cs.length} shown</span><span>${data.total||cs.length} total</span>
    </div>
    <div class="progress-bar"><div class="progress-fill" style="width:${cs.length?(enriched/cs.length*100).toFixed(0):0}%"></div></div>`;
  $('communities-list').innerHTML=cs.slice(0,30).map(c=>`
    <div class="community-card">
      <div style="display:flex;justify-content:space-between;align-items:start">
        <strong style="color:var(--accent);font-size:.84rem">${escHtml(c.title||'Community '+c.id)}</strong>
        <span style="font-size:.71rem;color:var(--text-3)">${c.node_count} nodes</span>
      </div>
      <p style="font-size:.77rem;color:var(--text-2);margin-top:6px;line-height:1.5">${escHtml((c.summary||'').slice(0,300))}</p>
      ${c.key_entry_points?.length?`<div style="margin-top:6px;font-size:.71rem;color:var(--text-3)">Entry: ${c.key_entry_points.slice(0,3).map(e=>escHtml(typeof e==='string'?e.split('/').pop():'')).join(', ')}</div>`:''}
    </div>`).join('');
  // God nodes panel
  const godNodes=data.god_nodes||[];
  if(godNodes.length&&$('god-nodes-panel')){
    $('god-nodes-panel').style.display='';
    $('god-nodes-list').innerHTML=godNodes.map(n=>`
      <div style="display:flex;align-items:center;gap:8px;padding:5px 0;border-bottom:1px solid var(--surface-2)">
        <span style="color:var(--amber);font-size:.7rem;min-width:36px;text-align:right">×${n.degree}</span>
        <span style="color:var(--text);font-size:.78rem;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${escHtml(n.qualified_name)}">${escHtml(n.qualified_name.split('/').pop().split('::').pop())}</span>
        <span style="color:var(--text-3);font-size:.7rem">${escHtml(n.kind)}</span>
        <span style="color:var(--text-3);font-size:.68rem">↑${n.in_degree} ↓${n.out_degree}</span>
      </div>`).join('');
  }
  // Cross-community bridges panel
  const bridges=data.cross_community_bridges||[];
  if(bridges.length&&$('bridges-panel')){
    $('bridges-panel').style.display='';
    $('bridges-list').innerHTML=bridges.map(b=>`
      <div style="padding:4px 0;border-bottom:1px solid var(--surface-2);font-size:.76rem">
        <span style="color:var(--accent)">${escHtml(b.from.split('::').pop())}</span>
        <span style="color:var(--text-3);margin:0 4px">→</span>
        <span style="color:var(--green)">${escHtml(b.to.split('::').pop())}</span>
        <span style="color:var(--text-3);font-size:.68rem;margin-left:6px">[${escHtml(b.from_community+'→'+b.to_community)}]</span>
      </div>`).join('');
  }
}

/* ── Graph ───────────────────────────────────────────────────────────────────── */
async function runGraph(){
  const sym=$('graph-symbol').value.trim();const rel=$('graph-relation').value;const to=$('graph-to').value.trim();
  if(!sym||!currentProject)return;
  $('graph-result').textContent='Querying…';
  const url=`/api/graph?project=${encodeURIComponent(currentProject)}&symbol=${encodeURIComponent(sym)}&relation=${rel}${to?'&to='+encodeURIComponent(to):''}`;
  const data=await api(url.slice(4));
  $('graph-result').textContent=JSON.stringify(data,null,2);
}

async function exportGraph(fmt){
  if(!currentProject)return;
  $('graph-export-info').textContent=`Exporting as ${fmt}…`;
  const data=await api('/graph_export?project='+encodeURIComponent(currentProject)+'&format='+fmt+'&max_nodes=5000');
  if(data.error){$('graph-export-info').textContent='Error: '+data.error;return;}
  const content=fmt==='graphml'?data.graphml:JSON.stringify(data,null,2);
  const blob=new Blob([content],{type:fmt==='graphml'?'application/xml':'application/json'});
  const a=document.createElement('a');a.href=URL.createObjectURL(blob);
  a.download=`knowledge_graph.${fmt==='graphml'?'graphml':'json'}`;a.click();
  const nc=data.stats?.node_count??(data.nodes?.length??'?');
  const ec=data.stats?.edge_count??(data.edges?.length??'?');
  $('graph-export-info').textContent=`Exported ${nc} nodes, ${ec} edges as ${fmt}.`;
}

/* ── Sigma.js graph renderer ─────────────────────────────────────────────────── */
let _sigmaInst=null,_sigmaFA2=null,_sigmaGraph=null;
const _PALETTE=['#7b61ff','#00c28e','#ffb800','#ff4060','#9b6dff','#00d4ff','#fb8f44','#6366f1','#10b981','#ec4899'];

function stopGraph(){
  if(_sigmaFA2){try{_sigmaFA2.stop();}catch(e){}  _sigmaFA2=null;}
  if(_sigmaInst){try{_sigmaInst.kill();}catch(e){}  _sigmaInst=null;}
  _sigmaGraph=null;
}

function switchGraphLayout(){
  if(!_sigmaGraph||!_sigmaInst)return;
  const layout=($('graph-layout')&&$('graph-layout').value)||'fa2';
  if(_sigmaFA2){try{_sigmaFA2.stop();}catch(e){} _sigmaFA2=null;}
  if(layout==='circular'&&window.circularLayout){
    window.circularLayout.assign(_sigmaGraph,{scale:200});
    _sigmaInst.refresh();
  }else if(layout==='fa2'&&window.FA2Layout){
    _sigmaFA2=new window.FA2Layout(_sigmaGraph,{settings:{gravity:1,scalingRatio:10,slowDown:8,barnesHutOptimize:_sigmaGraph.order>500}});
    _sigmaFA2.start();
    setTimeout(()=>{if(_sigmaFA2)_sigmaFA2.stop();},4000);
  }
}

async function visualizeFullGraph(maxNodes){
  if(!currentProject)return;
  stopGraph();
  $('graph-canvas-info').textContent='Loading graph data…';

  // Check Sigma available
  if(!window.Sigma||!window.Graph){
    $('graph-canvas-info').textContent='Sigma.js not loaded — check /static/sigma-graph.min.js';
    return;
  }

  let data;
  try{data=await api('/graph_export?project='+encodeURIComponent(currentProject)+'&format=json&max_nodes='+maxNodes);}
  catch(e){$('graph-canvas-info').textContent='Error: '+escHtml(e.message);return;}
  if(data.error){$('graph-canvas-info').textContent='Error: '+escHtml(data.error);return;}

  const nodes=data.nodes||[];const edges=data.edges||[];const communities=data.communities||[];
  if(!nodes.length){$('graph-canvas-info').textContent='No graph data. Build the project index first.';return;}

  // Build community color map
  const commColors={};
  communities.forEach((c,i)=>{commColors[c.id]=_PALETTE[i%_PALETTE.length];});

  // Build graphology graph
  const g=new window.Graph({multi:false,allowSelfLoops:false});
  nodes.forEach(n=>{
    const color=commColors[n.community_id]||'#484f58';
    const size=n.kind==='file'?3:n.kind==='function'||n.kind==='method'?5:4;
    g.addNode(n.id,{
      label:n.name,x:Math.random()*2-1,y:Math.random()*2-1,
      size,color,
      nodeType:n.kind,file:n.file||'',communityId:n.community_id,
    });
  });
  const edgeSet=new Set();
  edges.forEach(e=>{
    const k=e.from+'→'+e.to;
    if(!edgeSet.has(k)&&g.hasNode(e.from)&&g.hasNode(e.to)){
      edgeSet.add(k);
      g.addEdge(e.from,e.to,{color:'rgba(72,79,88,.4)',size:.5});
    }
  });

  // Legend
  const legHtml=communities.slice(0,10).map((c,i)=>
    `<div class="legend-item"><div class="legend-dot" style="background:${_PALETTE[i%_PALETTE.length]}"></div>${escHtml((c.title||'Community '+c.id).slice(0,22))}</div>`
  ).join('');
  $('graph-legend').innerHTML=legHtml;

  // Mount Sigma
  const container=$('sigma-container');
  container.innerHTML='';
  const isDark=document.documentElement.dataset.theme!=='light';
  _sigmaGraph=g;
  _sigmaInst=new window.Sigma(g,container,{
    renderEdgeLabels:false,
    labelRenderedSizeThreshold:6,
    defaultEdgeColor:'rgba(72,79,88,.35)',
    defaultNodeColor:'#7b61ff',
    labelColor:{color:isDark?'#8891b8':'#4a5280'},
    labelSize:10,
    nodeProgramClasses:{},
    stagePadding:30,
    zIndex:true,
  });

  // Tooltip on hover
  const tip=$('sigma-tooltip');
  _sigmaInst.on('enterNode',({node,event})=>{
    const attrs=g.getNodeAttributes(node);
    tip.innerHTML=`<strong style="color:var(--accent)">${escHtml(attrs.label)}</strong><br>`+
      `<span style="color:var(--text-3)">${escHtml(attrs.nodeType||'')} · degree ${g.degree(node)}</span><br>`+
      `<span style="color:var(--text-3);font-size:.71rem">${escHtml((attrs.file||'').split('/').slice(-2).join('/'))}</span>`;
    tip.style.display='block';
    tip.style.left=(event.original.clientX+14)+'px';
    tip.style.top=(event.original.clientY+10)+'px';
  });
  _sigmaInst.on('leaveNode',()=>{tip.style.display='none';});
  _sigmaInst.on('clickNode',({node})=>{
    const attrs=g.getNodeAttributes(node);
    $('graph-symbol').value=attrs.label;
  });

  // ForceAtlas2 layout via Web Worker
  if(window.FA2Layout){
    _sigmaFA2=new window.FA2Layout(g,{settings:{gravity:1,scalingRatio:10,slowDown:8,barnesHutOptimize:g.order>500,strongGravityMode:false}});
    _sigmaFA2.start();
    setTimeout(()=>{if(_sigmaFA2)_sigmaFA2.stop();},5000);
  }else if(window.circularLayout){
    window.circularLayout.assign(g,{scale:200});
    _sigmaInst.refresh();
  }

  $('graph-canvas-info').textContent=`${nodes.length} nodes · ${edges.length} edges · ${communities.length} communities${data.truncated?' (truncated)':''} — scroll to zoom · drag to pan · click node to inspect`;
}

/* ── Wiki ────────────────────────────────────────────────────────────────────── */
async function loadWikiList(){
  if(!currentProject)return;
  const data=await api('/wiki?project='+encodeURIComponent(currentProject));
  const pages=data.pages||[];
  $('wiki-page-list').innerHTML=pages.length
    ?pages.map(p=>`<li><a onclick="loadWikiPage('${escAttr(p)}')">${escHtml(p)}</a></li>`).join('')
    :'<li style="color:var(--text-3);font-size:.81rem">No wiki pages. Run build(action="wiki").</li>';
}

async function loadWikiPage(name){
  const data=await api('/wiki/page?project='+encodeURIComponent(currentProject)+'&name='+encodeURIComponent(name));
  $('wiki-content').innerHTML=simpleMarkdown(escHtml(data.content||''));
}

async function runWikiSearch(){
  const q=$('wiki-search-q').value.trim();const scope=$('wiki-scope').value;
  if(!q||!currentProject)return;
  const data=await api('/ask?project='+encodeURIComponent(currentProject)+'&q='+encodeURIComponent(q)+'&scope='+scope);
  const results=data.results||[];
  $('wiki-search-results').innerHTML=results.length
    ?results.map(r=>`<div class="result-item">
        <div class="path">${escHtml(r.path?.split('/').slice(-2).join('/')||'')}<span class="score">${(r.score||0).toFixed(3)}</span></div>
        <pre>${escHtml((r.content||'').slice(0,300))}</pre>
      </div>`).join('')
    :'<div style="color:var(--text-3);font-size:.81rem;padding:10px">No results.</div>';
}

/* ── Ask (dedicated) ─────────────────────────────────────────────────────────── */
async function runAsk(){
  const q=$('ask-q').value.trim();const scope=$('ask-scope').value;
  if(!q||!currentProject)return;
  $('ask-results').innerHTML='<div class="loader">Asking…</div>';
  try{
    const data=await api('/ask?project='+encodeURIComponent(currentProject)+'&q='+encodeURIComponent(q)+'&scope='+scope);
    const results=data.results||[];
    $('ask-results').innerHTML=results.length
      ?results.map(r=>`<div class="result-item">
          <div class="path">${escHtml(r.path?.split('/').slice(-2).join('/')||'')}<span class="score">${(r.score||0).toFixed(3)}</span></div>
          <div style="font-size:.81rem;line-height:1.6;color:var(--text-2);margin-top:4px">${simpleMarkdown(escHtml((r.content||'').slice(0,600)))}</div>
        </div>`).join('')
      :'<div style="color:var(--text-3);font-size:.81rem;padding:10px">No results.</div>';
  }catch(e){$('ask-results').innerHTML=`<div style="color:var(--red)">${escHtml(e.message)}</div>`;}
}

function loadAskSuggestions(){
  const suggestions=['How does authentication work?','What calls the main handler?','How is the database accessed?','What are the main entry points?','How does error handling work?','What is the overall architecture?'];
  $('ask-suggestions').innerHTML=suggestions.map(s=>
    `<button class="btn secondary" style="font-size:.77rem;padding:4px 10px" onclick="$('ask-q').value='${escAttr(s)}';runAsk()">${escHtml(s)}</button>`
  ).join('');
}

/* ── Search ──────────────────────────────────────────────────────────────────── */
async function runSearch(){
  const q=$('search-q').value.trim();const scope=$('search-scope').value;
  if(!q)return;
  const data=await api('/search?project='+encodeURIComponent(currentProject)+'&q='+encodeURIComponent(q)+'&scope='+scope);
  const results=data.results||[];
  $('search-results').innerHTML=results.length
    ?results.map(r=>`<div class="result-item">
        <div class="path">${escHtml(r.path?.split('/').slice(-3).join('/')||'')}:${r.start_line||0}-${r.end_line||0}
          <span class="score">${(r.score||0).toFixed(3)}</span></div>
        <pre>${escHtml((r.content||'').slice(0,400))}</pre>
      </div>`).join('')
    :'<div style="color:var(--text-3);font-size:.81rem;padding:10px">No results.</div>';
}

/* ── Health / Status ─────────────────────────────────────────────────────────── */
async function loadKBHealth(){
  if(!currentProject)return;
  try{
    const[kbh,aps]=await Promise.all([
      api('/kb_health?project='+encodeURIComponent(currentProject)),
      api('/auto_pipeline_status'),
    ]);
    const pct=kbh.enrichment_pct!=null?kbh.enrichment_pct.toFixed(0)+'%':'—';
    $('kb-health-grid').innerHTML=[
      {val:pct,lbl:'Enriched'},
      {val:(kbh.enriched_communities??'—')+'/'+(kbh.total_communities??'—'),lbl:'Communities'},
      {val:kbh.wiki_page_count??'—',lbl:'Wiki Pages'},
      {val:kbh.patterns_cached?'✓ cached':'✗ none',lbl:'Patterns'},
    ].map(s=>`<div class="stat-box"><div class="val" style="font-size:.88rem">${s.val}</div><div class="lbl">${s.lbl}</div></div>`).join('');
    // Per-level enrichment breakdown
    const byLevel=kbh.enrichment_by_level||{};
    const lvlKeys=Object.keys(byLevel).sort((a,b)=>+a-+b);
    if(lvlKeys.length>0&&$('kb-enrich-level-detail')){
      const pctRaw=kbh.enrichment_pct||0;
      $('kb-enrich-level-detail').innerHTML=`
        <div style="font-size:.74rem;color:var(--text-3);margin-bottom:6px;font-weight:700;text-transform:uppercase;letter-spacing:.08em">Per-level Enrichment</div>
        <div style="display:flex;flex-direction:column;gap:6px">
        ${lvlKeys.map(lvl=>{
          const d=byLevel[lvl];const lpct=d.pct||0;
          const c=lpct>=90?'var(--green)':lpct>=60?'var(--amber)':'var(--red)';
          return `<div>
            <div style="display:flex;justify-content:space-between;font-size:.77rem;margin-bottom:3px">
              <span style="color:var(--text-3)">Level ${lvl}</span>
              <span style="color:var(--text-2)">${d.enriched}/${d.total} <span style="color:${c};font-weight:700">${lpct.toFixed(0)}%</span></span>
            </div>
            <div style="height:5px;background:var(--surface-3);border-radius:3px;overflow:hidden">
              <div style="height:100%;width:${lpct}%;background:${c};transition:width .4s ease"></div>
            </div></div>`;
        }).join('')}
        </div>`;
    }
    const steps=(kbh.patterns_steps||[]).join(' → ');
    const cachedAt=kbh.patterns_cached_at?new Date(kbh.patterns_cached_at).toLocaleString():'—';
    const lastEv=kbh.last_pipeline_event;
    const lastRun=lastEv?`${lastEv.status} at ${new Date(lastEv.at).toLocaleString()}`:'not recorded';
    $('kb-health-detail').innerHTML=
      `<div style="margin-bottom:4px">Patterns steps: <span style="color:var(--accent)">${steps||'—'}</span></div>`+
      `<div style="margin-bottom:4px">Patterns cached at: <span style="color:var(--text-2)">${cachedAt}</span></div>`+
      `<div>Last pipeline: <span style="color:var(--text-2)">${escHtml(lastRun)}</span></div>`;
    const enabled=aps.enabled;const events=(aps.events||[]).slice(-5).reverse();
    $('pipeline-events-meta').textContent=`Auto-pipeline: ${enabled?'✓ enabled':'✗ disabled (OPENCODE_AUTO_PIPELINE=0)'}`;
    $('pipeline-events-list').innerHTML=events.length
      ?events.map(e=>{
        const color=e.status==='ok'?'var(--green)':e.status==='error'?'var(--red)':'var(--text-2)';
        const at=e.at?new Date(e.at).toLocaleTimeString():'';
        return `<div style="margin-bottom:4px;padding:4px 8px;background:var(--surface-2);border-radius:4px">
          <span style="color:${color}">${escHtml(e.status)}</span>
          <span style="color:var(--text-3)"> ${escHtml(e.project||'')} ${at}</span></div>`;
      }).join('')
      :'<div style="color:var(--text-3)">No events.</div>';
  }catch(e){$('kb-health-grid').innerHTML=`<div style="color:var(--text-3);font-size:.81rem">KB health unavailable: ${escHtml(e.message)}</div>`;}
}

let _enrichHierJobId=null,_enrichHierTimer=null;
async function triggerEnrichHierarchy(){
  if(!currentProject){toast('No project selected','error');return;}
  const btn=$('enrich-hier-btn');
  const statusEl=$('kb-enrich-job-status');
  if(btn)btn.disabled=true;
  if(statusEl){statusEl.style.display='block';statusEl.textContent='Starting enrichment…';}
  try{
    const r=await fetch('/api/enrich_hierarchy',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({project:currentProject})});
    const d=await r.json();
    if(d.error){toast('Enrich error: '+d.error,'error');if(btn)btn.disabled=false;return;}
    _enrichHierJobId=d.job_id;
    toast('Hierarchy enrichment started (job '+d.job_id.slice(0,8)+'…)','info');
    _pollEnrichJob();
  }catch(e){toast('Failed: '+e.message,'error');if(btn)btn.disabled=false;}
}
function _pollEnrichJob(){
  if(!_enrichHierJobId)return;
  clearTimeout(_enrichHierTimer);
  _enrichHierTimer=setTimeout(async()=>{
    try{
      const j=await fetch('/api/jobs/'+_enrichHierJobId).then(r=>r.json());
      const statusEl=$('kb-enrich-job-status');
      const running=j.status==='running'||j.status==='pending';
      const msg=j.result?.enriched!=null?` (${j.result.enriched} enriched)`:j.result?.error?` error: ${j.result.error}`:'';
      if(statusEl)statusEl.textContent=`Enrichment: ${j.status}${msg}`;
      if(running){_pollEnrichJob();}
      else{
        const btn=$('enrich-hier-btn');if(btn)btn.disabled=false;
        _enrichHierJobId=null;
        if(j.status==='done'){toast('Hierarchy enrichment complete!','success');loadKBHealth();}
        else{toast('Enrichment ended: '+j.status,'error');}
      }
    }catch(e){const statusEl=$('kb-enrich-job-status');if(statusEl)statusEl.textContent='Poll error: '+e.message;}
  },5000);
}

async function loadStatus(){
  const[health,metrics]=await Promise.all([fetch('/healthz').then(r=>r.json()),api('/metrics')]);
  $('daemon-metrics').innerHTML=[
    {val:metrics.connected_clients??health.connected_clients??'—',lbl:'Clients'},
    {val:health.active_watchers??'—',lbl:'Watchers'},
    {val:metrics.uptime_s!=null?metrics.uptime_s.toFixed(0)+'s':health.uptime_s!=null?health.uptime_s.toFixed(0)+'s':'—',lbl:'Uptime'},
  ].map(s=>`<div class="stat-box"><div class="val">${s.val}</div><div class="lbl">${s.lbl}</div></div>`).join('');
  const snap=metrics;
  if($('metrics-snapshot'))$('metrics-snapshot').textContent=
    `${snap.call_count??0} searches · p50=${snap.latency_ms?.p50??'—'}ms · p95=${snap.latency_ms?.p95??'—'}ms · 0-result=${snap.zero_result_pct!=null?snap.zero_result_pct.toFixed(1):'—'}%`;
  loadKBHealth();
  loadMetricsCharts();
  loadAlerts();
}

let _latencyChart=null, _zeroresultChart=null;
async function loadMetricsCharts(){
  const hours=($('metrics-hours')&&$('metrics-hours').value)||'24';
  let data;
  try{data=await api('/metrics/history?hours='+hours);}
  catch(e){console.warn('metrics/history unavailable',e);return;}
  if(!data||!data.timestamps||!data.timestamps.length){
    if($('metrics-snapshot'))$('metrics-snapshot').textContent='No search history yet.';
    return;
  }
  const labels=data.timestamps.map(ts=>{
    const d=new Date(ts*1000);
    return hours<=1?d.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'}):d.toLocaleDateString([],{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});
  });
  const chartOpts={responsive:true,animation:false,plugins:{legend:{display:true,labels:{color:'#8891b8',font:{size:11}}},tooltip:{mode:'index',intersect:false}},scales:{x:{ticks:{color:'#4e5880',font:{size:10},maxTicksLimit:8},grid:{color:'#1b1f32'}},y:{ticks:{color:'#4e5880',font:{size:10}},grid:{color:'#1b1f32'}}}};
  const canvasL=$('chart-latency');
  if(canvasL&&typeof Chart!=='undefined'){
    if(_latencyChart)_latencyChart.destroy();
    _latencyChart=new Chart(canvasL,{type:'line',data:{labels,datasets:[
      {label:'p50',data:data.latency_p50,borderColor:'#7b61ff',backgroundColor:'rgba(123,97,255,.1)',borderWidth:1.5,pointRadius:0,fill:true,tension:.3},
      {label:'p95',data:data.latency_p95,borderColor:'#ffb800',backgroundColor:'rgba(255,184,0,.08)',borderWidth:1.5,pointRadius:0,fill:false,tension:.3},
    ]},options:{...chartOpts,scales:{...chartOpts.scales,y:{...chartOpts.scales.y,title:{display:true,text:'ms',color:'#4e5880',font:{size:10}}}}}});
  }
  const canvasZ=$('chart-zeroresult');
  if(canvasZ&&typeof Chart!=='undefined'){
    if(_zeroresultChart)_zeroresultChart.destroy();
    _zeroresultChart=new Chart(canvasZ,{type:'line',data:{labels,datasets:[
      {label:'0-result %',data:data.zero_result_pct,borderColor:'#ff4060',backgroundColor:'rgba(255,64,96,.1)',borderWidth:1.5,pointRadius:0,fill:true,tension:.3},
    ]},options:{...chartOpts,scales:{...chartOpts.scales,y:{...chartOpts.scales.y,min:0,max:100,title:{display:true,text:'%',color:'#4e5880',font:{size:10}}}}}});
  }
}

async function loadAlerts(){
  let data;
  try{data=await api('/alerts');}
  catch(e){return;}
  const viols=data.violations||[];

  // ── Topbar alert badge + nav dot ──
  const badge=$('alert-badge');
  const navDot=$('nav-health-alert');
  if(viols.length){
    $('alert-badge-count').textContent=viols.length;
    badge.classList.add('visible');
    if(navDot) navDot.style.display='block';
  } else {
    badge.classList.remove('visible');
    if(navDot) navDot.style.display='none';
  }

  const violEl=$('alerts-violations');
  const rulesEl=$('alerts-rules-list');
  if(violEl){
    violEl.innerHTML=viols.length
      ?viols.map(v=>`<div style="background:var(--red-bg);border:1px solid var(--red);border-radius:4px;padding:6px 10px;margin-bottom:6px;font-size:.77rem;color:var(--red)">⚠ ${escHtml(v.name)}: ${escHtml(v.message||v.metric)}</div>`).join('')
      :'<div style="color:var(--green);font-size:.77rem;padding:4px 0">✓ No active violations</div>';
  }
  if(rulesEl){
    const rules=data.rules||[];
    rulesEl.innerHTML=rules.length
      ?'<div style="color:var(--text-3);margin-bottom:4px">Active rules:</div>'+rules.map(r=>`<div style="padding:3px 0;display:flex;align-items:center;gap:8px"><span style="color:${r.enabled?'var(--green)':'var(--text-3)'}">${r.enabled?'●':'○'}</span><span style="color:var(--text-2)">${escHtml(r.name)}</span><span style="color:var(--text-3);font-size:.74rem">${escHtml(r.metric)} ${escHtml(r.op)} ${r.threshold}</span></div>`).join('')
      :'<div style="color:var(--text-3);font-size:.77rem">No alert rules configured.</div>';
  }
}

/* ── Verify ──────────────────────────────────────────────────────────────────── */
async function loadVerify(){
  try{
    const data=await api('/verify_status');
    const verdict=data.verdict||'unknown';
    const badgeCls=verdict==='GO'?'go':verdict==='NO-GO'?'nogo':verdict==='WARNINGS'?'warn-lg':'none';
    $('verify-badge').innerHTML=`<span class="badge ${badgeCls}">${escHtml(verdict)}</span>`;
    const last=data.last_run;
    if(last){
      const ts=last.timestamp||last.ts||'';
      const dur=(last.duration_s||0).toFixed(0);
      $('verify-meta').textContent=`Last run: ${ts?new Date(ts).toLocaleString():ts} · ${last.passed||0} passed · ${last.failed||0} failed · ${dur}s`;
    }
    // Category grid
    const cats=data.categories||{};
    const catHtml=Object.entries(cats).map(([cat,v])=>{
      const pass=v.passed??v.pass??0;const fail=v.failed??v.fail??0;const ok=fail===0;
      return `<div class="verify-cat ${ok?'pass':''}">
        <span class="vc-icon">${ok?'✅':'❌'}</span>
        <span class="vc-name">${escHtml(cat.replace(/_/g,' '))}</span>
        <span class="vc-count">${pass}/${pass+fail}</span></div>`;
    }).join('');
    $('verify-category-grid').innerHTML=catHtml||'<div class="loader">No category data</div>';
    // Sparkline
    const history=data.history||[];
    if(history.length>0)drawSparkline(history,'verify-sparkline');
    // Failures
    const failures=data.failures||[];
    if(failures.length>0){
      $('verify-failures-card').style.display='';
      $('verify-failures').innerHTML=failures.map(f=>
        `<div style="padding:5px 0;border-bottom:1px solid var(--surface-2);font-size:.81rem">
          <span class="badge ${f.severity==='P0'?'err':'warn'}">${escHtml(f.severity||'?')}</span>
          <strong style="color:var(--text);margin-left:6px">${escHtml(f.name||'')}</strong>
          <span style="color:var(--text-3);margin-left:6px">${escHtml(f.message||'')}</span></div>`
      ).join('');
    }else $('verify-failures-card').style.display='none';
  }catch(e){
    $('verify-badge').innerHTML='<span class="badge none">unavailable</span>';
    $('verify-meta').textContent='Could not load verification status: '+e.message;
  }
}

async function runVerification(){
  const btn=$('verify-run-btn');btn.disabled=true;btn.textContent='⏳ Running…';
  $('verify-job-status').textContent='Starting verification…';
  try{
    const resp=await fetch('/api/run_prerelease',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({project:currentProject})});
    const data=await resp.json();
    if(data.error){$('verify-job-status').textContent='Error: '+data.error;btn.disabled=false;btn.textContent='▶ Run Verification';return;}
    const taskId=data.task_id;let n=0;
    const poll=async()=>{n++;if(n>120){btn.disabled=false;btn.textContent='▶ Run Verification';return;}
      const st=await fetch('/api/prerelease_poll?id='+taskId).then(r=>r.json()).catch(()=>null);
      if(!st||st.status==='running'){$('verify-job-status').textContent=`Running… (${n*3}s)`;setTimeout(poll,3000);return;}
      btn.disabled=false;btn.textContent='▶ Run Verification';$('verify-job-status').textContent='Done.';
      await loadVerify();
    };poll();
  }catch(e){$('verify-job-status').textContent='Failed: '+e.message;btn.disabled=false;btn.textContent='▶ Run Verification';}
}

async function triggerAutoFix(){
  const btn=$('verify-fix-btn');btn.disabled=true;btn.textContent='⏳ Fixing…';
  try{
    const resp=await fetch('/api/auto_fix_trigger',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({project:currentProject})});
    const data=await resp.json();
    if(data.error){toast('Auto-fix failed: '+data.error,'error');}else toast('Auto-fix started (id: '+data.task_id+')','info');
  }catch(e){toast('Auto-fix error: '+e.message,'error');}
  btn.disabled=false;btn.textContent='🔧 Auto-Fix';
}

/* ── Sparkline ───────────────────────────────────────────────────────────────── */
function drawSparkline(history,containerId){
  const el=$(containerId);if(!el)return;
  const W=el.offsetWidth||400,H=48;
  const vals=history.map(h=>h.passed||0);
  const totals=history.map(h=>(h.passed||0)+(h.failed||0));
  const maxTotal=Math.max(...totals,1);const n=vals.length;
  const px=(i,v)=>[(i/Math.max(n-1,1))*(W-10)+5, H-4-((v/maxTotal)*(H-12))];
  const passLine=vals.map((v,i)=>px(i,v).join(',')).join(' ');
  const totalLine=totals.map((v,i)=>px(i,v).join(',')).join(' ');
  const dots=vals.map((v,i)=>{const[x,y]=px(i,v);const ok=totals[i]>0&&v===totals[i];return `<circle cx="${x}" cy="${y}" r="3" fill="${ok?'#00c28e':'#ffb800'}"/>`}).join('');
  el.innerHTML=`<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" style="width:100%;overflow:visible"><polyline points="${totalLine}" fill="none" stroke="#222844" stroke-width="1.5"/><polyline points="${passLine}" fill="none" stroke="#00c28e" stroke-width="2"/>${dots}</svg>`;
}

/* ── Integrations ────────────────────────────────────────────────────────────── */
async function loadIntegrations(){
  $('integrations-cards').innerHTML='<div class="loader">Checking integrations…</div>';
  try{
    const data=await api('/integrations_status');
    const results=data.results||[];
    if(!results.length){$('integrations-cards').innerHTML='<div class="loader">No integration data</div>';return;}
    const icons={codex:'🤖','claude-code':'💻',opencode:'⚡',hermes:'📬',bash_aliases:'🔧',systemd:'⚙'};
    const okCount=results.filter(r=>r.status==='configured'||r.status==='already_ok').length;
    $('integrations-meta').textContent=`${okCount}/${results.length} integrations configured`;
    $('integrations-cards').innerHTML=results.map(r=>{
      const ok=r.status==='configured'||r.status==='already_ok';
      const err=r.status==='missing'||r.status==='error';
      const cls=ok?'ok':err?'err':'';
      const stCls=ok?'ok':err?'err':'warn';
      const stTxt=ok?'✓ configured':err?'✗ '+r.status:'⚠ '+r.status;
      const icon=icons[r.name]||'🔌';
      return `<div class="integ-card ${cls}">
        <div class="integ-title">${icon} ${escHtml(r.name||'')}</div>
        <div class="integ-status ${stCls}">${stTxt}</div>
        ${r.message?`<div style="font-size:.71rem;color:var(--text-3);margin-top:4px">${escHtml(r.message.slice(0,80))}</div>`:''}
      </div>`;
    }).join('');
  }catch(e){$('integrations-cards').innerHTML=`<div style="color:var(--text-3);font-size:.81rem">Failed: ${escHtml(e.message)}</div>`;}
}

/* ── Release ─────────────────────────────────────────────────────────────────── */
async function loadRelease(){
  try{
    const data=await fetch('/api/prerelease_status').then(r=>r.ok?r.json():null);
    if(data)renderReleaseReport(data);
    else $('release-verdict').innerHTML='<div style="color:var(--text-3);font-size:.84rem">No pre-release report yet. Click ▶ Run Pre-Release Check to generate one.</div>';
  }catch(e){$('release-verdict').innerHTML='<div style="color:var(--text-3);font-size:.84rem">No report available.</div>';}
}

async function runPrerelease(){
  $('release-spinner').style.display='inline';$('release-stages').innerHTML='';
  $('release-verdict').innerHTML='<div style="color:var(--text-3)">Running pre-release checks…</div>';
  try{
    const resp=await fetch('/api/run_prerelease',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({project:currentProject})});
    const data=await resp.json();
    if(data.error){$('release-verdict').innerHTML=`<div style="color:var(--red)">Error: ${escHtml(data.error)}</div>`;$('release-spinner').style.display='none';return;}
    const taskId=data.task_id;let attempts=0;
    const poll=async()=>{
      attempts++;if(attempts>120){$('release-spinner').style.display='none';return;}
      const st=await fetch('/api/prerelease_poll?id='+taskId).then(r=>r.json()).catch(()=>null);
      if(!st||st.status==='running'){setTimeout(poll,3000);return;}
      $('release-spinner').style.display='none';
      const report=await fetch('/api/prerelease_status').then(r=>r.ok?r.json():null);
      if(report)renderReleaseReport(report);
    };poll();
  }catch(e){$('release-verdict').innerHTML=`<div style="color:var(--red)">Failed: ${escHtml(e.message)}</div>`;$('release-spinner').style.display='none';}
}

function renderReleaseReport(data){
  const verdict=data.verdict||'UNKNOWN';
  const vColor=verdict==='GO'?'var(--green)':verdict==='NO-GO'?'var(--red)':'var(--amber)';
  const vIcon=verdict==='GO'?'🟢':verdict==='NO-GO'?'🔴':'🟡';
  $('release-verdict').innerHTML=`
    <div style="display:flex;align-items:center;gap:12px;padding:10px;background:var(--surface-2);border-radius:8px">
      <span style="font-size:2rem">${vIcon}</span>
      <div><div style="font-size:1.2rem;font-weight:700;color:${vColor}">${verdict}</div>
        <div style="font-size:.74rem;color:var(--text-3)">${data.timestamp||''} · ${(data.total_duration_s||0).toFixed(1)}s</div></div>
    </div>`;
  const sIcon={pass:'✅',fail:'🔴',warn:'🟡',skip:'⏭️'};
  const rows=(data.stages||[]).map(s=>`<tr>
    <td>${sIcon[s.status]||'?'} ${escHtml(s.stage)}</td>
    <td><span class="badge ${s.status==='pass'?'ok':s.status==='fail'?'err':'none'}">${s.status}</span></td>
    <td style="color:var(--text-2)">${s.duration_s.toFixed(1)}s</td>
    <td style="font-size:.74rem;color:var(--text-3)">${escHtml((s.message||'').slice(0,80))}</td>
  </tr>`).join('');
  $('release-stages').innerHTML=`<table style="margin-top:10px"><thead><tr><th>Stage</th><th>Status</th><th>Time</th><th>Message</th></tr></thead><tbody>${rows}</tbody></table>`;
  const shots=(data.screenshots||[]);
  if(shots.length>0){
    $('release-screenshots-card').style.display='';
    $('release-screenshots').innerHTML=shots.map(s=>`<div style="text-align:center;font-size:.7rem;color:var(--text-3)"><div style="background:var(--surface-2);padding:4px;border-radius:4px;border:1px solid var(--border)">${escHtml(s.split('/').pop())}</div></div>`).join('');
  }
  const anomalies=(data.anomalies||[]);
  if(anomalies.length>0){
    $('release-anomalies-card').style.display='';
    $('release-anomalies').innerHTML=anomalies.map(a=>
      `<div style="padding:4px 0;border-bottom:1px solid var(--surface-2)">
        <span class="badge ${a.severity==='P0'?'err':'warn'}">${escHtml(a.severity||'?')}</span>
        <strong style="margin-left:6px">${escHtml(a.scenario||'')}</strong>: ${escHtml(a.message||'')}
      </div>`
    ).join('');
  }
}

/* ── QA Gate ─────────────────────────────────────────────────────────────────── */
async function loadQaGate(){
  try{
    const data=await fetch('/api/qa_status').then(r=>r.ok?r.json():null);
    if(data&&data.verdict)renderQaReport(data);
    else $('qa-verdict').innerHTML='<div style="color:var(--text-3);font-size:.84rem">No QA report yet. Click ▶ Run Full QA Gate to generate one.</div>';
  }catch(e){$('qa-verdict').innerHTML='<div style="color:var(--text-3);font-size:.84rem">No report available.</div>';}
}

async function runQaGate(){
  $('qa-spinner').style.display='inline';$('qa-pillars').innerHTML='';
  $('qa-verdict').innerHTML='<div style="color:var(--text-3)">Running QA gate (8 pillars)…</div>';
  try{
    const resp=await fetch('/api/run_qa',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({project:currentProject})});
    const data=await resp.json();
    if(data.error){$('qa-verdict').innerHTML=`<div style="color:var(--red)">Error: ${escHtml(data.error)}</div>`;$('qa-spinner').style.display='none';return;}
    const taskId=data.task_id;let attempts=0;
    const poll=async()=>{
      attempts++;if(attempts>200){$('qa-spinner').style.display='none';return;}
      const st=await fetch('/api/qa_poll?id='+taskId).then(r=>r.json()).catch(()=>null);
      if(!st||st.status==='running'){setTimeout(poll,4000);return;}
      $('qa-spinner').style.display='none';
      const report=await fetch('/api/qa_status').then(r=>r.ok?r.json():null);
      if(report)renderQaReport(report);
    };poll();
  }catch(e){$('qa-verdict').innerHTML=`<div style="color:var(--red)">Failed: ${escHtml(e.message)}</div>`;$('qa-spinner').style.display='none';}
}

function renderQaReport(data){
  const v=data.verdict||'UNKNOWN';
  const isGo=v.includes('GO')&&!v.includes('NO');
  const isNoGo=v.includes('NO-GO');
  const vColor=isGo?'var(--green)':isNoGo?'var(--red)':'var(--amber)';
  const vIcon=isGo?'🟢':isNoGo?'🔴':'🟡';
  $('qa-verdict').innerHTML=`
    <div style="display:flex;align-items:center;gap:12px;padding:10px;background:var(--surface-2);border-radius:8px">
      <span style="font-size:2rem">${vIcon}</span>
      <div>
        <div style="font-size:1.2rem;font-weight:700;color:${vColor}">${escHtml(v)}</div>
        <div style="font-size:.74rem;color:var(--text-3)">${escHtml(data.timestamp||'')} · ${(data.total_s||0).toFixed(0)}s · P0=${data.p0_count||0} · P1=${data.p1_count||0} · Healed=${data.fixes_applied||0}</div>
      </div>
    </div>`;
  const sIcon={pass:'✅',fail:'❌',warn:'⚠️',skip:'⏭️'};
  const rows=(data.pillars||[]).map(p=>{
    const tot=p.checks.length;const ok=(p.checks||[]).filter(c=>c.status==='pass').length;
    return `<tr>
      <td>${sIcon[p.status]||'?'} ${escHtml(p.label||p.name)}</td>
      <td><span class="badge ${p.status==='pass'?'ok':p.status==='fail'?'err':'warn'}">${p.status}</span></td>
      <td>${ok}/${tot}</td>
      <td>${(p.p0_count||0)>0?`<span class="badge err">P0=${p.p0_count}</span>`:''}</td>
      <td style="color:var(--text-2)">${p.duration_s||0}s</td>
    </tr>`;
  }).join('');
  $('qa-pillars').innerHTML=`<table style="margin-top:10px"><thead><tr><th>Pillar</th><th>Status</th><th>Checks</th><th>P0</th><th>Time</th></tr></thead><tbody>${rows}</tbody></table>`;
  const failures=(data.pillars||[]).flatMap(p=>(p.checks||[]).filter(c=>c.status==='fail'||c.status==='warn'));
  if(failures.length>0){
    $('qa-failures-card').style.display='';
    $('qa-failures').innerHTML=failures.map(f=>
      `<div style="padding:4px 0;border-bottom:1px solid var(--surface-2)">
        <span class="badge ${f.severity==='P0'?'err':f.severity==='P1'?'warn':'none'}">${escHtml(f.severity)}</span>
        <strong style="margin-left:6px">${escHtml(f.name)}</strong>: ${escHtml((f.message||'').slice(0,120))}
      </div>`
    ).join('');
  }
}

/* ── Architecture Map ──────────────────────────────────────────────────────────── */
async function loadArchMap(){
  if(!currentProject)return;
  $('arch-map-content').innerHTML='<div class="loader">Loading…</div>';
  const mode=$('arch-map-level').value;
  try{
    const ep=mode==='all'?'/overview?project='+encodeURIComponent(currentProject)+'&what=hierarchy':'/overview?project='+encodeURIComponent(currentProject)+'&what=architecture_domains';
    const data=await api(ep);
    if(data.error){$('arch-map-content').innerHTML=`<div style="color:var(--text-3)">${escHtml(data.error)}</div>`;return;}
    if(mode==='all'){
      const levels=data.levels||{};const maxLvl=data.max_level||1;
      $('arch-map-levels').textContent=`${maxLvl} levels`;
      let html='';
      for(let lvl=maxLvl;lvl>=1;lvl--){
        const comms=(levels[String(lvl)]||[]).slice(0,20);if(!comms.length)continue;
        html+=`<div style="margin-bottom:12px"><div class="card-title" style="margin-bottom:6px">Level ${lvl} — ${comms.length} domains</div>`;
        html+=comms.map(c=>`<div class="community-card" style="margin-bottom:5px;padding:8px 10px">
          <div style="display:flex;justify-content:space-between">
            <strong style="color:var(--accent);font-size:.82rem">${escHtml(c.title||'Domain '+c.id)}</strong>
            <span style="font-size:.71rem;color:var(--text-3)">${c.node_count} nodes</span>
          </div>${c.summary?`<p style="font-size:.76rem;color:var(--text-2);margin-top:3px">${escHtml(c.summary.slice(0,180))}</p>`:''}</div>`).join('');
        html+='</div>';
      }
      $('arch-map-content').innerHTML=html||'<div class="loader">No hierarchy. Run Build Hierarchy.</div>';
    }else{
      const domains=data.architecture_domains||[];
      $('arch-map-levels').textContent=`${data.hierarchy_levels||1} levels, ${domains.length} domains`;
      $('arch-map-content').innerHTML=domains.length?domains.map(d=>`<div class="community-card" style="margin-bottom:8px;padding:12px">
        <div style="display:flex;justify-content:space-between"><strong style="color:var(--accent);font-size:.83rem">${escHtml(d.title||'Domain '+d.id)}</strong><span class="badge info">L${d.level}</span></div>
        ${d.summary?`<p style="font-size:.77rem;color:var(--text-2);margin-top:5px;line-height:1.5">${escHtml(d.summary.slice(0,240))}</p>`:''}
        <div style="font-size:.71rem;color:var(--text-3);margin-top:3px">${d.node_count} nodes</div></div>`).join('')
        :'<div class="loader">No hierarchy built yet. Click Build Hierarchy.</div>';
    }
  }catch(e){$('arch-map-content').innerHTML=`<div style="color:var(--red);font-size:.81rem">Error: ${escHtml(e.message)}</div>`;}
}

async function buildHierarchy(){
  toast('Building hierarchy in background…','info');
  try{
    const r=await fetch('/api/build_hierarchy',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({project:currentProject})});
    const d=await r.json();
    if(d.error)toast('Hierarchy error: '+d.error,'error');
    else{toast(`Hierarchy: ${d.levels_built||0} levels built`,'success');await loadArchMap();}
  }catch(e){toast('Build failed: '+e.message,'error');}
}

/* ── Service Mesh ──────────────────────────────────────────────────────────────── */
let _meshData=null;
async function loadServiceMesh(){
  if(!currentProject)return;
  $('service-mesh-content').innerHTML='<div class="loader">Scanning services…</div>';
  $('service-mesh-description').style.display='none';
  $('mesh-graph-wrap').style.display='none';
  try{
    const data=await api('/service_mesh?project='+encodeURIComponent(currentProject));
    if(data.error){$('service-mesh-content').innerHTML=`<div style="color:var(--text-3)">${escHtml(data.error)}</div>`;return;}
    if(data.description){$('service-mesh-description').textContent=data.description;$('service-mesh-description').style.display='block';}
    _meshData=data;
    renderMeshView();
  }catch(e){$('service-mesh-content').innerHTML=`<div style="color:var(--red);font-size:.81rem">Error: ${escHtml(e.message)}</div>`;}
}

function renderMeshView(){
  if(!_meshData)return;
  const view=($('mesh-view')&&$('mesh-view').value)||'list';
  const data=_meshData;
  const services=data.services||[];const edges=data.edges||[];
  const pc={'grpc':'#7b61ff','http':'#00c28e','message_queue':'#ffb800','database':'#9b6dff'};
  if(view==='graph'){
    $('service-mesh-content').style.display='none';
    $('mesh-graph-wrap').style.display='block';
    // Build graph nodes/edges from service mesh
    const palette=Object.values(pc);
    const nodes=services.map((s,i)=>({
      id:s.name,name:s.name,kind:'service',file:'',community_id:i,
    }));
    const graphEdges=edges.map(e=>({from:e.from,to:e.to,protocol:e.protocol}));
    visualizeMeshCanvas(nodes,graphEdges,palette);
  }else{
    $('service-mesh-content').style.display='';
    $('mesh-graph-wrap').style.display='none';
    const pcv={'grpc':'var(--accent)','http':'var(--green)','message_queue':'var(--amber)','database':'var(--purple)'};
    const sHtml=services.map(s=>`<div class="integ-card ${(s.protocols||[]).length?'ok':''}">
      <div class="integ-title">📦 ${escHtml(s.name)}</div>
      <div style="display:flex;gap:3px;flex-wrap:wrap;margin-top:3px">
        ${(s.protocols||[]).map(p=>`<span class="badge" style="font-size:.69rem;color:${pcv[p]||'var(--text-2)'}">${p}</span>`).join('')}
      </div></div>`).join('');
    const eHtml=edges.length?`<div style="margin-top:12px"><div class="card-title" style="margin-bottom:6px">Connections (${edges.length})</div>
      <table><thead><tr><th>From</th><th>Protocol</th><th>To</th></tr></thead><tbody>
      ${edges.slice(0,30).map(e=>`<tr><td>${escHtml(e.from)}</td><td style="color:${pcv[e.protocol]||'var(--text-2)'}">${escHtml(e.protocol)}</td><td>${escHtml(e.to)}</td></tr>`).join('')}
      </tbody></table></div>`:'';
    $('service-mesh-content').innerHTML=`<div class="integrations-grid" style="margin-bottom:10px">${sHtml}</div>${eHtml}`;
  }
}

let _meshSigma=null,_meshSigmaFA2=null,_meshSigmaGraph=null;
function visualizeMeshCanvas(nodes,edges,palette){
  if(_meshSigmaFA2){try{_meshSigmaFA2.stop();}catch(e){} _meshSigmaFA2=null;}
  if(_meshSigma){try{_meshSigma.kill();}catch(e){} _meshSigma=null;}
  _meshSigmaGraph=null;
  const container=$('mesh-sigma-container');
  if(!container)return;
  if(!nodes.length){$('mesh-canvas-info').textContent='No services detected.';return;}
  if(!window.Sigma||!window.Graph){$('mesh-canvas-info').textContent='Graph renderer not loaded.';return;}
  const PROTO_COLOR={grpc:'#7b61ff',http:'#00c28e',message_queue:'#ffb800',database:'#9b6dff'};
  const g=new window.Graph({multi:true,type:'directed'});
  nodes.forEach((n,i)=>{
    g.addNode(n.id,{label:n.name,size:12,color:palette[i%palette.length],x:Math.random(),y:Math.random()});
  });
  edges.forEach((e,i)=>{
    if(g.hasNode(e.from)&&g.hasNode(e.to)){
      try{g.addEdge(e.from,e.to,{color:PROTO_COLOR[e.protocol]||'#888',size:1.5,label:e.protocol||''});}catch(_){}
    }
  });
  _meshSigmaGraph=g;
  _meshSigma=new window.Sigma(g,container,{
    renderEdgeLabels:true,
    labelFont:'monospace',labelSize:11,
    defaultEdgeType:'arrow',
    labelColor:{color:'#c8d0f0'},
    defaultNodeColor:'#7b61ff',
    backgroundColor:'#0b0e1a',
    stagePadding:30,
  });
  if(window.FA2Layout){
    _meshSigmaFA2=new window.FA2Layout(g,{settings:{gravity:1,scalingRatio:6,slowDown:10}});
    _meshSigmaFA2.start();
    setTimeout(()=>{if(_meshSigmaFA2)_meshSigmaFA2.stop();},3000);
  }else if(window.circularLayout){
    window.circularLayout.assign(g,{scale:150});
    _meshSigma.refresh();
  }
  $('mesh-canvas-info').textContent=`${nodes.length} services · ${edges.length} connections`;
}

/* ── Federation Map ──────────────────────────────────────────────────────────────── */
let _fedSigma=null,_fedSigmaFA2=null;
async function loadFedMap(){
  if(!currentProject)return;
  $('fed-canvas-info').textContent='Loading…';
  let data;
  try{data=await api('/federation?project='+encodeURIComponent(currentProject)+'&action=list');}
  catch(e){$('fed-canvas-info').textContent='Error: '+escHtml(e.message);return;}
  const members=data.members||data.projects||[];
  if(!members.length){$('fed-canvas-info').textContent='No federation members found for this project.';return;}
  // Render member list
  $('fed-members-list').innerHTML=`<div class="card-title" style="margin-bottom:6px">Members (${members.length})</div>`+
    members.map(m=>`<div style="padding:4px 0;border-bottom:1px solid var(--surface-2);display:flex;align-items:center;gap:8px">
      <span style="color:var(--accent);font-size:.78rem">📁</span>
      <span style="color:var(--text-2);font-size:.8rem">${escHtml(m.path||m.name||m)}</span>
      <span style="color:var(--text-3);font-size:.74rem;margin-left:auto">${escHtml(m.status||'')}</span>
    </div>`).join('');
  // Sigma.js: root node in center, members around it (circular layout)
  if(_fedSigmaFA2){try{_fedSigmaFA2.stop();}catch(e){} _fedSigmaFA2=null;}
  if(_fedSigma){try{_fedSigma.kill();}catch(e){} _fedSigma=null;}
  const container=$('fed-sigma-container');
  if(!container||!window.Sigma||!window.Graph){$('fed-canvas-info').textContent=`${members.length} members`;return;}
  const rootName=currentProject.split('/').pop();
  const palette=['#7b61ff','#00c28e','#ffb800','#ff4060','#9b6dff','#00d4ff','#fb8f44','#6366f1','#10b981','#ec4899'];
  const g=new window.Graph({type:'undirected'});
  const angle=2*Math.PI/(members.length||1);
  g.addNode('__root__',{label:rootName,size:18,color:'#7b61ff',x:0,y:0});
  members.forEach((m,i)=>{
    const name=(m.path||m.name||m).split('/').pop();
    const id=m.path||m.name||m;
    const rad=1.0;
    g.addNode(id,{label:name,size:10,color:palette[(i+1)%palette.length],
      x:rad*Math.cos(i*angle-Math.PI/2),y:rad*Math.sin(i*angle-Math.PI/2)});
    try{g.addEdge('__root__',id,{color:'rgba(123,97,255,.4)',size:1});}catch(_){}
  });
  _fedSigma=new window.Sigma(g,container,{
    labelFont:'monospace',labelSize:11,
    labelColor:{color:'#c8d0f0'},
    defaultNodeColor:'#7b61ff',
    backgroundColor:'#0b0e1a',
    stagePadding:30,
  });
  $('fed-canvas-info').textContent=`${members.length} members`;
}

/* ── Impact Analysis ────────────────────────────────────────────────────────────── */
async function runImpactAnalysis(){
  const sym=$('impact-symbol').value.trim();if(!sym||!currentProject)return;
  $('impact-result').innerHTML='<div class="loader">Analyzing…</div>';
  try{
    const data=await api('/impact_narrative?project='+encodeURIComponent(currentProject)+'&symbol='+encodeURIComponent(sym));
    if(data.error){$('impact-result').innerHTML=`<div style="color:var(--text-3)">${escHtml(data.error)}</div>`;return;}
    const rc=data.risk==='high'?'var(--red)':data.risk==='medium'?'var(--amber)':'var(--green)';
    $('impact-result').innerHTML=`
      <div style="display:flex;align-items:start;gap:12px;margin-bottom:12px;padding:10px;background:var(--surface-2);border-radius:var(--radius)">
        <div style="font-size:1.3rem;font-weight:700;color:${rc};white-space:nowrap">${(data.risk||'?').toUpperCase()} RISK</div>
        <div><div style="font-size:.82rem;color:var(--text);line-height:1.5">${escHtml(data.summary||'')}</div>
        ${data.action?`<div style="font-size:.78rem;color:var(--accent);margin-top:4px">→ ${escHtml(data.action)}</div>`:''}</div>
      </div>
      <div class="stat-grid" style="margin-bottom:10px">
        <div class="stat-box"><div class="val">${data.impact_count||0}</div><div class="lbl">Callers</div></div>
        <div class="stat-box"><div class="val">${(data.affected_domains||[]).length}</div><div class="lbl">Domains</div></div>
      </div>
      ${(data.affected_domains||[]).length?`<div style="margin-bottom:10px"><div class="card-title" style="margin-bottom:6px">Affected Domains</div>${data.affected_domains.map(d=>`<span class="badge warn" style="margin:2px 4px 2px 0">${escHtml(d)}</span>`).join('')}</div>`:''}
      ${(data.callers||[]).length?`<div><div class="card-title" style="margin-bottom:6px">Top Callers (first 10)</div>${data.callers.slice(0,10).map(c=>`<div style="font-size:.78rem;color:var(--text-2);padding:2px 0">${escHtml(c.qualified_name||c.name||'')} <span style="color:var(--text-3)">${escHtml((c.file||'').split('/').slice(-1)[0])}</span></div>`).join('')}</div>`:''}`;
  }catch(e){$('impact-result').innerHTML=`<div style="color:var(--red);font-size:.81rem">Error: ${escHtml(e.message)}</div>`;}
}

/* ── Semantic Trace ──────────────────────────────────────────────────────────────── */
async function runSemanticTrace(){
  const from=$('trace-from').value.trim();const to=$('trace-to').value.trim();
  if(!from||!to||!currentProject)return;
  $('trace-result').innerHTML='<div class="loader">Tracing flow…</div>';
  try{
    const data=await api('/semantic_trace?project='+encodeURIComponent(currentProject)+'&from='+encodeURIComponent(from)+'&to='+encodeURIComponent(to));
    if(data.error){$('trace-result').innerHTML=`<div style="color:var(--text-3)">${escHtml(data.error)}</div>`;return;}
    const path=data.path||[];
    $('trace-result').innerHTML=`
      <div style="padding:10px;background:var(--surface-2);border-radius:var(--radius);margin-bottom:10px;font-size:.83rem;line-height:1.6;color:var(--text)">${escHtml(data.narrative||'')}</div>
      <div class="stat-grid" style="margin-bottom:10px">
        <div class="stat-box"><div class="val">${data.hops||0}</div><div class="lbl">Hops</div></div>
        <div class="stat-box"><div class="val">${data.found?'✓':'✗'}</div><div class="lbl">Direct Path</div></div>
      </div>
      ${path.length?`<div class="card-title" style="margin-bottom:6px">Call Chain</div>
        <div style="display:flex;flex-direction:column;gap:2px">
          ${path.slice(0,15).map((n,i)=>`<div style="display:flex;align-items:center;gap:6px;font-size:.78rem;padding:2px 0${i<path.length-1?';border-bottom:1px solid var(--surface-2)':''}">
            <span style="color:var(--text-3);min-width:18px">${i+1}.</span>
            <span style="color:var(--accent)">${escHtml(n.qualified_name||n.name||'?')}</span>
            <span style="color:var(--text-3)">(${escHtml(n.kind||'')})</span>
            <span style="color:var(--text-3);font-size:.71rem">${escHtml((n.file||'').split('/').slice(-1)[0])}</span>
          </div>`).join('')}
        </div>`:''}`;
  }catch(e){$('trace-result').innerHTML=`<div style="color:var(--red);font-size:.81rem">Error: ${escHtml(e.message)}</div>`;}
}

/* ── Feature Trace ───────────────────────────────────────────────────────────── */
async function runFeatureTrace(){
  const q=$('feature-q').value.trim();
  if(!q||!currentProject)return;
  $('feature-result').innerHTML='<div class="loader">Tracing feature…</div>';
  try{
    const data=await api('/feature?project='+encodeURIComponent(currentProject)+'&q='+encodeURIComponent(q));
    if(data.error){$('feature-result').innerHTML=`<div style="color:var(--text-3)">${escHtml(data.error)}</div>`;return;}
    const eps=(data.entry_points||[]);
    const chain=(data.call_chain||[]);
    const services=(data.involved_services||[]);
    const decisions=(data.key_design_decisions||[]);
    $('feature-result').innerHTML=`
      ${data.algorithm?`<div class="card" style="margin:0 0 12px;padding:14px 16px">
        <div class="card-title" style="margin-bottom:6px;font-size:.83rem">Algorithm Overview</div>
        <div style="font-size:.82rem;line-height:1.7;color:var(--text-2)">${simpleMarkdown(escHtml(data.algorithm))}</div>
      </div>`:''}
      ${data.design_rationale?`<div class="card" style="margin:0 0 12px;padding:14px 16px;border-left:3px solid var(--accent)">
        <div class="card-title" style="margin-bottom:6px;font-size:.83rem">Design Rationale</div>
        <div style="font-size:.82rem;line-height:1.7;color:var(--text-2)">${simpleMarkdown(escHtml(data.design_rationale))}</div>
      </div>`:''}
      ${decisions.length?`<div class="card" style="margin:0 0 12px;padding:14px 16px">
        <div class="card-title" style="margin-bottom:8px;font-size:.83rem">Key Design Decisions</div>
        <ul style="margin:0;padding-left:18px">${decisions.map(d=>`<li style="font-size:.81rem;line-height:1.6;color:var(--text-2);margin-bottom:4px">${simpleMarkdown(escHtml(String(d)))}</li>`).join('')}</ul>
      </div>`:''}
      ${eps.length?`<div class="card" style="margin:0 0 12px;padding:14px 16px">
        <div class="card-title" style="margin-bottom:8px;font-size:.83rem">Entry Points</div>
        <div style="display:flex;flex-direction:column;gap:4px">
          ${eps.slice(0,8).map(e=>`<div style="font-size:.79rem;padding:4px 8px;background:var(--surface-2);border-radius:4px;font-family:monospace">
            <span style="color:var(--accent)">${escHtml(e.qualified_name||e.name||'?')}</span>
            <span style="color:var(--text-3);margin-left:8px">${escHtml(e.kind||'')}</span>
            <span style="color:var(--text-3);margin-left:8px;font-size:.72rem">${escHtml((e.file||'').split('/').slice(-2).join('/'))}</span>
          </div>`).join('')}
        </div>
      </div>`:''}
      ${chain.length?`<div class="card" style="margin:0 0 12px;padding:14px 16px">
        <div class="card-title" style="margin-bottom:8px;font-size:.83rem">Call Chain (${chain.length} nodes)</div>
        <div style="display:flex;flex-direction:column;gap:2px">
          ${chain.slice(0,20).map((n,i)=>`<div style="display:flex;align-items:center;gap:8px;font-size:.78rem;padding:3px 0;border-bottom:1px solid var(--surface-2)">
            <span style="color:var(--text-3);min-width:22px;font-variant-numeric:tabular-nums">${i+1}.</span>
            <span style="color:var(--accent);font-family:monospace">${escHtml(n.qualified_name||n.name||'?')}</span>
            <span style="color:var(--text-3)">${escHtml(n.kind||'')}</span>
            <span style="color:var(--text-3);font-size:.71rem;margin-left:auto">${escHtml((n.file||'').split('/').slice(-2).join('/'))}</span>
          </div>`).join('')}
          ${chain.length>20?`<div style="font-size:.75rem;color:var(--text-3);padding-top:6px">…and ${chain.length-20} more</div>`:''}
        </div>
      </div>`:''}
      ${services.length?`<div class="card" style="margin:0 0 12px;padding:14px 16px">
        <div class="card-title" style="margin-bottom:8px;font-size:.83rem">Involved Services / Layers</div>
        <div style="display:flex;flex-wrap:wrap;gap:6px">${services.map(s=>`<span style="font-size:.78rem;padding:3px 10px;background:var(--surface-2);border-radius:12px;color:var(--text-2)">${escHtml(String(s))}</span>`).join('')}</div>
      </div>`:''}
    `;
  }catch(e){$('feature-result').innerHTML=`<div style="color:var(--red);font-size:.81rem">Error: ${escHtml(e.message)}</div>`;}
}

/* ── Coverage Status ─────────────────────────────────────────────────────────── */
async function loadSysstat(){
  let data;
  try{data=await api('/system_status');}
  catch(e){$('sysstat-summary').textContent='Could not load status: '+e.message;return;}
  if(!data||data.error){$('sysstat-summary').textContent='Status unavailable: '+(data&&data.error||'unknown error');return;}

  // ocs_status.py uses pass/warn/fail; normalise
  const passed=data.passed??data.pass??0;
  const warned=data.warned??data.warn??0;
  const failed=data.failed??data.fail??0;
  const total=data.total_checks||(passed+warned+failed+( data.skip??0));
  const pct=total?Math.round(passed/total*100):0;
  const color=failed>0?'var(--red)':warned>0?'var(--amber)':'var(--green)';
  $('sysstat-summary').innerHTML=`<span style="font-size:1.1rem;font-weight:700;color:${color}">${pct}% PASS</span> &nbsp; ${passed} passed · ${warned} warned · ${failed} failed of ${total} checks`;
  if(data.timestamp)$('sysstat-ts').textContent='Last run: '+new Date(data.timestamp*1000).toLocaleString();

  // Build category map from flat checks array
  const cats={};
  (data.checks||[]).forEach(c=>{
    const cat=c.category||'misc';
    if(!cats[cat])cats[cat]={label:cat.replace(/_/g,' '),items:[],worst:'PASS'};
    cats[cat].items.push(c);
    if(c.status==='FAIL')cats[cat].worst='FAIL';
    else if(c.status==='WARN'&&cats[cat].worst!=='FAIL')cats[cat].worst='WARN';
    else if(c.status==='SKIP'&&cats[cat].worst==='PASS')cats[cat].worst='SKIP';
  });
  // Also accept pre-built categories dict
  const catsDict=data.categories||cats;
  $('sysstat-categories').innerHTML=Object.entries(catsDict).map(([k,v])=>{
    const st=v.status||v.worst||'PASS';
    const stColor=st==='FAIL'?'var(--red)':st==='WARN'?'var(--amber)':st==='SKIP'?'var(--text-3)':'var(--green)';
    const msgs=(v.items||[]).filter(i=>i.message).slice(0,4).map(i=>`<div style="color:var(--text-3);font-size:.74rem;padding-left:10px">${escHtml(i.status+': '+i.name+' — '+i.message)}</div>`).join('');
    const details=(v.details||[]).slice(0,4).map(d=>`<div style="color:var(--text-3);font-size:.74rem;padding-left:10px">${escHtml(d)}</div>`).join('');
    const body=msgs||details;
    return `<div style="padding:8px 12px;background:var(--surface-2);border-radius:var(--radius);border-left:3px solid ${stColor}">
      <div style="display:flex;align-items:center;gap:10px;margin-bottom:${body?'4px':'0'}">
        <span style="color:${stColor};font-weight:700;font-size:.78rem">${escHtml(st)}</span>
        <span style="color:var(--text-2);font-size:.81rem">${escHtml(v.label||k)}</span>
        ${v.message?`<span style="color:var(--text-3);font-size:.76rem;margin-left:auto">${escHtml(v.message)}</span>`:''}
      </div>${body}</div>`;
  }).join('');

  // Coverage matrix — use pre-built or skip
  if(data.coverage_matrix){
    const mat=data.coverage_matrix;const tiers=['unit','integration','e2e_mock','e2e_real'];
    $('sysstat-coverage').innerHTML=`<table style="font-size:.77rem;width:100%;border-collapse:collapse">
      <thead><tr><th style="text-align:left;padding:4px 8px;color:var(--text-3)">Feature</th>${tiers.map(t=>`<th style="padding:4px 8px;color:var(--text-3);text-align:center">${escHtml(t.replace(/_/g,' '))}</th>`).join('')}</tr></thead>
      <tbody>${mat.map(row=>`<tr>${[row.feature,...tiers.map(t=>{const n=row[t]??0;const c=n>0?'var(--green)':'var(--text-3)';return `<td style="text-align:center;padding:3px 8px;color:${c}">${n>0?n+' ✓':'—'}</td>`;})].map((cell,i)=>i===0?`<td style="padding:3px 8px;color:var(--text-2)">${escHtml(cell)}</td>`:cell).join('')}</tr>`).join('')}</tbody>
    </table>`;
  }else{
    $('sysstat-coverage').innerHTML='<div style="color:var(--text-3);font-size:.81rem">Coverage matrix not yet available — run a full check to populate.</div>';
  }

  // Dashboard completeness checklist
  const checklist=data.dashboard_checklist||[
    {done:true,label:'Chart.js time-series charts (Health tab)'},
    {done:true,label:'SSE live stream (/api/events/stream)'},
    {done:true,label:'Alert rules panel (/api/alerts)'},
    {done:true,label:'Metrics persistence (SQLite)'},
    {done:true,label:'Static file serving (/static/)'},
    {done:true,label:'Coverage status tab (this page)'},
    {done:false,label:'Service topology force-directed graph (Phase 3f)'},
    {done:false,label:'Federation topology map tab (Phase 3g)'},
  ];
  $('sysstat-dashboard-checklist').innerHTML=checklist.map(item=>{
    const done=item.done;
    const sc=done?'var(--green)':'var(--text-3)';const tc=done?'var(--text-2)':'var(--text-3)';
    return `<div style="display:flex;align-items:center;gap:8px;padding:2px 0"><span style="color:${sc};font-size:.9rem">${done?'✓':'○'}</span><span style="color:${tc}">${escHtml(item.label)}</span></div>`;
  }).join('');
}

async function runSysstat(){
  $('sysstat-run-btn').disabled=true;
  $('sysstat-spinner').style.display='';
  try{
    await fetch('/api/system_status?refresh=1',{method:'POST'}).catch(()=>{});
    await new Promise(r=>setTimeout(r,3000));
    await loadSysstat();
  }finally{
    $('sysstat-run-btn').disabled=false;
    $('sysstat-spinner').style.display='none';
  }
}

/* ── Import Cycles ───────────────────────────────────────────────────────────── */
async function loadImportCycles(){
  if(!currentProject){toast('Select a project first','warn');return;}
  const el=$('import-cycles-result');
  el.innerHTML='<div style="color:var(--text-3)">Scanning…</div>';
  try{
    const d=await api('/import_cycles?project='+encodeURIComponent(currentProject));
    if(d.error){el.innerHTML=`<div style="color:var(--red)">${escHtml(d.error)}</div>`;return;}
    if(!d.cycle_count){
      el.innerHTML='<div style="color:var(--green);font-size:.88rem">✓ No circular imports detected.</div>';
      return;
    }
    const rows=d.cycles.map(c=>{
      const sev=c.severity==='high'?'var(--red)':c.severity==='medium'?'var(--amber)':'var(--green)';
      return `<tr><td style="color:${sev};font-weight:600">${escHtml(c.severity)}</td><td>${c.length}</td><td style="font-family:monospace;font-size:.78rem">${c.cycle.map(escHtml).join(' → ')}</td></tr>`;
    }).join('');
    el.innerHTML=`<p style="font-size:.81rem;color:var(--amber)">⚠ ${d.cycle_count} cycle(s) found</p>
<table style="width:100%;border-collapse:collapse;font-size:.8rem">
<thead><tr><th style="text-align:left;padding:4px 8px;border-bottom:1px solid var(--border)">Severity</th><th style="padding:4px 8px;border-bottom:1px solid var(--border)">Len</th><th style="text-align:left;padding:4px 8px;border-bottom:1px solid var(--border)">Cycle</th></tr></thead>
<tbody>${rows}</tbody></table>`;
  }catch(e){el.innerHTML=`<div style="color:var(--red)">Error: ${escHtml(e.message)}</div>`;}
}

/* ── Callflow ────────────────────────────────────────────────────────────────── */
async function runCallflow(){
  const sym=($('callflow-symbol').value||'').trim();
  const dir=$('callflow-direction').value||'callees';
  if(!sym){toast('Enter a symbol name','warn');return;}
  if(!currentProject){toast('Select a project first','warn');return;}
  const el=$('callflow-result');
  el.innerHTML='<div style="color:var(--text-3)">Loading callflow…</div>';
  try{
    const url=`/api/callflow_html?project=${encodeURIComponent(currentProject)}&symbol=${encodeURIComponent(sym)}&direction=${dir}&depth=5`;
    const r=await fetch(url);
    if(!r.ok){el.innerHTML=`<div style="color:var(--red)">Error: ${r.status}</div>`;return;}
    const html=await r.text();
    const iframe=document.createElement('iframe');
    iframe.style.cssText='width:100%;height:500px;border:1px solid var(--border);border-radius:var(--radius);background:#fff';
    iframe.srcdoc=html;
    el.innerHTML='';
    el.appendChild(iframe);
  }catch(e){el.innerHTML=`<div style="color:var(--red)">Error: ${escHtml(e.message)}</div>`;}
}

/* ── Dedup ───────────────────────────────────────────────────────────────────── */
async function runDedup(dryRun){
  if(!currentProject){toast('Select a project first','warn');return;}
  const threshold=parseFloat($('dedup-threshold').value)||0.88;
  const el=$('dedup-result');
  el.innerHTML='<div style="color:var(--text-3)">'+(dryRun?'Previewing…':'Applying merge…')+'</div>';
  try{
    const r=await fetch('/api/dedup',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({project:currentProject,dry_run:dryRun,threshold}),
    });
    const d=await r.json();
    if(d.error){el.innerHTML=`<div style="color:var(--red)">${escHtml(d.error)}</div>`;return;}
    const badge=dryRun?'<span style="background:var(--amber);color:#000;font-size:.72rem;padding:1px 6px;border-radius:99px;margin-left:6px">DRY RUN</span>':'';
    const stratBadge=`<span style="font-size:.72rem;color:var(--text-3)">${d.strategy||'exact'} strategy · fuzzy=${d.fuzzy_available?'yes':'no'}</span>`;
    el.innerHTML=`<div style="margin-bottom:10px">
      <span style="font-size:.95rem;font-weight:600">${d.merged_count} node(s) ${dryRun?'would be merged':'merged'}</span>${badge}&nbsp;&nbsp;${stratBadge}
      <div style="font-size:.79rem;color:var(--text-3);margin-top:4px">${d.candidate_pairs_checked} pairs checked · ${d.skipped_low_entropy||0} low-entropy skipped</div>
    </div>`+
    (d.merged_pairs&&d.merged_pairs.length?
      `<div style="font-size:.79rem;max-height:200px;overflow-y:auto;background:var(--surface-3);padding:8px;border-radius:var(--radius)">
        ${d.merged_pairs.map(p=>`<div style="font-family:monospace;white-space:nowrap">${escHtml(p[0])} ← ${escHtml(p[1])}</div>`).join('')}
      </div>`:'<div style="color:var(--green);font-size:.82rem">✓ No duplicates found.</div>');
    if(!dryRun&&d.merged_count>0)toast(`Merged ${d.merged_count} duplicate node(s)`,'info');
  }catch(e){el.innerHTML=`<div style="color:var(--red)">Error: ${escHtml(e.message)}</div>`;}
}

async function loadFileTree(){
  if(!currentProject){toast('Select a project first','warn');return;}
  const wrap=$('file-tree-wrap');
  wrap.innerHTML='<div style="padding:12px;color:var(--text-3)">Loading…</div>';
  try{
    const r=await fetch('/api/tree_html?project='+encodeURIComponent(currentProject)+'&format=html');
    if(!r.ok){const d=await r.json();wrap.innerHTML=`<div style="padding:12px;color:var(--red)">${escHtml(d.error||'Error')}</div>`;return;}
    const html=await r.text();
    const iframe=document.createElement('iframe');
    iframe.style.cssText='width:100%;height:500px;border:none;background:#1e1e2e';
    wrap.innerHTML='';
    wrap.appendChild(iframe);
    iframe.srcdoc=html;
  }catch(e){wrap.innerHTML=`<div style="padding:12px;color:var(--red)">Error: ${escHtml(e.message)}</div>`;}
}

async function runPrImpact(){
  if(!currentProject){toast('Select a project first','warn');return;}
  const el=$('pr-impact-result');
  el.innerHTML='<div style="color:var(--text-3)">Analyzing…</div>';
  const raw=$('pr-files-input').value.trim();
  const files=raw?raw.split('\n').map(s=>s.trim()).filter(Boolean):null;
  const base=$('pr-base-branch').value.trim()||'main';
  try{
    const r=await fetch('/api/pr_impact',{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({project:currentProject,files,base_branch:base}),
    });
    const d=await r.json();
    if(d.error){el.innerHTML=`<div style="color:var(--red)">${escHtml(d.error)}</div>`;return;}
    const riskColor={none:'var(--text-3)',low:'var(--green)',medium:'var(--amber)',high:'var(--red)'}[d.risk_level]||'var(--text-1)';
    const comms=(d.communities_touched||[]).map(c=>`<span style="background:var(--surface-3);padding:1px 6px;border-radius:4px;font-size:.79rem;margin:2px 2px">${escHtml(c.title||'C'+c.community_id)}</span>`).join('');
    el.innerHTML=`<div style="margin-bottom:12px">
      <span style="font-size:1rem;font-weight:700;color:${riskColor}">Risk: ${d.risk_level.toUpperCase()}</span>
      <span style="margin-left:12px;font-size:.82rem;color:var(--text-2)">${d.nodes_affected} nodes · ${d.community_count} communities</span>
    </div>
    ${comms?`<div style="margin-bottom:10px">${comms}</div>`:''}
    ${(d.top_affected_nodes||[]).length?`<div style="font-size:.79rem;color:var(--text-2);margin-top:8px">Top affected: ${d.top_affected_nodes.slice(0,8).map(n=>`<code>${escHtml(n.name)}</code>`).join(', ')}</div>`:''}`;
  }catch(e){el.innerHTML=`<div style="color:var(--red)">Error: ${escHtml(e.message)}</div>`;}
}

async function runVacuum(dryRun){
  if(!currentProject){toast('Select a project first','warn');return;}
  const el=$('vacuum-result');
  el.innerHTML='<div style="color:var(--text-3)">'+(dryRun?'Scanning…':'Running vacuum…')+'</div>';
  try{
    const r=await fetch('/api/vacuum',{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({project:currentProject,dry_run:dryRun}),
    });
    const d=await r.json();
    if(d.error){el.innerHTML=`<div style="color:var(--red)">${escHtml(d.error)}</div>`;return;}
    const dirs=d.orphan_dirs_found||d.orphan_dirs_removed||[];
    const badge=dryRun?'<span style="background:var(--amber);color:#000;font-size:.72rem;padding:1px 6px;border-radius:99px">DRY RUN</span>':'';
    el.innerHTML=`<div style="margin-bottom:8px">
      <span style="font-weight:600">${dirs.length} orphan dir(s) ${dryRun?'found':'removed'}</span> ${badge}
      <span style="margin-left:8px;color:var(--text-3);font-size:.82rem">${d.freed_mb} MB freed</span>
    </div>`+
    (dirs.length?`<div style="font-family:monospace;font-size:.78rem;background:var(--surface-3);padding:8px;border-radius:var(--radius);max-height:160px;overflow-y:auto">${dirs.map(p=>`<div>${escHtml(p)}</div>`).join('')}</div>`:'<div style="color:var(--green);font-size:.82rem">✓ No orphan dirs found.</div>')+
    (d.empty_projects&&d.empty_projects.length?`<div style="margin-top:10px;font-size:.79rem;color:var(--amber)">${d.empty_project_count} empty project(s) in registry: ${d.empty_projects.slice(0,5).map(p=>`<code>${escHtml(p.split('/').pop())}</code>`).join(', ')}</div>`:'');
    if(!dryRun&&dirs.length)toast(`Vacuum freed ${d.freed_mb} MB`,'info');
  }catch(e){el.innerHTML=`<div style="color:var(--red)">Error: ${escHtml(e.message)}</div>`;}
}

/* ── Jobs ────────────────────────────────────────────────────────────────────── */
async function loadJobs(){
  const statusFilter=$('jobs-filter-status').value;
  const el=$('jobs-table');
  try{
    let url='/jobs';
    if(currentProject)url+='?project='+encodeURIComponent(currentProject);
    const d=await api(url);
    let jobs=d.jobs||[];
    if(statusFilter)jobs=jobs.filter(j=>j.status===statusFilter);
    $('jobs-count').textContent=jobs.length+' job(s)';
    if(!jobs.length){el.innerHTML='<div style="color:var(--text-3);font-size:.81rem;padding:10px">No jobs found.</div>';return;}
    const statusCls={queued:'warn',running:'',ok:'ok',error:'err',cancelled:''};
    const statusIcon={queued:'⏳',running:'⚙',ok:'✓',error:'✗',cancelled:'⦸'};
    function dur(j){
      if(!j.started_at)return'—';
      const end=j.completed_at?new Date(j.completed_at):new Date();
      return((end-new Date(j.started_at))/1000).toFixed(1)+'s';
    }
    el.innerHTML=`<div style="overflow-x:auto"><table style="width:100%;font-size:.79rem;border-collapse:collapse">
      <thead><tr style="border-bottom:1px solid var(--border-2)">
        <th style="text-align:left;padding:6px 8px;color:var(--text-3);font-weight:600">ID</th>
        <th style="text-align:left;padding:6px 8px;color:var(--text-3);font-weight:600">Action</th>
        <th style="text-align:left;padding:6px 8px;color:var(--text-3);font-weight:600">Project</th>
        <th style="text-align:left;padding:6px 8px;color:var(--text-3);font-weight:600">Status</th>
        <th style="text-align:left;padding:6px 8px;color:var(--text-3);font-weight:600">Queued</th>
        <th style="text-align:left;padding:6px 8px;color:var(--text-3);font-weight:600">Duration</th>
        <th style="text-align:left;padding:6px 8px;color:var(--text-3);font-weight:600">Error</th>
        <th style="text-align:left;padding:6px 8px;color:var(--text-3);font-weight:600"></th>
      </tr></thead>
      <tbody>${jobs.map(j=>{
        const cls=statusCls[j.status]||'';
        const icon=statusIcon[j.status]||'';
        const proj=j.project_path?(j.project_path.split('/').slice(-2).join('/')):'—';
        const qAt=j.queued_at?new Date(j.queued_at).toLocaleTimeString():'—';
        const errTxt=j.error?`<code style="color:var(--red);word-break:break-all">${escHtml(j.error.slice(0,80))}</code>`:'—';
        const canCancel=j.status==='queued'||j.status==='running';
        return`<tr style="border-bottom:1px solid var(--border)">
          <td style="padding:5px 8px;font-family:monospace;color:var(--text-3)">${escHtml(j.id)}</td>
          <td style="padding:5px 8px;color:var(--accent)">${escHtml(j.action)}</td>
          <td style="padding:5px 8px;color:var(--text-2)" title="${escAttr(j.project_path||'')}">${escHtml(proj)}</td>
          <td style="padding:5px 8px"><span class="badge ${cls}">${icon} ${escHtml(j.status)}</span></td>
          <td style="padding:5px 8px;color:var(--text-3)">${qAt}</td>
          <td style="padding:5px 8px;color:var(--text-2)">${dur(j)}</td>
          <td style="padding:5px 8px">${errTxt}</td>
          <td style="padding:5px 8px">${canCancel?`<button class="btn secondary" style="font-size:.72rem;padding:2px 8px" onclick="cancelJob('${escAttr(j.id)}')">Cancel</button>`:''}</td>
        </tr>`;
      }).join('')}</tbody>
    </table></div>`;
  }catch(e){el.innerHTML=`<div style="color:var(--red);padding:10px">Error: ${escHtml(e.message)}</div>`;}
}

async function cancelJob(jobId){
  try{
    const r=await fetch('/api/jobs/'+jobId+'/cancel',{method:'POST'});
    const d=await r.json();
    if(d.cancelled)toast('Job '+jobId+' cancellation requested','info');
    else toast('Could not cancel job '+jobId,'warn');
    await loadJobs();
  }catch(e){toast('Cancel error: '+e.message,'warn');}
}

/* ── Saved Queries ───────────────────────────────────────────────────────────── */
const _SQ_KEY='opencode_saved_queries';

function _sqLoad(){try{return JSON.parse(localStorage.getItem(_SQ_KEY)||'[]');}catch(e){return[];}}
function _sqSave(qs){try{localStorage.setItem(_SQ_KEY,JSON.stringify(qs));}catch(e){}}

function loadSavedQueries(){
  const qs=_sqLoad();
  const el=$('sq-list');
  if(!qs.length){el.innerHTML='<div style="color:var(--text-3);font-size:.81rem;padding:10px">No saved queries yet. Save one above.</div>';return;}
  el.innerHTML=qs.map((q,i)=>`<div class="card" style="margin-bottom:10px">
    <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:10px;flex-wrap:wrap">
      <div style="flex:1;min-width:200px">
        <div style="font-weight:600;font-size:.88rem;margin-bottom:3px">${escHtml(q.name||q.query)}</div>
        <code style="font-size:.77rem;color:var(--cyan)">${escHtml(q.query)}</code>
        <span style="margin-left:8px;font-size:.72rem;background:var(--surface-3);color:var(--text-3);padding:1px 6px;border-radius:99px">${escHtml(q.scope||'code')}</span>
        ${q.note?`<div style="font-size:.75rem;color:var(--text-3);margin-top:3px">${escHtml(q.note)}</div>`:''}
      </div>
      <div style="display:flex;gap:6px;flex-shrink:0">
        <button class="btn" style="font-size:.75rem;padding:3px 10px" onclick="runSavedQuery(${i})">▶ Run</button>
        <button class="btn secondary" style="font-size:.75rem;padding:3px 10px;color:var(--red)" onclick="deleteSavedQuery(${i})">Delete</button>
      </div>
    </div>
  </div>`).join('');
}

function saveQuery(){
  const query=$('sq-query').value.trim();
  if(!query){toast('Query text is required','warn');return;}
  const qs=_sqLoad();
  qs.unshift({
    id:Date.now().toString(36),
    name:$('sq-name').value.trim()||query.slice(0,40),
    query,
    scope:$('sq-scope').value||'code',
    note:$('sq-note').value.trim(),
    saved_at:new Date().toISOString(),
  });
  _sqSave(qs);
  $('sq-query').value='';$('sq-name').value='';$('sq-note').value='';
  toast('Query saved','info');
  loadSavedQueries();
}

function runSavedQuery(i){
  const qs=_sqLoad();
  const q=qs[i];if(!q)return;
  $('search-q').value=q.query;
  if($('search-scope'))$('search-scope').value=q.scope||'code';
  showPage('search');
  runSearch();
}

function deleteSavedQuery(i){
  const qs=_sqLoad();
  qs.splice(i,1);
  _sqSave(qs);
  loadSavedQueries();
}

/* ── Auto-load: Impact top nodes ─────────────────────────────────────────────── */
/* trigger on page load — show top-10 impactful nodes automatically */
async function _autoLoadImpact(){
  if(!currentProject)return;
  const el=$('impact-result');
  if(!el)return;
  el.innerHTML='<div class="loader">Loading top impactful symbols…</div>';
  try{
    const data=await api('/graph?project='+encodeURIComponent(currentProject)+'&symbol=*&relation=impact&max_nodes=12');
    const nodes=(data.nodes||data.results||[]).slice(0,12);
    if(!nodes.length){el.innerHTML='<div style="color:var(--text-3);font-size:.81rem">No graph data yet. Index the project first.</div>';return;}
    el.innerHTML=`<div style="margin-bottom:10px;font-size:.79rem;color:var(--text-3)">Top impactful symbols — click any to analyze blast radius</div>
      <div style="display:flex;flex-direction:column;gap:4px">
        ${nodes.map(n=>{
          const sym=n.qualified_name||n.name||n.symbol||String(n);
          const sym_e=typeof sym==='string'?sym:JSON.stringify(sym);
          const deg=n.out_degree??n.degree??'';
          return `<div onclick="$('impact-symbol').value=${JSON.stringify(sym_e)};runImpactAnalysis()" style="cursor:pointer;display:flex;align-items:center;gap:10px;padding:7px 10px;background:var(--surface-2);border-radius:var(--radius);border:1px solid transparent;transition:border .15s" onmouseenter="this.style.borderColor='var(--accent)'" onmouseleave="this.style.borderColor='transparent'">
            <span style="color:var(--accent);font-family:monospace;font-size:.82rem;flex:1">${escHtml(sym_e)}</span>
            ${deg!==''?`<span style="font-size:.75rem;color:var(--text-3);background:var(--surface-3);padding:1px 7px;border-radius:99px">${deg} edges</span>`:''}
          </div>`;
        }).join('')}
      </div>`;
  }catch(e){
    el.innerHTML=`<div style="color:var(--text-3);font-size:.82rem">Enter a symbol name to see its blast radius and risk level.</div>`;
  }
}
function loadTopImpact(){_autoLoadImpact();}

/* ── Auto-load: PR Impact git diff ──────────────────────────────────────────── */
async function loadPrImpactAuto(){
  if(!currentProject)return;
  const ta=$('pr-files-input');
  if(ta&&ta.value.trim())return; // user already typed something
  const el=$('pr-result');if(!el)return;
  el.innerHTML='<div class="loader">Auto-detecting changed files from git…</div>';
  // trigger run with empty files → backend does git diff
  try{
    const branch=($('pr-base-branch')&&$('pr-base-branch').value)||'main';
    const r=await fetch('/api/pr_impact',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({project:currentProject,base_branch:branch,files:[]})});
    const d=await r.json();
    _renderPrImpact(d);
  }catch(e){
    if(el)el.innerHTML=`<div style="color:var(--text-3);font-size:.82rem">Paste changed file paths above or click Analyze to detect from git diff.</div>`;
  }
}

/* ── Auto-load: Vacuum status ────────────────────────────────────────────────── */
async function loadVacuumStatus(){
  if(!currentProject)return;
  const el=$('vacuum-result');if(!el)return;
  // Only auto-load if placeholder is showing
  if(!el.textContent.includes('Dry Run'))return;
  el.innerHTML='<div class="loader">Scanning for orphan dirs (dry run)…</div>';
  try{
    await runVacuum(true);
  }catch(e){
    if(el)el.innerHTML='<div style="color:var(--text-3);font-size:.82rem">Click "Dry Run" to preview what would be removed.</div>';
  }
}

/* ── Auto-load: Dedup status ─────────────────────────────────────────────────── */
async function loadDedupStatus(){
  if(!currentProject)return;
  const el=$('dedup-result');if(!el)return;
  // Only auto-run if placeholder is showing
  if(!el.textContent.includes('Dry Run'))return;
  el.innerHTML='<div class="loader">Scanning for duplicate nodes (dry run)…</div>';
  try{
    await runDedup(true);
  }catch(e){
    if(el)el.innerHTML='<div style="color:var(--text-3);font-size:.82rem">Click "Dry Run" to preview what would be merged.</div>';
  }
}

/* ── SSE live updates ────────────────────────────────────────────────────────── */
let _liveFeedInit=false;
const _MAX_FEED=50;
function _pushLiveFeed(dotCls,text,subtext){
  const feed=$('live-feed');if(!feed)return;
  if(!_liveFeedInit){feed.innerHTML='';_liveFeedInit=true;}
  const now=new Date().toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',second:'2-digit'});
  const item=document.createElement('div');
  item.className='live-feed-item';
  item.innerHTML=`<span class="live-dot ${dotCls}"></span><span style="flex:1;color:var(--text-2)">${escHtml(text)}</span>${subtext?`<span style="color:var(--text-3);font-size:.72rem">${escHtml(subtext)}</span>`:''}
    <span style="color:var(--text-3);font-size:.71rem;flex-shrink:0;margin-left:6px">${now}</span>`;
  feed.insertBefore(item,feed.firstChild);
  // Keep at most _MAX_FEED items
  while(feed.children.length>_MAX_FEED)feed.removeChild(feed.lastChild);
  const badge=$('live-feed-badge');
  if(badge){badge.textContent='● LIVE';badge.className='badge ok';}
}

(function initSSE(){
  let es;
  function connect(){
    es=new EventSource('/api/events/stream');
    es.onmessage=function(ev){
      let msg;try{msg=JSON.parse(ev.data);}catch(e){return;}
      if(msg.type==='metrics'){
        $('daemon-dot').className='daemon-dot ok';
        $('daemon-status').textContent='connected';
        if($('metrics-snapshot'))$('metrics-snapshot').textContent=
          `${msg.call_count??0} searches · p50=${msg.latency_p50_ms??'—'}ms · p95=${msg.latency_p95_ms??'—'}ms · 0-result=${msg.zero_result_pct!=null?msg.zero_result_pct.toFixed(1):'—'}%`;
        // Push periodic heartbeat to live feed
        const load=msg.load_avg_1m!=null?`load=${msg.load_avg_1m.toFixed(2)}`:'';
        const calls=msg.call_count!=null?`${msg.call_count} calls`:'';
        const parts=[calls,load].filter(Boolean).join(' · ');
        if(parts)_pushLiveFeed('blue','Heartbeat',parts);
      }
      if(msg.type==='search'){
        const q=msg.query?`"${msg.query.slice(0,40)}"`:'-';
        const lat=msg.latency_ms!=null?`${msg.latency_ms.toFixed(0)}ms`:'';
        const hits=msg.result_count!=null?`${msg.result_count} hits`:'';
        _pushLiveFeed('green',`Search ${q}`,[lat,hits].filter(Boolean).join(' · '));
      }
      if(msg.type==='job'){
        const act=msg.action||'';const status=msg.status||'';
        const dotC=status==='done'?'green':status==='error'?'red':'blue';
        _pushLiveFeed(dotC,`Job ${act}`,status);
        // Refresh jobs tab if open
        if(document.querySelector('#page-jobs.active'))loadJobs();
        // Refresh health if enrich_hierarchy completed
        if(act==='enrich_hierarchy'&&status==='done'){
          if(document.querySelector('#page-health.active'))loadKBHealth();
          if(document.querySelector('#page-overview.active'))loadOverview();
        }
      }
      if(msg.type==='index'){
        const proj=(msg.project||'').split('/').pop();
        _pushLiveFeed('blue',`Indexed ${proj}`,msg.files_indexed!=null?`${msg.files_indexed} files`:'');
      }
    };
    es.onerror=function(){
      $('daemon-dot').className='daemon-dot err';
      const badge=$('live-feed-badge');
      if(badge){badge.textContent='SSE';badge.className='badge none';}
      es.close();
      setTimeout(connect,10000);
    };
  }
  connect();
})();

/* ── Boot ────────────────────────────────────────────────────────────────────── */
(async()=>{
  // Set correct theme icon on boot (default is light)
  const isLight=document.documentElement.dataset.theme==='light';
  $('theme-btn').textContent=isLight?'🌙':'☀';

  try{
    await loadProjects();
    await loadOverview();
    loadAlerts();  // populate alert badge on startup without blocking
  }catch(e){
    $('daemon-dot').className='daemon-dot err';
    $('daemon-status').textContent='error';
    $('projects-table').innerHTML=`<div style="color:var(--red);padding:10px">Failed to connect to daemon: ${escHtml(e.message)}</div>`;
  }
  // Auto-refresh every 15s for overview; every 30s for health; alerts every 45s
  setInterval(()=>{
    if(document.querySelector('#page-overview.active'))loadOverview();
  },15000);
  setInterval(()=>{
    if(document.querySelector('#page-health.active'))loadStatus();
  },30000);
  setInterval(loadAlerts, 45000);
})();
</script>
</body>
</html>
"""
