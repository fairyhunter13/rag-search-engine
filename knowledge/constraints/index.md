# Constraint

* [`watching` answers for one project, and arming lands seconds after
  registration](watching-is-per-project-and-arming-is-asynchronous.md) - The `index` reply's
  `watching` field used to be thread liveness. That is true the moment the watcher starts, and
  says nothing about whether this project's inotify watches exist. It is `watch.armed(project)`
  now, which is membership in the armed set. The rebuild lands up to WATCH_POLL_MS plus ~5 s after
  registration, and a write inside that window is gone for good.
* [A cached SQLite handle is not free at rest, and the cache was per thread and per
  project](a-sqlite-handle-is-not-free-at-rest.md) - Every connection carries its own page cache,
  2.12 MB once filled at the default. The daemon held 1,506 handles over 415 stores on four threads
  and closed none, which is 3.2 GB of the 3.5 GiB it was resident at.
* [A fleet alert decides on two samples, because every field in the row describes the wrong
  span](a-fleet-alert-decides-on-two-samples-not-on-a-count.md) - `last_error` is cleared by the
  hourly sweep, `last_error_at` is restamped by every failure, and `error_total` never resets. So
  no single read of the registry separates a transient failure from a stuck project. The checker
  holds the previous failing set instead.
* [A model carries three things, and pooling is the one that fails
  invisibly](a-model-carries-three-things-and-pooling-is-the-invisible-one.md) - Prefixes, context
  limit and pooling all travel with an embedding model. Only pooling produces a plausible unit
  vector when it is wrong. So a mispooled arm reads as the model losing rather than as a bug.
* [A new language is indexed with no change, and the price is that it cannot be named in a
  filter](a-new-language-costs-nothing-and-cannot-be-filtered.md) - Discovery is a denylist, so a
  .zig file is chunked, embedded and searchable the day the language exists. It carries lang=""
  and is reachable only by leaving the lang filter unset. That used to return an empty result
  instead of saying so.
* [A watch batch carries one event, and each one paid for a whole
  walk](a-watch-batch-carries-one-event.md) - The watcher submitted per batch, so an editor saving
  through a build cost 303 index passes in 15 minutes. A pass is a full content-hash walk whatever
  moved. A 15 s per-project quiet window merges them, and the freshness it gives up is the window.
* [An eval query is removed from the corpus it is hunting, and the corpus contains what the arm
  changes](an-eval-query-must-not-be-findable-by-identity.md) - Leaving the lead block in the
  indexed copy makes every arm score near 1.000 on string identity. An arm whose file type is
  absent from the query set cannot be read at all. Those are the two ways this harness lies
  quietly.
* [Joining a root is a configuration change to an existing project, and it has to reach the
  index](joining-a-root-is-a-config-change-to-an-existing-project.md) - One registry row per
  resolved path records why it is there. A root that claims a project widens or narrows its
  effective excludes. The config signature turns the next pass into a reconcile rather than a
  no-op.
* [Nine nodes run on the CPU EP in both exports, so the GPU rule is written about tensor math rather than about nodes](nine-nodes-run-on-the-cpu-and-that-is-the-design.md) - `session.disable_cpu_ep_fallback` was measured and refuses both models outright over shape plumbing ORT pins to CPU on purpose. The enforcement that shipped is an op allowlist plus a 1% time bound, because either half alone is defeated.
* [No quantity of served prose makes a client prefer this server over its own
  grep](served-prose-does-not-beat-grep.md) - Three escalations of the MCP instructions and the
  tool description all lost to grep on a literal-string question in a navigable repo. So the agent
  layer asks the one question the client's own tools cannot answer, and asserts on the transcript.
* [On 2026-07-28 the caller identifies itself per request, so nothing about it may be read from a
  session](who-called-is-a-per-request-read.md) - The handshake is gone and this daemon is
  stateless_http=True. So `client_params` is a fallback and not a source. clientInfo, capabilities
  and the protocol version all arrive in `params._meta` on every call. Three defects in a row came from reading a session that was never there, or from writing to a context the resolver boundary throws away.
* [The CPU side of an indexing pass is already flat, and six arms failed to move
  it](the-cpu-side-of-an-indexing-pass-is-already-flat.md) - Measured over three repetitions per
  arm on a quiet machine, an indexing pass costs ~1.1 mean cores. None of MALLOC_ARENA_MAX,
  RAYON_NUM_THREADS, intra/inter-op thread caps, spin-wait or the CPU arena cleared its
  pre-committed threshold. MALLOC_ARENA_MAX=2 made it 27% worse.
* [The embed batch ceiling read as adaptive and was a constant, and 32 is where the VRAM stops
  falling](the-embed-batch-ceiling-read-as-adaptive-and-was-a-constant.md) - `adaptive_batch` never
  bound below its ceiling on a 16 GB card, so the live batch was always 128. Swept over 128/64/32/16:
  128 and 64 both hold 4,440 MiB, 32 holds 2,392 and 16 holds 2,398. The floor is the weights, so the
  −50% gate was unreachable by construction and is recorded as failed rather than moved.
* [The search unit is the caller's own workspace plus what it federates, and that is containment rather than authorization](the-search-unit-is-the-callers-own-workspace.md) - The root was a string the model wrote, so any of ~159 registered projects was reachable from any session by naming it. The boundary now comes from the client's own roots, through a parameter the model cannot see — and the honest claim for it is narrow.
* [This host cannot produce an admissible latency number while it
  throttles](this-host-cannot-produce-an-admissible-latency-number.md) - The laptop's GPU runs at
  a sixth of rated clock. So every timing figure measured here measures the host rather than the
  engine. The cause is a power cap, not the heat the first reading blamed.
* [What a root and 135 federated members cost, and why MemoryHigh fires 404 times without
  hurting](what-a-root-and-135-members-cost.md) - Measured 2026-08-20 against the live daemon: 12
  searches over a 135-member federation, p50 1.46 s, 91% of one core while working, 2.31 GiB anon.
  The cgroup sits at its 4 GiB MemoryHigh and has logged 404 high events. The total memory stall
  across the process's life is 27 ms, because the excess is page cache.
* [The tracked tree is clean because the history was rewritten twice](the-public-history-was-rewritten-twice.md) - Two `filter-repo` rewrites made this public history publishable, on 2026-06-19 and 2026-08-28. `--replace-text` left every banned term in the commit messages. The tree looks clean because it was made clean, and none of that is derivable from the repo.
