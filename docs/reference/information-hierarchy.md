# Information Hierarchy — Canonical Definition (June 2026)

> **Status:** reference — repo-agnostic, machine-verified by `check_world_model.py`. Do not conflate with Information Architecture (structure) or Visual Hierarchy (rendering).

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
4. **`[code: file:line]` grounding (doc-tooling law).** Every claim about a codebase must cite a real file:line that was actually read. Claims without resolvable citations are hallucinated structure and must be rejected by the citation-resolution gate.

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
| Every claim code-grounded | All `[code: file:line]` citations resolve (plain file read + content-substring match) |
| No IA/IH conflation | No co-equal Labeling / Navigation / Search headings at §1/§2/§3 level |

---

## See also

- `docs/info-hierarchy.md` — RSE's DIKW doctrine ladder (how RSE spends LLM tokens at each rung)
- `docs/reference/world-model.md` — canonical WM definition
- `docs/reference/llm-drivers.md` — doc-tooling LLM driver doctrine
- `docs/CONFORMANCE_EVALUATION.md` — current-state scorecard + gap map for this repo
