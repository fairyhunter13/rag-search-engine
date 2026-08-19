# The name guard would have published the list

**2026-08-05** · P18/HR34 · mechanism: `src/tests/live/test_public_hygiene.py`, `.github/workflows/ci.yml`

`test_no_banned_device_names` exists so that no company/project/device name reaches a public
place. It carries the `live` marker, so the `live-fast` job runs it on **every push**. Its failure
message printed the offending lines verbatim:

```python
f"Banned device/company/project name(s) found in {len(hits)} tracked line(s) …:\n"
+ "\n".join(hits[:10])
```

GitHub Actions logs on a public repository are world-readable and permanent. So on the one event
the guard exists to detect — a banned name entering the tree — it would have written up to ten of
those names, with paths and line numbers, into a public log. The tree would then be cleaned by the
very run that published the list.

Nothing had triggered it, because `RSE_NAME_BAN` has never been set on this device: the guard has
been iterating an empty token list since it shipped on 2026-08-04.

## How the surface was found

Not by reading the guard. By reading a log. The most recent `live-fast` run
(`gh run view --log`) contains:

```
Runner name: '<the self-hosted runner's own name>'
/home/<user>/…/src/rag_search/core/gpu.py:109: FutureWarning: The pynvml package is deprecated
```

(Both genericized here, because this file is tracked and the log is not.) 45 `/home/` lines in one
run, one of them the maintainer's real home path, emitted by pytest's own
warning summary because the self-hosted jobs run in `$RSE_DEV_REPO` rather than a checkout. P18
forbids that string in the tracked tree and has no opinion about the build log, which is the same
blind spot one level out: **every guard in this file reads `git grep` and `git ls-files`, and the
repository is not the only public surface it has.**

## What changed

`_safe_content_hits` / `_safe_path_hits` render the evidence per destination. Locally the hits
print in full — the operator's terminal is not a publication, and a hash there would turn every fix
into a lookup. Under `GITHUB_ACTIONS` they collapse to `<path-hash>:<lineno>`, reusing the device
`index/bounded_parse.py` already uses for `parse_timeout_count` under HR39: enough to locate a
failure, never enough to reconstruct it. The assertion fails identically either way; only the
evidence is withheld.

NB4 asserts the redaction, because a redaction nobody tests is the same shape of claim as a ban
list nobody sets.

## The redaction test failed in the one place it was about

NB4 passed locally and failed in CI on the first push. It set `GITHUB_ACTIONS=true` to check the
redacted arm, then checked the *local* arm without unsetting it — and under real CI the variable is
already set, so the local arm was redacted too. A test that forces a destination on must also force
the other one off: on a developer machine the second state is the ambient one and the bug is
invisible, while in CI it is the first. `_render(publishing=…)` now sets both explicitly.

Worth stating because the fix is not "remember to unset": it is that a destination-aware assertion
has two destinations, and the ambient one is whichever machine you happen to be on.

## What is deliberately not fixed

The home path in the log. It comes from pytest printing absolute source paths for a tree that
genuinely lives under `$HOME` on a self-hosted runner; removing it means relocating the dev tree to
a neutral path or filtering every step's output, and a username is a far weaker disclosure than a
customer name. It is recorded here so the next person finds it already weighed rather than missed.

## The ban matches by substring, so short tokens will eventually fire on innocent prose

Matching is `git grep -inF` plus a path substring test, and that is deliberate: a token needs no
escaping, and banning a name also bans every compound built from it. The cost is that a short
enough token stops being a name and starts being a string. Two of the shortest entries were
measured against corpora that contain no project of ours — a 104k-word system dictionary and the
whole of the virtualenv's third-party sources: one is an ordinary English word, and one already
occurs inside a vendored file. Neither is in the tracked tree, so nothing is red; both would go red
the day a doc paragraph or a vendored dependency happens to spell them.

Recorded rather than acted on. Narrowing those two entries to compound-only forms would trade a
protection that is certain — the bare token also catches compounds nobody has thought of yet, which
is the whole reason the granularity was chosen — for one that is hypothetical. The reason to write
it down anyway is that this guard's CI failure is *redacted*: it reports `<path-hash>:<lineno>` and
no text, so a future red run on an English word looks identical to a real leak. This paragraph is
what makes that five-minute diagnosis instead of an afternoon.

The same measurement retired two entries. Because matching is by substring, a token that contains
another token in the list can never fail on its own, and over an exact scan of all 1048 reachable
commits the two such entries' commit sets were strict subsets of their parents'. They were dropped:
identical coverage in the tree, in paths and in history, two fewer things to keep true. History
exposure was 964 before and after, which is also the standing evidence that pruning the list is not
a route to shrinking it.

## The private half was written, pushed, and had never run

An audit of this work the same day found the mechanism correct and its execution absent. Three
things, all of the same shape: something was *declared* and nothing checked that it was *in force*.

**The private nightly had never executed.** The companion repo's guards — the only ones that can
assert what the ban list contains — live behind a scheduled workflow that requested a runner label
no runner had, in a repository with no runner registered at all. A runner belongs to exactly one
repo or org, and a personal account has no account-level pool, so the public repo's runner could
never serve it. The job queued, and GitHub cancelled it at the 24-hour limit, four consecutive
nights. A queued run emits no failure and ages out silently; `timeout-minutes` never fires, because
queue time is not job time. So every assertion in that file was green and none had ever run.

