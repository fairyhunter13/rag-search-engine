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

## Related

- Build logs join commit history as a public surface no `git grep` guard reaches. GitHub's own
  guidance is that rewriting history does not remove cached views, pull-request references or
  build logs — those require Support.
- `docs/decisions/` entries for P18's tree-only scope live in `model.yaml`'s P18 text.
