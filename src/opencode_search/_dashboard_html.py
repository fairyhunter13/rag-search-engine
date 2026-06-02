"""Self-contained dashboard HTML — imported by dashboard.py.

Single file, no CDN, no build step.  All CSS and JS are inline.
"""
from __future__ import annotations

_DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>opencode-search</title>
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
.kpi-card.ok{border-top-color:var(--green)}.kpi-card.ok .kpi-val{color:var(--green)}
.kpi-card.warn{border-top-color:var(--amber)}.kpi-card.warn .kpi-val{color:var(--amber)}
.kpi-card.crit{border-top-color:var(--red)}.kpi-card.crit .kpi-val{color:var(--red)}
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
/* ── Responsive ─────────────────────────────────────────────────────────────── */
@media(max-width:768px){
  .sidebar{width:48px}.brand-name,.nav-group,.sb-project{display:none}
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
    <button class="nav-btn active" id="nav-overview" onclick="showPage('overview')"><span class="nav-icon">⬡</span><span class="nav-label">Overview</span></button>
    <div class="nav-group">Explore</div>
    <button class="nav-btn" id="nav-search" onclick="showPage('search')"><span class="nav-icon">⚡</span><span class="nav-label">Search</span></button>
    <button class="nav-btn" id="nav-ask" onclick="showPage('ask')"><span class="nav-icon">💬</span><span class="nav-label">Ask</span></button>
    <button class="nav-btn" id="nav-graph" onclick="showPage('graph')"><span class="nav-icon">🕸</span><span class="nav-label">Graph</span></button>
    <div class="nav-group">Knowledge</div>
    <button class="nav-btn" id="nav-structure" onclick="showPage('structure')"><span class="nav-icon">📁</span><span class="nav-label">Structure</span></button>
    <button class="nav-btn" id="nav-patterns" onclick="showPage('patterns')"><span class="nav-icon">🎯</span><span class="nav-label">Patterns</span></button>
    <button class="nav-btn" id="nav-wiki" onclick="showPage('wiki')"><span class="nav-icon">📖</span><span class="nav-label">Wiki</span></button>
    <button class="nav-btn" id="nav-communities" onclick="showPage('communities')"><span class="nav-icon">🏘</span><span class="nav-label">Communities</span></button>
    <div class="nav-group">Monitor</div>
    <button class="nav-btn" id="nav-health" onclick="showPage('health')"><span class="nav-icon">💓</span><span class="nav-label">Health</span></button>
    <button class="nav-btn" id="nav-verify" onclick="showPage('verify')"><span class="nav-icon">✅</span><span class="nav-label">Verify</span></button>
    <button class="nav-btn" id="nav-release" onclick="showPage('release')"><span class="nav-icon">🚀</span><span class="nav-label">Release</span></button>
    <button class="nav-btn" id="nav-qa" onclick="showPage('qa')"><span class="nav-icon">🔬</span><span class="nav-label">QA Gate</span></button>
    <div class="nav-group">Admin</div>
    <button class="nav-btn" id="nav-projects" onclick="showPage('projects')"><span class="nav-icon">📋</span><span class="nav-label">Projects</span></button>
    <button class="nav-btn" id="nav-integrations" onclick="showPage('integrations')"><span class="nav-icon">🔌</span><span class="nav-label">Integrations</span></button>
    <div class="nav-group">Intelligence</div>
    <button class="nav-btn" id="nav-arch-map" onclick="showPage('arch-map')"><span class="nav-icon">🏛</span><span class="nav-label">Arch Map</span></button>
    <button class="nav-btn" id="nav-service-mesh" onclick="showPage('service-mesh')"><span class="nav-icon">🕷</span><span class="nav-label">Service Mesh</span></button>
    <button class="nav-btn" id="nav-impact" onclick="showPage('impact')"><span class="nav-icon">💥</span><span class="nav-label">Impact</span></button>
    <button class="nav-btn" id="nav-trace" onclick="showPage('trace')"><span class="nav-icon">🔎</span><span class="nav-label">Trace</span></button>
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
      <span id="daemon-dot" class="daemon-dot" title="Daemon status">●</span>
      <span id="daemon-status" style="font-size:.8rem;color:var(--text-3)">connecting…</span>
      <button class="icon-btn" id="theme-btn" onclick="toggleTheme()" title="Toggle theme">☀</button>
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
    <div class="page-title">Overview</div>
    <div id="overview-kpi" class="kpi-row"></div>
    <div class="two-col">
      <div>
        <div class="card">
          <div class="card-header"><span class="card-title">System Health</span><span id="health-badge" class="badge none">—</span></div>
          <div id="overview-health"><div class="loader">Loading…</div></div>
        </div>
        <div class="card">
          <div class="card-header"><span class="card-title">KB Completeness</span></div>
          <div id="overview-kb"><div class="loader">Loading…</div></div>
        </div>
      </div>
      <div>
        <div class="card">
          <div class="card-header"><span class="card-title">Recent Pipeline Events</span></div>
          <div id="overview-events" class="activity-list"><div class="loader">Loading…</div></div>
        </div>
      </div>
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
          <div class="graph-canvas-wrap">
            <canvas id="graph-canvas"></canvas>
            <div id="graph-legend" class="graph-legend"></div>
          </div>
          <div id="graph-canvas-info" style="font-size:.71rem;color:var(--text-3);padding:3px 0"></div>
          <div style="display:flex;gap:6px;flex-wrap:wrap">
            <button class="btn secondary" style="font-size:.77rem;padding:5px 12px" onclick="visualizeFullGraph(500)">Visualize 500</button>
            <button class="btn secondary" style="font-size:.77rem;padding:5px 12px" onclick="visualizeFullGraph(2000)">Visualize 2K</button>
            <button class="btn danger" style="font-size:.77rem;padding:5px 12px" onclick="stopGraph()">Stop</button>
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
  </div>

  <!-- PAGE: HEALTH -->
  <div id="page-health" class="page">
    <div class="page-title">Health &amp; Monitoring</div>
    <div class="card"><div class="card-title" style="margin-bottom:8px">Daemon Status</div><div id="daemon-metrics" class="stat-grid"></div></div>
    <div class="card">
      <div class="card-title" style="margin-bottom:8px">Knowledge Base Health</div>
      <div id="kb-health-grid" class="stat-grid" style="margin-bottom:14px"></div>
      <div id="kb-health-detail" style="font-size:.77rem;color:var(--text-3)"></div>
    </div>
    <div class="card">
      <div class="card-title" style="margin-bottom:8px">Auto-Pipeline Events</div>
      <div id="pipeline-events-meta" style="font-size:.74rem;color:var(--text-3);margin-bottom:8px"></div>
      <div id="pipeline-events-list" style="font-size:.74rem"></div>
    </div>
    <div class="card"><div class="card-title" style="margin-bottom:8px">Search Metrics</div><pre id="search-metrics">Loading…</pre></div>
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
        <button class="btn secondary" style="font-size:.81rem" onclick="loadServiceMesh()">↺ Scan</button>
      </div>
      <p style="font-size:.79rem;color:var(--text-3);margin-bottom:12px">Detected gRPC, HTTP, message queue, and database connections across federation members.</p>
      <div id="service-mesh-description" style="font-size:.82rem;color:var(--text-2);margin-bottom:12px;padding:8px;background:var(--surface-2);border-radius:var(--radius);display:none"></div>
      <div id="service-mesh-content"><div class="loader">Click Scan to detect service mesh…</div></div>
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
      <div id="impact-result"><div style="color:var(--text-3);font-size:.82rem">Enter a symbol name to see its blast radius and risk level.</div></div>
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
  const isDark=html.dataset.theme!=='light';
  html.dataset.theme=isDark?'light':'dark';
  $('theme-btn').textContent=isDark?'🌙':'☀';
}

