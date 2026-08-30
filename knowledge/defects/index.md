# Defect

* [1768 zero-root pins were an answered-empty case the log could not
  name](empty-root-answers-from-clients-nothing-identifies.md) - Of ~1865 zero-root pins only 97
  took the no-capability branch and none took the protocol-era branch. The rest asked and got an
  empty list back, a third case named nowhere. No `clientInfo` was logged, so the flag's rollout
  criterion was gated on evidence the instrumentation could not produce.
* [A committed file symlink was read through, in the lane that runs by
  default](the-git-lane-read-through-a-file-symlink.md) - `git ls-files` lists a file symlink as
  an ordinary path, and `is_file()` follows it. So a repo containing `notes.md ->
  ~/private/notes.md` indexed that content and attributed it to the containing project. The walk
  lane refuses it, the git lane accepts it, and the git lane is the default.
* [A dead row paged hourly, and no command could remove
  it](a-dead-row-paged-hourly-and-nothing-could-remove-it.md) - Twenty rows pointing at deleted
  temp directories re-failed on every sweep, so the two-sample rule pages forever. `doctor` named
  them, `--prune` reached only stores, and the recorded fix was editing projects.json by hand.
  `HEALTH_FAILING_CAP` is 20, so the alert was saturated with junk and could not have shown a real
  failure.
* [A delete is lost while a project is still settling, and delivered in 10 s once it
  has](a-delete-is-lost-while-a-project-is-still-settling.md) - The same tree, the same shape, the
  same fleet. A file deleted shortly after the project was registered stayed searchable past 300
  s. One deleted after a 90 s quiet period left the store in 10 s. The settling window was the probe's own doing -- the `index` tool re-armed every inotify watch on every call, and the test polled it.
* [A deleted file stayed searchable in a federated member, from two independent causes](a-deleted-file-stayed-searchable-in-a-member.md) - inotify keys a directory by inode and reports the last-registered path, so a member's writes were billed to its root. And the 60 s tick re-armed the whole watch set unconditionally, which is a 5.4 s blind window with no replay.
* [A deliberate stop was indistinguishable from a crash, because the exit that avoids the crash
  was unreachable](a-deliberate-stop-was-indistinguishable-from-a-crash.md) - uvicorn waited on
  MCP's open streams, so systemd SIGKILLed at 90 s and fired OnFailure on every ordinary stop. The
  existing restart tests could not see it, because their stop helper kills after 30 s and reports
  success.
* [A derived column was stored and never re-derived, so widening LANGS reclassified nothing](a-derived-column-was-stored-and-never-re-derived.md) - `files.lang` comes from the path alone, but it is written once at read time and the content-hash diff never rewrites an unchanged file. Growing LANGS from 40 to 166 extensions reached zero of the 2,058 `.groovy` files already indexed. The no-language bucket only fell because `.svg` was deleted from the index, which made the total look like progress.
* [A failure that resolved itself left no trace, and liveness called that
  healthy](a-failure-that-resolved-itself-left-no-trace.md) - `last_error` was one overwritten
  string cleared by the next success, and the reconcile sweep supplies a success every hour. So a
  project failing every sweep for a week read clean at every moment anyone looked. Meanwhile
  `coderag-alert@.service` checked only `is-active`.
