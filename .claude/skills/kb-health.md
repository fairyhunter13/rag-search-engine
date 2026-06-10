# kb-health skill

Cross-project knowledge-base quality audit: hierarchy depth, enrichment coverage, storage, watcher state.

## What this skill does

1. Call `overview(what='projects')` to list all registered projects.
2. For each project, call `overview(project_path, what='status')` to collect:
   - Total files, chunks, communities indexed
   - Hierarchy levels (level-1 only = no macro-structure; level 2+ = GraphRAG-ready)
   - Enrichment % (unenriched communities block `ask(scope=global)`)
   - Storage size (LanceDB + graph.db combined)
   - Watcher state (watching/stopped)
3. Report a table:

```
Project               Files    Chunks   Communities  Levels  Enriched  Storage  Watcher
redacted-name-3         20064    97480    1761         1-3     100%      1.2GB    watching
opencode-search-engine  890    11200     312         1-2     100%      185MB    watching
...
```

4. Flag any project with:
   - `level_max == 1`: needs `build(action="hierarchy")` then `build(action="enrich_hierarchy")`
   - `enrichment_pct < 100%`: needs `build(action="enrich")` or `build(action="enrich_hierarchy")`
   - Watcher stopped: needs daemon restart or `manage(action="reload")`
   - Storage > 3GB: candidate for `manage(action="vacuum")`

5. Suggest next actions for each flagged project.

## Rules

- Never call `build` automatically — only report what needs attention and let the user decide.
- Use `mcp__opencode-search__*` tools only; no Bash grep for this health check.
- If daemon is unreachable, report "Daemon offline — run: systemctl --user status opencode-search-mcp-daemon".

## After running

Summarize as:
- "All N projects healthy" — if no flags
- List projects needing action with specific commands to run

Run it now.
