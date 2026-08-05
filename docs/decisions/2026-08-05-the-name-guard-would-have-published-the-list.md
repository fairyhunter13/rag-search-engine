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

## Related

- Build logs join commit history as a public surface no `git grep` guard reaches. GitHub's own
  guidance is that rewriting history does not remove cached views, pull-request references or
  build logs — those require Support.
- `docs/decisions/` entries for P18's tree-only scope live in `model.yaml`'s P18 text.
