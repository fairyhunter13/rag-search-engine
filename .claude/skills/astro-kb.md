# kb skill

Dedicated verification of your federation root's knowledge base: exercises all MCP tool surfaces,
checks KB quality, and confirms recall is working end-to-end.

## What this skill does

Run these checks in order, reporting pass/fail for each:

### 1. Index health
```
overview(project_path="<YOUR_FEDERATION_ROOT>", what="status")
```
Verify: files > 0, chunks > 0, communities > 0, watcher = watching.

### 2. Search recall — code lookup
```
search("route handler", project_paths=["<YOUR_FEDERATION_ROOT>"])
search("database connection pool", project_paths=["..."])
```
Verify: each returns ≥3 results with file paths that exist.

### 3. Ask — architecture understanding
```
ask("how does the API routing work?", project_path="...", scope="all")
ask("describe the overall architecture", project_path="...", scope="global")
```
Verify: responses reference real file paths, not generic filler.

### 4. Ask — feature trace (scope=feature)
```
ask("how does authentication work end-to-end?", project_path="...", scope="feature")
```
Verify: response traces entry points → call chain → data storage.

### 5. Graph — call analysis
```
graph("main", project_path="...", relation="callees")
```
Verify: returns symbols, not "symbol not found".

### 6. Community structure
```
overview(project_path="...", what="communities")
```
Verify: communities with enriched titles (not "Community 1234").

## Report format

```
Federation Root KB Health

✅ Index:       N files / M chunks / K communities
✅ Search:      route handler → N results; pool → M results
✅ Ask all:     returns grounded file references
✅ Ask global:  synthesis across communities
✅ Ask feature: traces entry points → data storage
✅ Graph:       main → N callees
✅ Communities: N enriched / 0 unenriched

Overall: N/N PASS  (or N/M PASS — list failures)
```

## Rules

- No mocks. All checks use live MCP calls to the real daemon + real GPU.
- If daemon is down: report "Daemon offline" and stop — do not attempt restart here.
- Only call rebuild actions if the user explicitly asks after seeing the report.

Run it now.
