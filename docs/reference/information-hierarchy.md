# Information Hierarchy — Canonical Definition (June 2026)

> **Status:** reference — repo-agnostic. Do not conflate with Information Architecture (structure) or Visual Hierarchy (rendering).
>
> *Corrected 2026-07-28: this line used to claim the page was "machine-verified by `check_world_model.py`". It is not — that checker reads `docs/world-model/model.yaml` and nothing else (`scripts/check_world_model.py:14`). The §5 checklist below is a rubric a reader applies by hand, not a gate that runs.*

---

## 1. Definition

**Information hierarchy** is the **organization and prioritization of information by relevance or importance** — a value-ranked tree that runs from the broadest, most-essential categories at the top down to the finest granular data points at the leaf. It provides a logical flow from most- to least-essential, so the reader (or search system) encounters the most important things first.

The name "hierarchy" is precise: Wurman (1989, *Information Anxiety*, LATCH framework) defines H as *"a value system that places things in relative importance to one another."* Every node in the tree has a rank — not just a position.

### The three-way distinction

| Concept | What it is | Relationship to IH |
|---|---|---|
| **Information hierarchy** | The **value/importance-ranked tree** — what matters most and why | **The thing itself** |
| **Information Architecture (IA)** | The *structural framework* — organization schemes, labeling, navigation, search systems (IxDF 2026: "four systems") | **Supporting machinery** — IA's four systems (labeling / navigation / search / organization) are subordinate tools that expose the hierarchy; they are not co-equal parts of it |
| **Visual hierarchy** | The on-screen *rendering* — size, contrast, position, color weight | **Projection** — visual hierarchy renders the information hierarchy on screen; NN/g (2026): "arranging elements so users can perceive relative importance" |

**DIKW is orthogonal.** Data → Information → Knowledge → Wisdom is an *abstraction ladder*; IH is a *generality/importance ranking within* any single rung. They compose, not substitute.

---

## 2. Defining properties

1. **Value/generality spine.** Categories are ranked by generality (broadest = most primary). Labels: **Primary** (most general), **Secondary**, **Tertiary** (most specific leaf).
2. **Drill-down / roll-up traversal.** Drill-down = root→leaf, general→specific, finer granularity (Oracle/TIBCO OLAP). Roll-up = leaf→root, specific→general, coarser aggregation. Both operators must be named and supported.
3. **Importance ranking, not structural ordering.** Alphabetical / temporal / spatial ordering is not a hierarchy (Wurman LATCH distinguishes all five). A hierarchy requires an explicit value judgment.
4. *(retired 2026-07-28 — **`[code: file:line]` grounding**, the doc-tooling law: every claim about a codebase had to cite a real file:line, and the citation-resolution gate rejected the rest as hallucinated structure. It was never one of the three sourced properties above; it was a local law that existed because a generator wrote prose about code. Doc-tooling is deleted, so there is no generated prose to ground and no gate to reject it. The number is kept rather than reused. **The lesson survives its subject:** hallucinated structure was worth guarding against precisely because nothing downstream could tell it from the real thing — which is why RSE now has no generative documentation layer at all.)*

---

## 3. Canonical 5-section order (per-domain leaf files)

Defined from a companion repo's gold-standard exemplar (`docs/information-hierarchy/`):

```
§1  [Topic] Hierarchy                  ← the value/generality tree (PRIMARY / SECONDARY / TERTIARY)
§2  Traversal: drill-down · roll-up    ← the two reciprocal operators (general→specific; specific→general)
§3  Visual ranking                     ← how the hierarchy projects onto screen (tab/column order by importance)
§4  Supporting IA systems              ← labeling · navigation · search (subordinate tools, one heading)
§5  Cross-references                   ← links to related domains / model files
```

IA's labeling, navigation, and search systems appear **once**, under §4, as supporting machinery — never as co-equal §2/§3/§4 headings.

---

## 4. Sources (June 2026, independent convergence)

| Source | Key claim |
|---|---|
| Wurman, *Information Anxiety* (1989, LATCH) | "H = a value system that places things in relative importance to one another." |
| IxDF — "What is Information Architecture?" (2026) | IA = four systems: organization · labeling · navigation · search. Hierarchy is **one** organization scheme inside IA. "IA determines which elements are important; visual hierarchy renders it." |
| NN/g — "Visual Hierarchy in UX: Definition" (2026) | Visual hierarchy = "arranging elements by relative importance" expressed through size/contrast/position on screen. |
| Topcoder — IA vs UX (2026) | "IA is the skeleton and information hierarchy holds the content together." |
| Oracle / TIBCO / OLAP drill-roll documentation (2026) | Drill-down = root→leaf, general→specific. Roll-up = leaf→root, specific→general. |
| companion-governance-repo `docs/information-hierarchy/` (human-authored, June 2026) | Gold-standard exemplar: per-domain importance/generality spines, canonical 5-section order, every claim `[code: file:line]` grounded. |

---

## 5. Conformance checklist

| Property | Check |
|---|---|
| Value/generality spine is the document centerpiece | §1 heading = "[Topic] Hierarchy"; generality tree with PRIMARY/SECONDARY/TERTIARY labels present |
| Drill-down/roll-up named as §2 | Appears immediately after the tree spine |
| Visual ranking present as §3 | Framed as "rendering of the generality tree"; tab/column order by importance |
| IA systems subordinate under §4 | Labeling/navigation/search collapsed under one "## Supporting IA systems" heading |
| ~~Every claim code-grounded~~ | *retired 2026-07-28 with property 4 — the citation-resolution gate went with doc-tooling* |
| No IA/IH conflation | No co-equal Labeling / Navigation / Search headings at §1/§2/§3 level |

---

## See also

- `docs/info-hierarchy.md` — RSE's DIKW doctrine ladder *(was "how RSE spends LLM tokens at each rung" until 2026-07-28; every rung now reads `$0` — the climb is deterministic end to end, so what the page documents is where the **compute** goes, not the tokens)*
- `docs/reference/world-model.md` — canonical WM definition
- `docs/reference/llm-drivers.md` — the `claude -p` chat lane and its profile selection *(was "doc-tooling LLM driver doctrine"; doc-tooling is deleted and that page was rewritten down to the one live subsystem it documented)*
- `docs/CONFORMANCE_EVALUATION.md` — dated verification record for this repo

**Kept, not deleted, on 2026-07-28** — the same call, for the same reason, as `world-model.md`. The tier-3 plan's "documents only deleted subsystems" row does not describe this page: it defines *what an information hierarchy is*, from Wurman/IxDF/NN-g, and RSE's own hierarchy (`docs/info-hierarchy.md`) survives whole — it lost its prices, not its rungs. Only property 4 named something that left, and it was an import from doc-tooling rather than one of the sourced properties.
