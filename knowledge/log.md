---
type: Log
title: rag-search-engine knowledge history
---

# Bundle history

## 2026-08-18

- **Update**: `index.md` rewritten. It routed every type out of the bundle; it now lists the
  concepts below and states the two things that did not move — the `HR#` rows and §14 map in
  `docs/architecture/federation-ops-and-invariants.md`, and the dated records in `docs/decisions/`.
  Both are held by tests that `okf check` cannot replace. Reasoning:
  [docs/decisions/2026-08-18-the-bundle-becomes-the-home.md](../docs/decisions/2026-08-18-the-bundle-becomes-the-home.md).
- **Creation**: 11 Constraints, one per subject rather than one per `HR#`. 29 live rows describe
  about eleven subjects, so a file per row would have failed OKF's not-already-covered gate eleven
  times over. Each concept names the ids it holds and carries a sources table back to the guards.
- **Creation**: 7 Components and 2 Interfaces. `server/` and the HTTP route layer were the
  thinnest-covered subsystems in the repo and had no prose home at all.
- **Creation**: 5 Defects, absorbing the durable findings from `docs/audits/`, which was deleted in
  the same commit — 51 of its ~104 cited paths no longer existed and it said so itself.
- **Creation**: 4 Runbooks. Two procedures had no home (registry restore, the self-hosted CI
  runner); two were scattered across `CLAUDE.md` and the decision records they cite.
  Fresh-machine onboarding was deliberately **not** written: `README.md` covers it and is gated,
  so a second copy would be the seventh copy this bundle exists to prevent.

## 2026-08-17

- **Creation**: the bundle, empty of concepts. The global doctrine sends an agent to `knowledge/`
  before working; in this repo that is the wrong place to write, and an index that says so is
  cheaper than the seventh copy it prevents. Reasoning:
  [docs/decisions/2026-08-17-okf-lands-as-a-signpost.md](../docs/decisions/2026-08-17-okf-lands-as-a-signpost.md).
