export const meta = {
  name: 'storage-optimize',
  description: 'Per-project LanceDB + graph vacuum across all registered projects; reports MB freed per project',
  phases: [
    { title: 'Discover', detail: 'List all registered projects' },
    { title: 'Vacuum', detail: 'Run vector + graph vacuum per project in parallel' },
    { title: 'Report', detail: 'Summarize storage freed and flag projects needing hierarchy build' },
  ],
}

const DRY_RUN = (args && args.dry_run === true) || false
const MIN_SAVINGS_MB = (args && args.min_savings_mb) || 0

const VACUUM_SCHEMA = {
  type: 'object',
  properties: {
    project_path: { type: 'string' },
    project_name: { type: 'string' },
    vector_saved_mb: { type: 'number' },
    graph_saved_mb: { type: 'number' },
    total_saved_mb: { type: 'number' },
    before_mb: { type: 'number' },
    after_mb: { type: 'number' },
    orphans_removed: { type: 'number' },
    error: { type: 'string' },
    skipped: { type: 'boolean' },
    skip_reason: { type: 'string' },
  },
  required: ['project_path', 'project_name', 'total_saved_mb'],
}

// Phase 1: Discover all registered projects
phase('Discover')
log('Listing all registered projects...')

const projectsResult = await agent(
  `Call overview(what="projects") using the mcp__opencode-search__overview tool.
Return the full list as JSON with fields: project_path, project_name, indexed_at, file_count.
If none registered, return empty list.`,
  {
    label: 'discover-projects',
    phase: 'Discover',
    schema: {
      type: 'object',
      properties: {
        projects: {
          type: 'array',
          items: {
            type: 'object',
            properties: {
              project_path: { type: 'string' },
              project_name: { type: 'string' },
              indexed_at: { type: 'string' },
              file_count: { type: 'number' },
            },
            required: ['project_path'],
          },
        },
      },
      required: ['projects'],
    },
  }
)

if (!projectsResult || !projectsResult.projects || projectsResult.projects.length === 0) {
  log('No projects registered. Nothing to vacuum.')
  return { total_saved_mb: 0, projects_vacuumed: 0, message: 'No registered projects found.' }
}

const projects = projectsResult.projects
log(`Found ${projects.length} registered project(s): ${projects.map(p => p.project_name || p.project_path.split('/').pop()).join(', ')}`)

// Phase 2: Vacuum each project in parallel
phase('Vacuum')
log(`Running ${DRY_RUN ? 'dry-run' : 'live'} vacuum on ${projects.length} project(s)...`)

const vacuumResults = await pipeline(
  projects,
  async (project, _orig, idx) => {
    const pname = project.project_name || project.project_path.split('/').pop()
    const result = await agent(
      `You are vacuuming the opencode-search index for project: ${project.project_path} (name: ${pname}).
${DRY_RUN ? 'This is a DRY RUN — report expected savings but do not actually delete anything.' : 'This is a LIVE vacuum — actually run the vacuum operations.'}

Run these steps:
1. Call manage(project_path="${project.project_path}", action="vacuum"${DRY_RUN ? ', dry_run=True' : ''}) using the mcp__opencode-search__manage tool.
   This removes orphan index tier directories (index_budget, index_balanced, etc.).
   Record: orphans_removed, mb_freed from the result.

2. Extract before/after storage sizes from the vacuum result.

3. Estimate vector_saved_mb and graph_saved_mb if available, or set both to 0 if not reported separately.

Return: project_path="${project.project_path}", project_name="${pname}",
  vector_saved_mb, graph_saved_mb, total_saved_mb (sum of all freed),
  before_mb, after_mb, orphans_removed.
If any error occurs, set error=<message> and total_saved_mb=0, skipped=false.
If project is actively being indexed (watcher busy), set skipped=true, skip_reason="indexing in progress".`,
      {
        label: `vacuum:${pname}`,
        phase: 'Vacuum',
        schema: VACUUM_SCHEMA,
      }
    )
    return result
  }
)

// Phase 3: Report
phase('Report')

const valid = vacuumResults.filter(Boolean)
const vacuumed = valid.filter(r => !r.skipped && !r.error)
const skipped = valid.filter(r => r.skipped)
const errors = valid.filter(r => r.error && !r.skipped)
const totalSaved = vacuumed.reduce((sum, r) => sum + (r.total_saved_mb || 0), 0)
const significant = vacuumed.filter(r => r.total_saved_mb >= MIN_SAVINGS_MB)

log(`Vacuum complete: ${totalSaved.toFixed(1)} MB freed across ${vacuumed.length} projects`)

const summary = [
  `## Storage Optimization Report — ${DRY_RUN ? 'DRY RUN' : 'LIVE'} — ${projects.length} projects`,
  '',
  `**Total freed: ${totalSaved.toFixed(1)} MB** (${(totalSaved / 1024).toFixed(2)} GB)`,
  `Projects vacuumed: ${vacuumed.length} | Skipped: ${skipped.length} | Errors: ${errors.length}`,
  '',
  '### Per-project results',
  '',
  '| Project | Before | After | Freed | Orphans | Status |',
  '|---------|--------|-------|-------|---------|--------|',
  ...vacuumed.map(r => {
    const name = r.project_name || r.project_path.split('/').pop()
    const saved = (r.total_saved_mb || 0).toFixed(1)
    const before = r.before_mb ? r.before_mb.toFixed(0) + 'MB' : '?'
    const after = r.after_mb ? r.after_mb.toFixed(0) + 'MB' : '?'
    const orphans = r.orphans_removed || 0
    return `| ${name} | ${before} | ${after} | ${saved} MB | ${orphans} | ✅ |`
  }),
  ...skipped.map(r => {
    const name = r.project_name || r.project_path.split('/').pop()
    return `| ${name} | — | — | — | — | ⏸ ${r.skip_reason || 'skipped'} |`
  }),
  ...errors.map(r => {
    const name = r.project_name || r.project_path.split('/').pop()
    return `| ${name} | — | — | — | — | ❌ ${r.error} |`
  }),
]

return {
  dry_run: DRY_RUN,
  projects_total: projects.length,
  projects_vacuumed: vacuumed.length,
  projects_skipped: skipped.length,
  projects_errored: errors.length,
  total_saved_mb: totalSaved,
  total_saved_gb: totalSaved / 1024,
  summary: summary.join('\n'),
  details: valid,
}
