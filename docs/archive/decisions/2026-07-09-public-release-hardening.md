# Public-release hardening and the runnable-by-anyone contract

**2026-07-09**, runner hardening **2026-07-14** · P18/HR34 · guards: `test_public_hygiene.py`,
`test_no_real_project_in_tests.py`, `test_no_mocks_or_fakes.py`, `model.yaml` P7/P18/HR13/HR34

This repo is **public**. Never commit secrets, real device paths, or company/project names. Every
machine-specific value (storage paths, host, port, models, GPU device) is env-driven with XDG
defaults — see `core/config.py:8-46` (`XDG_DATA_HOME`, `RSE_REGISTRY_PATH`, `RSE_INDEX_ROOT`,
`RSE_MCP_DAEMON_HOST/PORT`, `RSE_GPU_DEVICE`). No hardcoded absolute paths (`/home/<user>/`,
`/root/`, `/Users/<user>/`, `C:\Users\<user>\`), usernames, or hostnames anywhere in tracked
source, tests, docs, scripts, or generated artifacts.

Device-specific *name* bans (real company/codename/device-id lists) deliberately stay out of this
public tree — they live only in the private `rse-live-audit` repo.

## The runnable-by-anyone contract

Public-release readiness is more than path hygiene: a fresh clone must run with zero source edits
given only env vars and the README setup steps.
`test_public_hygiene.py::test_runtime_config_is_env_driven` asserts every machine/deployment
constant in `core/config.py` — embed/rerank model, embed device, daemon host/port, query LLM
provider/model, GPU device override — is produced by `os.environ.get(...)`, not a hardcoded
literal.

This repo has **no submodules** since docgen's deletion (2026-07-28), so a plain `git clone` is
complete and there is no submodule URL left to audit.

## Audited and deliberately left alone

The CI `live-fast` job's `github.repository` guard was audited and found already fork-safe by
design — it exists specifically so forks lacking a self-hosted GPU runner skip the job instead of
queuing indefinitely (see the comment above that job in `.github/workflows/ci.yml`). **No change
needed**, recorded here so a future pass doesn't re-flag it.

## Self-hosted-runner hardening (2026-07-14)

Because this is a public repo whose GPU jobs run on a self-hosted runner (this device), the
`pull_request` trigger was removed and the fork-PR workflow-approval policy tightened to
`all_external_contributors`, so a fork PR can no longer trigger CI on the self-hosted runner. The
`github.repository`/ref `if` guards now stand as defense-in-depth.

There is also deliberately **no `schedule` trigger**: a nightly cron fired both live jobs
unattended and drained the owner's Claude session quota mid-workday.

And no commit-message trigger: `contains()` matches the whole message including the body, so a
commit that merely mentioned the tag in prose fired a 60-minute real-model run.

The audit behind this — `docs/audits/2026-07-09-whole-engine-conformance-and-research.md` — was
deleted 2026-08-18 and lives in git history; its durable findings are in `knowledge/defects/`.
