---
type: Decision
resource: .github/workflows/ci.yml, tests/live.py
title: CI runs no GPU and no live job, and the self-hosted runner is deregistered
description: A self-hosted runner on a public personal-account repo cannot be scoped by a runner group, and the GPU suites contend for the one card everything else is serialised on; both reasons point the same way.
tags: [ci, security, gpu]
status: stable
generated: { by: claude/opus-5, at: 2026-08-19T21:30:00Z }
---

# Decision

`.github/workflows/ci.yml` has two jobs, both `ubuntu-latest`: lint/compile/bundle, and
`pytest -m "not gpu and not live and not restart"`. The runner `rse-gpu-runner` is deregistered.
`-m gpu`, `-m live` and `-m restart` run from the shell, which was already the daily workflow.

# Why, and it is two reasons that happen to agree

**Contention.** `require_clear_gpu()` refuses to interleave. The self-hosted `live-fast` job ran on
every push to `main`, on this workstation, holding the one card the fleet index and the bake-off are
serialised on. A push during an overnight index either fails that lane or is failed by it — and the
failure is indistinguishable from a real regression.

**Scope.** GitHub's position is that self-hosted runners "should almost never be used for public
repositories", and the mitigation it recommends — runner groups — is org/enterprise only, so it is
**not available on a personal account**. The inherited workflow closed the fork-PR path properly
(no `pull_request` trigger, and an actor + ref + repository gate on both self-hosted jobs), so this
was never an open hole. But those `if:` lines were doing a runner group's job on a machine holding
SSH keys and a private client index, one workflow edit away from not being true.

Deleting the risk class costs a shell command that was already being typed. Managing it costs an
ephemeral registration, an `environment:` with a required reviewer, and a standing invariant that
nobody re-reads.

# What this gives up, and why the trade holds

CI no longer proves the GPU and daemon layers on every push. It never really did: those jobs had no
checkout and ran the maintainer's working tree, so a green tick attested to a commit whose bytes
might not have run — the workflow carried three paragraphs of comment about exactly that problem and
a gate that failed the job whenever any agent session had uncommitted work. The honest version of
that guarantee is a local run against the real card, which is where the numbers came from anyway.

`permissions: contents: read` is stated rather than inherited, and all three actions are pinned to
40-char commit SHAs. A tag is mutable by anyone with write access to the action's repo — the
mechanism of CVE-2025-30066 (tj-actions/changed-files, March 2025), which rewrote every version tag
to a malicious commit and exfiltrated secrets through the workflow log.