/* ── Navigation ──────────────────────────────────────────────────────────────── */
const _PAGE_LOAD={
  overview:loadOverview, search:()=>{}, ask:loadAskSuggestions, graph:()=>{},
  structure:loadStructure, patterns:loadPatterns, wiki:loadWikiList,
  communities:loadCommunities, health:loadStatus, verify:loadVerify,
  release:loadRelease, qa:loadQaGate, projects:()=>{}, integrations:loadIntegrations,
  'arch-map':loadArchMap, 'service-mesh':()=>{}, impact:()=>{}, trace:()=>{},
};

function showPage(name){
  document.querySelectorAll('.page').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.nav-btn').forEach(b=>b.classList.remove('active'));
  const page=$('page-'+name);if(page)page.classList.add('active');
  const btn=$('nav-'+name);if(btn)btn.classList.add('active');
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
    const[health,kbh,aps,over]=await Promise.all([
      fetch('/healthz').then(r=>r.ok?r.json():{}),
      api('/kb_health?project='+encodeURIComponent(currentProject)),
      api('/auto_pipeline_status'),
      api('/overview?project='+encodeURIComponent(currentProject)),
    ]);
    const gs=over.graph_stats||{};
    const pctRaw=kbh.enrichment_pct!=null?kbh.enrichment_pct:null;
    const pct=pctRaw!=null?pctRaw.toFixed(0):'—';

    // KPI cards
    const kpis=[
      {val:(gs.node_count||0).toLocaleString(),lbl:'Graph Nodes',icon:'🕸',cls:gs.node_count>0?'ok':''},
      {val:(gs.edge_count||0).toLocaleString(),lbl:'Graph Edges',icon:'➜',cls:''},
      {val:(kbh.wiki_page_count||0).toLocaleString(),lbl:'Wiki Pages',icon:'📖',cls:kbh.wiki_page_count>0?'ok':''},
      {val:(gs.total_communities||0).toLocaleString(),lbl:'Communities',icon:'🏘',cls:''},
      {val:pct+'%',lbl:'Enriched',icon:'✨',cls:pctRaw>=90?'ok':pctRaw>=50?'warn':''},
      {val:health.connected_clients!=null?String(health.connected_clients):'—',lbl:'Clients',icon:'👁',cls:''},
    ];
    $('overview-kpi').innerHTML=kpis.map(k=>`<div class="kpi-card ${k.cls}"><div class="kpi-val">${escHtml(k.val)}</div><div class="kpi-label">${k.lbl}</div><div class="kpi-icon">${k.icon}</div></div>`).join('');

    updateMetricStrip(kbh,gs,health);

    // Health
    const daemonOk=health.ok===true||health.healthy===true;
    const kbOk=pctRaw!=null&&pctRaw>80&&kbh.wiki_page_count>0;
    const hb=$('health-badge');
    hb.className='badge '+(daemonOk&&kbOk?'ok':'warn');
    hb.textContent=daemonOk&&kbOk?'Healthy':'Degraded';
    $('overview-health').innerHTML=[
      {label:'Daemon',val:daemonOk?'● Running':'○ Down',cls:daemonOk?'ok':'err'},
      {label:'Uptime',val:health.uptime_s!=null?health.uptime_s.toFixed(0)+'s':'—',cls:''},
      {label:'Port',val:health.port||8765,cls:''},
    ].map(r=>`<div style="display:flex;justify-content:space-between;padding:5px 0;font-size:.81rem;border-bottom:1px solid var(--surface-2)">
      <span style="color:var(--text-3)">${r.label}</span>
      <span style="color:${r.cls==='ok'?'var(--green)':r.cls==='err'?'var(--red)':'var(--text-2)'}">${escHtml(String(r.val))}</span></div>`).join('');

    // KB completeness
    $('overview-kb').innerHTML=`
      <div style="display:flex;justify-content:space-between;font-size:.79rem;color:var(--text-3);margin-bottom:4px">
        <span>${kbh.enriched_communities||0}/${kbh.total_communities||0} communities enriched</span>
        <span>${pct}%</span>
      </div>
      <div class="progress-bar"><div class="progress-fill" style="width:${pctRaw||0}%"></div></div>
      <div style="margin-top:8px;font-size:.77rem;color:var(--text-3)">${kbh.wiki_page_count||0} wiki pages · Patterns: ${kbh.patterns_cached?'✓ cached':'✗ none'}</div>`;

    // Recent events
    const events=(aps.events||[]).slice(-8).reverse();
    $('overview-events').innerHTML=events.length
      ?events.map(e=>{
        const dotCls=e.status==='ok'?'ok':e.status==='error'?'error':'scheduled';
        const at=e.at?new Date(e.at).toLocaleTimeString():'';
        return `<div class="activity-item"><div class="activity-dot ${dotCls}"></div>
          <div class="activity-text">${escHtml(e.project||'')} <span style="color:var(--text-3)">${escHtml(e.status)}</span></div>
          <div class="activity-time">${at}</div></div>`;
      }).join('')
      :'<div class="loader">No events this session</div>';
  }catch(e){
    $('overview-kpi').innerHTML=`<div style="color:var(--red);font-size:.81rem;padding:10px">Failed: ${escHtml(e.message)}</div>`;
  }
}

