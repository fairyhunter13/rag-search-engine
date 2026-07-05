# World Model — Canonical Definition (June 2026)

> **Status:** reference — repo-agnostic. This document defines *what a world model is* (independent of any specific repo). For OSE's governance/spec world model see `docs/world-model/`. For companion repo's governance+execution world model see `companion-governance-repo/docs/world-model/`.

---

## 1. Definition

A **world model** is a **structured internal representation of an environment's state and dynamics** (rules, causal relations) that, given a current state and an action, **predicts the next state** — enabling multi-step forward rollout, planning, and counterfactual reasoning.

The term is contested across the field ("refers to so many different things that the speaker and listener often mean different things by it" — ACM survey 2026). **Regime declaration is mandatory.** We use the **understanding + symbolic/relational-state regime** (LeCun-JEPA 2026: "predict in abstract space, not pixels") — symbolic/relational states beat raw pixels for planning and control; a `model.yaml`/PDDL-style model is the right shape.

The **distinguishing capability is planning**: a model that only replays fixed scripts is a *simulator*, not a world model (WorldPrediction WM-vs-PP split, 2026). The defining payoff is the ability to reason about *sequences of actions not yet taken*.

---

## 2. Four-property conformance bar

Every world model must satisfy all four properties:

| # | Property | Minimum requirement |
|---|---|---|
| (1) | **State representation** | Explicit data structure enumerating what can change (entities, attributes, relations, invariants) |
| (2) | **Action-conditioned next-state** | For each action: a **guard** (precondition — when the action is permitted) and a **delta** (effect — what changes). Guard + delta = operator schema (PDDL-style). |
| (3) | **Multi-step rollout** | Can execute a sequence of actions step-by-step; intermediate states are inspectable. |
| (4) | **Planning / counterfactual** | Can find a sequence of actions from a start state to a goal state; or evaluate "what if X had happened instead". A model without planning is only a *replayer*. |

A **validator is mandatory** (2026 CWM finding): multi-step rollout degrades via *action hallucination* (illegal or phantom actions inserted by an LLM planner), not only state errors. The validator must reject sequences that contain illegal or undefined actions before any state-delta is applied.

---

## 3. Per-repo regime

| Repo | Regime | Executor |
|---|---|---|
| **OSE (rag-search-engine)** | **Governance/spec only** — the "domain" of OSE *is* the development rules; no separate business domain to simulate. State = codebase + invariants/laws. Action = a diff/change. Guard = does the diff satisfy the preconditions (P0–P11)? Delta = resulting conformance verdict. Planner = which change-sequences are permitted. Validator = `check_world_model.py` (rejects diffs that violate L1 invariants). | `scripts/check_world_model.py` |
| **companion-governance-repo** | **Governance/spec + execution** — governance/spec layer (repo invariants) **plus** an executable domain world model. State = `SimState` (commitments/call-offs/returns/transfers/inspections/invoices/SOH). Actions = domain commands (CreateCommitment, PostCallOff, …). Planner = BFS `plan(state, goal)`. Validator = `--validate` mode in `simulate.py`. | `docs/world-model/simulate.py` |

**One shared `model.yaml` schema, two profiles.** `scripts/gen_world_model_skills.py` and `scripts/check_world_model.py` are parameterized per repo (governance-only profile for OSE; governance+execution profile for companion repo) — one tool, two profiles, not two separate generators.

---

## 4. Sources (June 2026)

| Source | Key claim |
|---|---|
| ACM Computing Surveys — "Understanding World or Predicting Future?" (10.1145/3746449, June 2026) | "The essential purpose of a world model is to understand the dynamics of the world and compute the next state with certainty." Symbolic/PDDL executable models **are** valid world models. |
| arXiv 2604.22748 — "Agentic World Modeling: Laws" | Planning = the distinguishing capability; correct action-*sequences*, not single-step replay. |
| arXiv 2502.13092 — Text2World | Symbolic WMs pair naturally with planners: guard = precondition, state-delta = effect. |
| arXiv 2506.04363 — WorldPrediction WM-vs-PP split | A model that predicts the *next frame* only = Predictive Model. A model that supports *goal-directed planning* = World Model. |
| arXiv 2506.22355 — Embodied AI Agents (2026 survey) | Object-centric/relational/symbolic states beat pixels for planning and control. |
| LeCun-JEPA (2026) | Predict in **abstract space**, not pixel space; abstract-state prediction is the right target. |
| Code-World-Model (CWM) finding, 2026 | Multi-step rollout degrades via **action hallucination**, not state errors → validator mandatory. |

---

## 5. Conformance checklist

| Property | Check |
|---|---|
| State representation explicit | `model.yaml` has a `state:` or `L2_components:` section enumerating entities and attributes |
| Guard + delta for every action | Each command/action entry has `guard:` (precondition) and `delta:` (effect) |
| Multi-step rollout executable | `simulate.py --scenario` or `check_world_model.py` can execute a step sequence |
| Planning present | `plan(state, goal)` function exists (or governance-level: `check_world_model.py` evaluates change-sequence) |
| Validator present | Sequences with illegal/undefined actions are rejected before state-delta is applied |
| Regime declared | README/SPEC explicitly states which regime (governance-only or governance+execution) |
| Skills generated + default-enforced | `.claude/skills/world-model.md` present and auto-loaded each session |

---

## See also

- `docs/world-model/model.yaml` — OSE machine-readable governance/spec WM (L1–L4 layers)
- `docs/world-model/README.md` — OSE WM narrative + tools
- `companion-governance-repo/docs/world-model/SPEC.md` — companion repo governance+execution WM narrative
- `companion-governance-repo/docs/world-model/simulate.py` — companion repo executable planner + validator
- `scripts/check_world_model.py` — conformance checker (both profiles)
- `docs/reference/llm-drivers.md` — doc-tooling driver doctrine
