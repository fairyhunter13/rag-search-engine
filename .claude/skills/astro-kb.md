# acme-kb skill

Dedicated verification of the redacted-name-3 knowledge base: exercises all 7 MCP tool surfaces,
checks hierarchy quality, and confirms recall is working end-to-end.

## What this skill does

Run these checks in order, reporting pass/fail for each:

### 1. Index health
```
overview(project_path="<ACME_PROJECT_PATH>", what="status")
```
Verify: files > 15000, chunks > 80000, communities > 1000, watcher = watching.

### 2. Hierarchy quality
```
overview(project_path="...", what="hierarchy")
```
Verify: level 2+ communities exist (not all level=1).
If only level=1: report "Hierarchy not built — run: build(action='hierarchy') then build(action='enrich_hierarchy')"

### 3. Architecture domains
```
overview(project_path="...", what="architecture_domains")
```
Verify: returns ≥3 named top-level domains (not empty / error).

### 4. Search recall — code lookup
```
search("route handler", project_paths=["<ACME_PROJECT_PATH>"])
search("database connection pool", project_paths=["..."])
```
Verify: each returns ≥3 results with file paths that exist.

### 5. Ask — architecture understanding
```
ask("how does the API routing work?", project_path="...", scope="all")
ask("describe the overall architecture", project_path="...", scope="global")
```
Verify: responses reference real file paths, not generic filler.

### 6. Ask — feature trace (scope=feature)
```
ask("how does authentication work end-to-end?", project_path="...", scope="feature")
```
Verify: response traces entry points → call chain → data storage.

### 7. Graph — call analysis
```
graph("main", project_path="...", relation="callees")
```
Verify: returns symbols, not "symbol not found".

### 8. Community structure
```
overview(project_path="...", what="communities")
```
Verify: ≥10 communities with enriched titles (not "Community 1234").

## Report format

```
redacted-name-3 KB Health — 2026-06-10

✅ Index:       20064 files / 97480 chunks / 1761 communities
✅ Hierarchy:   3 levels (level-1: 1761, level-2: 42, level-3: 8)
✅ Arch domains: 5 domains (API layer, DB, Auth, Frontend, Config)
✅ Search:      route handler → 12 results; pool → 8 results
✅ Ask all:     returns grounded file references
✅ Ask global:  synthesis across 42 level-2 communities
⚠️ Ask feature: "auth" trace incomplete — missing DB write path
✅ Graph:       main → 34 callees
✅ Communities: 1761 enriched / 0 unenriched

Overall: 8/8 PASS  (or N/8 PASS — list failures)
```

## Rules

- No mocks. All checks use live MCP calls to the real daemon + real GPU.
- If daemon is down: report "Daemon offline" and stop — do not attempt restart here.
- Only call `build` if the user explicitly asks after seeing the report.

Run it now.