function updateMetricStrip(kbh,gs,health){
  const daemonOk=health&&(health.ok===true||health.healthy===true);
  const pct=kbh&&kbh.enrichment_pct!=null?kbh.enrichment_pct:null;
  const pills=[
    {val:daemonOk?'● Daemon up':'○ Daemon down',cls:daemonOk?'ok':'err'},
    {val:(gs&&gs.node_count?gs.node_count.toLocaleString():'0')+' nodes',cls:gs&&gs.node_count>0?'ok':''},
    {val:(kbh&&kbh.wiki_page_count?kbh.wiki_page_count.toLocaleString():'0')+' wiki',cls:kbh&&kbh.wiki_page_count>0?'ok':''},
    {val:(pct!=null?pct.toFixed(0):'—')+'% enriched',cls:pct>=90?'ok':pct>=50?'warn':''},
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

function stopGraph(){if(_graphAnim){cancelAnimationFrame(_graphAnim);_graphAnim=null;}}

async function visualizeFullGraph(maxNodes){
  if(!currentProject)return;
  stopGraph();
  $('graph-canvas-info').textContent='Loading graph data…';
  const canvas=$('graph-canvas');const ctx=canvas.getContext('2d');
  ctx.fillStyle='#0b0e1a';ctx.fillRect(0,0,canvas.width,canvas.height);
  let data;
  try{data=await api('/graph_export?project='+encodeURIComponent(currentProject)+'&format=json&max_nodes='+maxNodes);}
  catch(e){$('graph-canvas-info').textContent='Error: '+escHtml(e.message);return;}
  if(data.error){$('graph-canvas-info').textContent='Error: '+data.error;return;}
  const nodes=data.nodes||[];const edges=data.edges||[];const communities=data.communities||[];
  if(!nodes.length){$('graph-canvas-info').textContent='No graph data. Build the project index first.';return;}

  const commColors={};
  const palette=['#7b61ff','#00c28e','#ffb800','#ff4060','#9b6dff','#00d4ff','#fb8f44','#6366f1','#10b981','#ec4899'];
  communities.forEach((c,i)=>{commColors[c.id]=palette[i%palette.length];});

  const idToIdx={};nodes.forEach((n,i)=>{idToIdx[n.id]=i;});
  const cw=canvas.offsetWidth||800,ch=canvas.offsetHeight||460;
  canvas.width=cw;canvas.height=ch;
  const spread=Math.min(cw,ch)*0.4;
  const sim=nodes.map(n=>({
    id:n.id,name:n.name,kind:n.kind,file:n.file,community_id:n.community_id,
    x:cw/2+(Math.random()-.5)*spread,y:ch/2+(Math.random()-.5)*spread,vx:0,vy:0,
    r:n.kind==='file'?4:5,color:commColors[n.community_id]||'#484f58',
  }));
  const adjOut=Array.from({length:nodes.length},()=>[]);
  edges.forEach(e=>{const fi=idToIdx[e.from],ti=idToIdx[e.to];if(fi!==undefined&&ti!==undefined)adjOut[fi].push(ti);});

  // Legend
  const legHtml=communities.slice(0,8).map((c,i)=>
    `<div class="legend-item"><div class="legend-dot" style="background:${palette[i%palette.length]}"></div>${escHtml((c.title||'Community '+c.id).slice(0,20))}</div>`
  ).join('');
  $('graph-legend').innerHTML=legHtml;

  let panX=0,panY=0,scale=1,dragging=false,lastMX=0,lastMY=0;
  canvas.onmousedown=e=>{dragging=true;lastMX=e.clientX;lastMY=e.clientY;canvas.style.cursor='grabbing';};
  canvas.onmouseup=()=>{dragging=false;canvas.style.cursor='grab';};
  canvas.onmouseleave=()=>{dragging=false;$('graph-canvas-tooltip').style.display='none';};
  canvas.onmousemove=e=>{
    if(dragging){panX+=e.clientX-lastMX;panY+=e.clientY-lastMY;lastMX=e.clientX;lastMY=e.clientY;return;}
    const rect=canvas.getBoundingClientRect();
    const mx=(e.clientX-rect.left-panX)/scale,my=(e.clientY-rect.top-panY)/scale;
    let best=null,bestD=20;
    sim.forEach(n=>{const d=Math.hypot(n.x-mx,n.y-my);if(d<bestD){bestD=d;best=n;}});
    const tip=$('graph-canvas-tooltip');
    if(best){tip.innerHTML=`<strong>${escHtml(best.name)}</strong><br>${escHtml(best.kind)} · ${escHtml((best.file||'').split('/').slice(-2).join('/'))}`;
      tip.style.display='block';tip.style.left=(e.clientX+12)+'px';tip.style.top=(e.clientY+8)+'px';
    }else tip.style.display='none';
  };
  canvas.onwheel=e=>{e.preventDefault();const f=e.deltaY<0?1.1:.9;scale=Math.max(.1,Math.min(10,scale*f));};

  const K=Math.sqrt((cw*ch)/(nodes.length||1));
  const REPEL=K*K*1.5,ATTRACT=.4;
  let alpha=1,tick=0;const MAX_TICKS=300;

  function frame(){
    if(tick>=MAX_TICKS)alpha=0;
    if(alpha>.001){
      sim.forEach(n=>{n.fx=0;n.fy=0;});
      const step=nodes.length>500?4:1;
      for(let i=0;i<sim.length;i+=step)for(let j=i+1;j<sim.length;j+=step){
        const dx=sim[j].x-sim[i].x,dy=sim[j].y-sim[i].y,d2=dx*dx+dy*dy+.01;
        const f=REPEL/d2;sim[i].fx-=f*dx;sim[i].fy-=f*dy;sim[j].fx+=f*dx;sim[j].fy+=f*dy;
      }
      adjOut.forEach((tos,fi)=>tos.forEach(ti=>{
        const dx=sim[ti].x-sim[fi].x,dy=sim[ti].y-sim[fi].y,d=Math.hypot(dx,dy)+.01;
        const f=d*ATTRACT/K;sim[fi].fx+=f*dx;sim[fi].fy+=f*dy;sim[ti].fx-=f*dx;sim[ti].fy-=f*dy;
      }));
      sim.forEach(n=>{n.fx+=(cw/2-n.x)*.015;n.fy+=(ch/2-n.y)*.015;});
      sim.forEach(n=>{n.vx=(n.vx+n.fx)*.85;n.vy=(n.vy+n.fy)*.85;n.x=Math.max(10,Math.min(cw-10,n.x+n.vx*alpha));n.y=Math.max(10,Math.min(ch-10,n.y+n.vy*alpha));});
      alpha*=.98;tick++;
    }
    ctx.save();ctx.fillStyle='#0b0e1a';ctx.fillRect(0,0,cw,ch);
    ctx.translate(panX,panY);ctx.scale(scale,scale);
    ctx.strokeStyle='rgba(37,45,74,.6)';ctx.lineWidth=.6/scale;
    adjOut.forEach((tos,fi)=>tos.forEach(ti=>{ctx.beginPath();ctx.moveTo(sim[fi].x,sim[fi].y);ctx.lineTo(sim[ti].x,sim[ti].y);ctx.stroke();}));
    sim.forEach(n=>{ctx.beginPath();ctx.arc(n.x,n.y,n.r,0,2*Math.PI);ctx.fillStyle=n.color;ctx.fill();});
    ctx.restore();
    _graphAnim=requestAnimationFrame(frame);
  }
  $('graph-canvas-info').textContent=`${nodes.length} nodes · ${edges.length} edges · ${communities.length} communities${data.truncated?' (truncated)':''} — drag pan, scroll zoom, hover details`;
  _graphAnim=requestAnimationFrame(frame);
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

async function loadStatus(){
  const[health,metrics]=await Promise.all([fetch('/healthz').then(r=>r.json()),api('/metrics')]);
  $('daemon-metrics').innerHTML=[
    {val:metrics.connected_clients??health.connected_clients??'—',lbl:'Clients'},
    {val:health.active_watchers??'—',lbl:'Watchers'},
    {val:metrics.uptime_s!=null?metrics.uptime_s.toFixed(0)+'s':health.uptime_s!=null?health.uptime_s.toFixed(0)+'s':'—',lbl:'Uptime'},
  ].map(s=>`<div class="stat-box"><div class="val">${s.val}</div><div class="lbl">${s.lbl}</div></div>`).join('');
  $('search-metrics').textContent=JSON.stringify(metrics,null,2);
  loadKBHealth();
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
async function loadServiceMesh(){
  if(!currentProject)return;
  $('service-mesh-content').innerHTML='<div class="loader">Scanning services…</div>';
  $('service-mesh-description').style.display='none';
  try{
    const data=await api('/service_mesh?project='+encodeURIComponent(currentProject));
    if(data.error){$('service-mesh-content').innerHTML=`<div style="color:var(--text-3)">${escHtml(data.error)}</div>`;return;}
    if(data.description){$('service-mesh-description').textContent=data.description;$('service-mesh-description').style.display='block';}
    const services=data.services||[];const edges=data.edges||[];
    const pc={'grpc':'var(--accent)','http':'var(--green)','message_queue':'var(--amber)','database':'var(--purple)'};
    const sHtml=services.map(s=>`<div class="integ-card ${s.protocols.length?'ok':''}">
      <div class="integ-title">📦 ${escHtml(s.name)}</div>
      <div style="display:flex;gap:3px;flex-wrap:wrap;margin-top:3px">
        ${s.protocols.map(p=>`<span class="badge" style="font-size:.69rem;color:${pc[p]||'var(--text-2)'}">${p}</span>`).join('')}
      </div></div>`).join('');
    const eHtml=edges.length?`<div style="margin-top:12px"><div class="card-title" style="margin-bottom:6px">Connections (${edges.length})</div>
      <table><thead><tr><th>From</th><th>Protocol</th><th>To</th></tr></thead><tbody>
      ${edges.slice(0,30).map(e=>`<tr><td>${escHtml(e.from)}</td><td style="color:${pc[e.protocol]||'var(--text-2)'}">${escHtml(e.protocol)}</td><td>${escHtml(e.to)}</td></tr>`).join('')}
      </tbody></table></div>`:'';
    $('service-mesh-content').innerHTML=`<div class="integrations-grid" style="margin-bottom:10px">${sHtml}</div>${eHtml}`;
  }catch(e){$('service-mesh-content').innerHTML=`<div style="color:var(--red);font-size:.81rem">Error: ${escHtml(e.message)}</div>`;}
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

/* ── Boot ────────────────────────────────────────────────────────────────────── */
(async()=>{
  try{
    await loadProjects();
    await loadOverview();
  }catch(e){
    $('daemon-dot').className='daemon-dot err';
    $('daemon-status').textContent='error';
    $('projects-table').innerHTML=`<div style="color:var(--red);padding:10px">Failed to connect to daemon: ${escHtml(e.message)}</div>`;
  }
  // Auto-refresh every 30s for overview and health pages
  setInterval(()=>{
    if(document.querySelector('#page-overview.active'))loadOverview();
    else if(document.querySelector('#page-health.active'))loadStatus();
  },30000);
})();
</script>
</body>
</html>
"""