The fix that mattered was not the runner. Those checks read a token file, an environment variable
and `git log` — they call no daemon and touch no GPU — but the suite's `conftest` aborted every
session when the daemon was unreachable, so they could only be scheduled somewhere expensive. They
now carry their own marker and run daemon-free in seconds. **A precondition that costs more than
the test it guards gets scheduled where it will not run**, and that, not the label, is why this
went unnoticed. A new check asserts a successful run of that workflow exists and is recent: a
workflow cannot notice that it did not run.

**One of the three environment scopes was written but never loaded.** `environment.d` is read when
the systemd user manager starts. The file had been correct for hours while
`systemctl --user show-environment` carried nothing, so every user service and every
non-interactive shell — including agent sessions — ran the guards here against an empty list.
Only interactive shells were covered, by a separate `.bashrc` block. The private scope check reads
the three *files*, deliberately, because a process can only ever observe the one scope it
inherited; it therefore could not see this, and a second check now covers it.
`systemctl --user daemon-reload` is enough to apply it — no re-login.

**And one correction.** The note handed forward from this work said the private CI still selected
on the retired `slow` marker and could be cleaned up. `slow` is retired *here*. In the private repo
it is registered and carried by three tests, and deleting the term would have silently added three
LLM-heavy tests to a nightly. A marker's retirement is per-suite, and "dead term" is a claim worth
checking in the suite that uses it.

**What the first run then found.** Making the workflow execute is what surfaced the rest, which is
the argument for the heartbeat in one line. The guards themselves passed — the first time any of
them had run in CI. The GPU job beside them did not: it aborted claiming its federation had
collapsed to zero indexed members, on a machine where the same code counts 137. The job passes a
root path from a repository secret that had never been created, and **an undefined secret is
injected as the empty string, not left unset** — which satisfies `os.environ.get(name, default)`
and then resolves to nothing at all. Coalesce with `or` when an empty value is as meaningless as an
absent one. Separately, the new heartbeat asked the forge for a *completed successful* run, and the
run it executes in is by definition not one yet: it demanded a predecessor only it could create.
Both are the same error as the one above, one turn further in — **a check is not in force until
something has watched it pass**, and neither of these could have been found by reading it.

**And then the suite itself.** With the workflow finally executing, the audit half of it reported
30 failed, 548 errors and 3 skips out of 1181. Almost none of that was a defect in the engine —
it was a private suite still auditing surfaces this repo had deliberately removed. `/api/kb_health`
went with the tier-3 trim, and one session fixture hitting it once per federation member accounted
for **every one of the 548 errors**. The wiki routes went the same way, and that file `skip`s on a
non-200, so a deleted endpoint made it silently inert rather than red. The retired `ask` tool took
fifteen more with it. Roughly **half the suite was testing things that no longer exist**, and the
only reason that was survivable is that it was never running to be believed.

What the deletions uncovered is the part worth keeping. A reachability matrix listed fourteen
`overview` `what` values, six of them retired, and **passed for all fourteen** — a retired variant
answers HTTP 200 with `{"error": "unknown what=…"}`, and the matrix only checked for 200. The graph
matrix had the same shape from the other direction: its `status != "error"` assertion sat behind
`if "status" in data`, and the tool reports a bad relation as a bare `{"error": …}` with no status
key, so `semantic_trace` passed long after it was removed. **A reachability check that cannot
distinguish a live surface from a deleted one is a green light wired to nothing**, and it is the
failure this whole entry is about, one level down: not a guard that never ran, but a guard that ran
and could only ever say yes. The fix in both cases was to stop retyping the list and derive it —
the server will name its own valid set if you ask it for a bogus one.

Two more of the same family. A test asserted a federation root has zero symbols of its own, which
is the belief WG3 measured and rejected; and another asserted that an *unscoped* search fans out to
a federation, which cannot be observed from a caller that is not an MCP client with roots — it was
measuring the harness. Both deleted rather than repaired: the properties they claimed are not
properties the system has.

Only one finding was about the data. Four stores carried 3–8 `vec_chunks` rows whose `chunks` rows
were gone — the exact residue BQ7 was written for, from the write path that gated the vec0 delete
on a probe of `chunks`. That defect is fixed, but the fix heals a stranded row when the same
`chunk_id` is re-inserted, and these ids will never be re-inserted; as BQ7's own docstring notes,
`delete_by_path` enumerates from `chunks` too, so **nothing short of `clear()` can reach them**.
Which is worth recording as a gap: the engine has no supported repair for the one artifact its own
validator calls INVALID. They were removed directly, after a backup, and all four stores search
normally. **602 passed, 0 failed, 0 errors, 0 skipped.**

## Related

- Build logs join commit history as a public surface no `git grep` guard reaches. GitHub's own
  guidance is that rewriting history does not remove cached views, pull-request references or
  build logs — those require Support.
- `docs/decisions/` entries for P18's tree-only scope live in `model.yaml`'s P18 text.
