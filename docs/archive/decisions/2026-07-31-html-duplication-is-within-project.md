# 87.8% of the `.html` corpus is byte-identical copies, and almost all of it is one project's

**2026-07-31** · P3 · `scripts/survey_duplicate_files.py`, `index/discover.py`

`.html` is the largest remaining corpus cost — 43,397 chunks, 10.2% of the fleet. The standing
explanation was "one purchased theme vendored across sibling repos", and the standing proposal was
an extension rule in `_should_drop`. Both were assumptions. The survey now in `scripts/` measures
them, and it changes the design rather than confirming it.

```
.html files scanned          : 6494
distinct contents            :  794
within-project reclaim       : 5546  (85.4%)
cross-fleet reclaim          : 5700  (87.8%)
extra bought by going cross  :  154
projects with within-dupes   :   12
```

## The seam is within-project, and the number says so without argument

P3 makes federation members independent stores. A cross-fleet dedupe breaks that: one member's
results would depend on what some *other* member happened to index, and de-registering a project
would silently change a third project's search. That is a real coupling and it has to earn itself.

It buys **154 files** — 2.4% of what the within-project rule already gets, 0.4% of the corpus.
Not close. **Within-project is the seam**, and the question is closed.

## It is also not a theme, and not spread out

The group-size histogram is bimodal: 467 groups of exactly 2 copies (ordinary noise, ~8% of the
reclaim) and then a wall at 25–27 — **110 groups at exactly 25 copies**. That is the shape of a
vendored library's `examples/` or `demo/` directory duplicated per install, not of a shared theme.
The two genuinely cross-project groups (999 copies across 10 projects, 326 across 11) are the only
part that matches the original story, and they are the part a within-project rule already reclaims
almost entirely, because those copies are also duplicated *inside* each project.

One project accounts for **5,142 of the 5,546** (92.7%). Nine of the remaining eleven are under 75
copies each. So this is not a fleet-wide tax to be repaid by a fleet-wide mechanism — it is one
repository, and any rule that lands should be measured against the possibility of simply excluding
that tree in its project config, which costs no code at all.

## `_should_drop` cannot host it, and the reason is written in the file

Every rule in `_should_drop` (`index/discover.py:144`) is a pure predicate on one path: exclusion
globs, `is_generated_path`, `is_secret_path`, `is_image_path`, hidden segments, gitignore, size.
Duplication is not a property of a path. It is a property of a path *and everything already seen*,
so there is nowhere in that function for it to go.

Moving it to the indexer instead is worse, and the comment at `discover.py:181-190` says why. That
function is shared deliberately: the watcher's screen, the indexer, and the drift gate must apply
**one** rule, because when they disagreed on the size cap the result was a churn loop — a file past
the cap was watched and indexed, reported orphaned by the set-drift check, purged, and re-indexed on
its next write. A dedupe that lives only in the indexer reproduces that failure exactly: discovery
would keep reporting all 25 copies as indexable, and the drift gate would keep restoring the 24 the
indexer dropped.

So the shared decision has to become stateful across one project walk. That is the actual work, and
it is larger than the "extension rule" this was filed as.

## Two things that must be settled before any of it is written

- **Which copy survives has to be deterministic.** `iter_files` walks with `os.walk`
  (`discover.py:490`) in filesystem order, which is not stable across machines or across a
  re-clone. "First one wins" therefore makes the survivor arbitrary, and two walks that pick
  differently are the churn loop again with extra steps. A total order — shortest relative path,
  ties broken lexically — has to be part of the rule, not left to the walk.
- **Where the hash lives.** Hashing 6,494 files costs a full read of each on every walk. The size
  rule is last in `_should_drop` precisely because it costs a `stat()`; a `sha256` of the body is
  far more expensive than that, and it would run on every discovery pass, not just on the
  duplicated extension.

## Not scheduled

`.html` is a code language today, so any rule that changes what is indexed moves
`_code_source_fingerprint` and buys a full re-derive of every store. The `e7`+`fg3` stamp move has
just been paid for and converged; this one waits for the next stamp rather than triggering its own.
The survey is committed so the numbers do not have to be re-derived when it does.

## Addendum — 2026-08-01: retired by measurement

Everything above was true when recorded and one day later the premise is gone. Re-measured over all
150 stores:

| | at the survey | today |
|---|---:|---:|
| `.html` files indexed | 6,494 | **605** |
| `.html` chunks | 43,397 | **2,631** |
| share of fleet corpus | 10.2% | **0.87%** — 13th extension, behind `.groovy` at 1.10% |
| within-project reclaim | 5,546 files | **404 files / 459 chunks = 0.15%** |
| projects with within-dupes | 12 | 11 |

**The cause is the option this document proposed and then set aside.** The body says any rule
"should be measured against the possibility of simply excluding that tree in its project config,
which costs no code at all". That is what happened: the project holding 92.7% of the duplication now
excludes the vendored trees in its `.rse-index.yaml`, deliberately keeping the part it authors. No
code, no stamp, and the tax it was going to repay is gone.

**The shape is gone too, not just the size.** The wall of *110 groups at exactly 25 copies* — the
evidence that this was a vendored `examples/` tree — no longer exists. The histogram is now 42 groups
of 2, a long thin tail, and two groups of 59 at the top. The bimodality that made a batch rule
attractive was a property of the excluded trees.

**And the trigger it was waiting for has already come and gone.** "Waits for the next stamp" was
written against `e7`; the fleet is on `e8` as of `d392193`, and nothing here rode along with it.
A future reader must not treat that sentence as a pending window.

**Stream 4 is retired.** Not deferred — the work is a stateful rule inside the one function the
watcher, the indexer and the drift gate must all agree on, and the case for paying that is
**459 chunks, 0.15% of the corpus**. The two open design questions in the section above (deterministic
survivor order; where the hash lives) are still the right questions and are still unanswered; they
are simply not worth answering at this price. Same disposition `21d0880` used for T1/T2: anyone
reviving this re-measures first, because this document has now been wrong about its own headline
once.