* [A floating range on onnxruntime-gpu changed the CUDA major version](a-floating-range-changed-the-cuda-major.md) - A resolver bump from onnxruntime-gpu 1.26 to 1.29 changed the linked CUDA major from 12 to 13 against a cu12 wheel set. Every GPU test failed on a missing libcublasLt.so.13 and the session fell back to CPU.
* [A member answered alone, and the reply read like the
  federation's](a-member-answered-alone-and-read-like-the-federation.md) - A search from a
  member's own directory covered 1 project of 143. The reply carried no sign of it, and the caller
  had no second call to make: `enforce` refuses a member that names its root.
* [A populated submodule was invisible to discovery, so a third of the Gen-3 PHP corpus was never
  indexed](a-submodule-is-invisible-to-discovery.md) - `git ls-files` lists a gitlink as one entry
  and never descends, so every file inside a checked-out submodule was absent from the index. 22
  Acme worktrees hold one, and 2,584 PHP files sat behind them. `--recurse-submodules` is not
  the fix, because git refuses it beside `--others`.
* [A project whose directory was gone could not be turned
  off](a-vanished-project-could-not-be-turned-off.md) - `index_project` refused every call for a
  path that is not a directory, and the refusal sat above the unflag branch. So the row an
  operator most needs to disable was the one row the surface could not act on. Two of them survived every restart, and reconcile retried and logged a traceback for each at every start.
* [A released member kept the excludes of the root that released
  it](a-released-member-kept-the-roots-excludes.md) - Joining a root narrows a member and submits
  it for re-index. Leaving one widens it and submitted nothing. Unregister reported only the rows
  it deleted, so the one member that survives the release was the one it never named.
* [A rootless call with no pin resolved to the daemon's cwd, which is
  $HOME](a-rootless-call-told-the-caller-to-index-their-home-directory.md) - `scope.default_root`
  fell back to `Path.cwd()` when the client sent no roots. The daemon's cwd is the operator's home
  directory. So a real Claude Code session's rootless `search` came back as "$HOME is not indexed
  -- call index(root=$HOME) first". Taken, that advice indexes everything on the machine. It now refuses and names the fix.
* [An unflagged project stayed fully searchable, because the gate asked whether the row existed](an-unflagged-project-stayed-fully-searchable.md) - `registry.get` returns disabled rows and unflagging deliberately deletes no store, so a project the user explicitly turned off answered searches by name. The error string had been claiming three conditions the code never checked.
* [doctor --prune globbed fresh directories against a stale row
  snapshot](prune-raced-a-store-the-daemon-was-writing.md) - `registry.load()` releases its lock
  before returning, so the claimed set was a snapshot and the glob after it was live. A project
  claimed in between read as unclaimed, and its store was deleted under the daemon's open handle.
  On Linux the daemon keeps committing into the unlinked inode and reports nothing.
* [doctor walked rows to stores and never stores to rows](doctor-only-walked-the-registry.md) -
  Every plan in the chain verified with `coderag doctor reports no orphans`. It could not see 144
  index directories, 436 MiB, whose rows were gone. A row-driven walk starts from a row, and these
  had none.
* [One live test skipped the disable-don't-prune teardown, and it was the whole of doctor's
  red](a-live-test-skipped-the-disable-teardown.md) - The suite's rule is that a live test
  disables what it registers and never prunes. Ten tests in the module did. One did not. Its two
  leaked rows were the entire 151-enabled/149-indexed gap, the entire `failed: 2` on `/healthz`,
  and the two `MISSING` lines `doctor` exited 1 on.
* [The chunk budget and the token window do not fit each
  other](chunks-are-cut-before-the-model-sees-them.md) - 2,000 non-whitespace chars produces
  chunks of p50 904 and p95 1373 tokens against a 768-token window. So 70% of chunks are cut, and
  two arms then showed the cut costs no recall.
* [The CLI ran the search in its own process, so it built a second CUDA session](the-cli-loaded-a-second-model-beside-the-daemon.md) - `coderag search` called `search.search` directly, which loaded both models into the CLI process beside the daemon's copy. The card is 16303 MiB, the daemon holds 7660 MiB and one CLI search adds 3114 MiB, so a third consumer exhausts it and `cublasCreate` fails. The CLI asks the daemon now, and falls back to a local search only where nothing answers.
* [The ignore list only ever matched at the root, because fnmatch anchors](the-ignore-list-only-ever-matched-at-the-root.md) - `node_modules/*` matched `node_modules/a.js` and not `packages/a/node_modules/a.js`, and the same for vendor, dist, build, target, `__pycache__` and .git. 278 of 70,218 indexed files sat under a nested copy of a directory the list claims to exclude. Gitignore's own spelling for that list is `node_modules/`, with no leading slash, which matches at any depth.
* [The index reply graded a project by one row of 143](the-index-reply-graded-a-project-by-one-row-of-143.md) - Every action takes the unit to be the root together with its members. The status the same tool returns did not: `indexed` was the root's own row, 33,053 chunks of the 185,453 the project answers from.
* [The pool cut starved the members it was built to
  reach](the-pool-cut-starved-the-members-it-was-built-to-reach.md) - `limit` was 60, so 337
  members shared the 30 slots the caller's own half left, in federation order. 307 projects never
  reached the reranker, and the leaf holding the answer was one of them. `limit` is a floor now,
  and every project with a candidate contributes its best hit for about 3.2 s more.
* [The pre-rerank cut ranked 136 projects by scores that are only meaningful in
  one](per-project-ranks-were-truncated-as-if-comparable.md) - RRF fuses lanes inside a project,
  so its scores are per-project ranks: every project's rank-1 hit scores about the same.
  Truncating the flat pool to CANDIDATES by that score keeps roughly everyone's top one. It drops
  the caller's own rank-3 hit before the reranker sees it, and with rerank=False that order is
  final.
* [The publishability guard read a variable nothing set, then a list nothing could
  match](the-guard-read-a-variable-nothing-set.md) - Two independent failures in one guard. The
  installer wrote a differently-named variable. Once that was fixed, it wrote a colon-joined value
  into a comma-split reader. The first is loud, the second is green.
* [The scheduler suppressed its own death, and it is the one failure no registry row can
  carry](the-scheduler-suppressed-its-own-death.md) - `contextlib.suppress(Exception)` around the
  hourly sweep and the per-tick watcher rearm dropped the exception with no log line. The sweep is
  what would have written the registry row. So a freshness mechanism that raised every hour left
  `/healthz` green and every project reading clean.
* [The secret filter had a dash-shaped hole, and three files went through
  it](the-secret-filter-had-a-dash-shaped-hole.md) - `*.env` needs a literal dot, so `laravel-env`
  was indexed with 287 value-bearing KEY=value lines, `env-template` with 197 and `.env-example`
  with 9. `*.template` does not match `env-template` either, so the two templates were missed
  rather than exempted. Indexed by accident, which is the same outcome as the one that is not.
* [The stop hung again, because a canceled task group waits for a thread it cannot
  cancel](a-cancelled-task-group-cannot-reach-a-shielded-thread.md) - timeout_graceful_shutdown
  bounds the connection wait and nothing else. The 90 s was spent in the lifespan shutdown,
  waiting on a plain-`def` tool running under anyio's shield. The tool restarted the threads the
  finally block had just stopped.
* [The watchdog ping rode the job loop, so slow and dead were the same
  observation](liveness-rode-the-job-loop.md) - `_tick` sent `WATCHDOG=1` as its first statement
  and then ran every job, and `WatchdogSec` was three of those ticks. A job slower than the
  remaining budget therefore reads as a dead process. systemd killed a working daemon at exactly
  180 s, five times, and each restart re-entered the load that caused it.
* [The watcher went blind in two ways, and reported
  neither](the-watcher-went-blind-in-two-ways-and-said-neither.md) - A project dropped for a broken
  config never re-armed: the repair moves no registry row, and the row was the whole comparison.
  And an `OSError` out of inotify killed the thread, which `server._guarded` does not wrap. The
  checker read 2 of the 12 fields `/healthz` publishes, so both read as healthy.
* [The watcher woke the indexer for files git ignores, forever](the-watcher-woke-the-indexer-for-files-git-ignores.md) - `discover.indexable` is the shared predicate, but it never sees gitignore -- the indexer gets that from `git ls-files --exclude-standard`. So a gitignored build cache, which no pass will ever index, submitted a full-project job on every write to it. Caught mid-fleet-index: `3 changes detected` every ~4 s and a queue that grew while the worker drained it.
* [watch.armed() answered for projects the watcher had just refused to
  watch](armed-reported-the-unfiltered-set.md) - `_armed` was assigned the unfiltered intent list.
  So a project dropped four lines earlier for an unparseable config still reported `watching:
  True` through `tools._status`. The live federation test waits on exactly that flag, so its green
  never proved an inotify watch existed.
