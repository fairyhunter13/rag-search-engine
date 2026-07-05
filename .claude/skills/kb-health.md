# kb-health skill

Cross-project knowledge-base quality audit: hierarchy depth, enrichment coverage, storage, watcher state.

## What this skill does

1. Call `overview(what='projects')` to list all registered projects.
2. For each project, call `overview(project_path, what='status')` to collect:
   - Total files, chunks, communities indexed
   - Hierarchy levels (level-1 only = no macro-structure; level 2+ = GraphRAG-ready)
   - Enrichment % (unenriched communities block `ask(scope=global)`)
   - Storage size (sqlite-vec + graph.db combined)
   - Watcher state (watching/stopped)
3. Report a table:

```
Project               Files    Chunks   Communities  Enriched  Storage  Watcher
your-federation-root  ...      ...      ...          100%      ...      watching
rag-search-engine ...     ...      ...          100%      ...      watching
...
```

4. Flag any project with:
   - `enrichment_pct < 100%`: needs KB enrichment trigger via CLI or dashboard
   - Watcher stopped: needs daemon restart or `manage(action="reload")`
   - Storage > 3GB: candidate for `manage(action="vacuum")`

5. Suggest next actions for each flagged project.

## Rules

- Never call `build` automatically — only report what needs attention and let the user decide.
- Use `mcp__rag-search__*` tools only; no Bash grep for this health check.
- If daemon is unreachable, report "Daemon offline — run: systemctl --user status rag-search-mcp-daemon".

## After running

Summarize as:
- "All N projects healthy" — if no flags
- List projects needing action with specific commands to run

Run it now.
