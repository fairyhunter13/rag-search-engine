# Open Knowledge Format (OKF) — Canonical Definition (June 2026)

> **Status:** reference. Canonical source: `github.com/GoogleCloudPlatform/knowledge-catalog/tree/main/okf` (published 12 Jun 2026, v0.1).

---

## 1. Definition

**OKF (Open Knowledge Format)** is Google Cloud's open specification for representing knowledge as a **directory tree of markdown files**, where:
- Each file = **one concept** (file path = concept identity)
- Each file = YAML frontmatter block + free-form markdown body
- The **only required frontmatter field** is `type`
- Concepts **cross-link with ordinary markdown links** → the link graph IS the knowledge graph (no separate edge store, no rigid schema)

OKF is a *knowledge graph in prose*: concepts reference each other through links, forming a navigable semantic web without imposing a fixed taxonomy.

---

## 2. Specification

### 2.1 File structure

```
<okf-bundle>/
  index.md          # reserved — progressive disclosure of this folder (required at bundle root)
  log.md            # reserved — change history (optional)
  <concept>.md      # one per concept; path = identity
  <subfolder>/
    index.md        # reserved — progressive disclosure of subfolder
    <concept>.md
```

### 2.2 Frontmatter schema

```yaml
---
# REQUIRED
type: <string>          # concept type; open vocabulary; unknown types MUST NOT be rejected

# RESERVED-OPTIONAL (preserve when present, but never required)
title: <string>         # human-readable display name
description: <string>   # one-line summary
resource: <uri>         # canonical URI for this concept
tags: [<string>, ...]   # free-form labels
timestamp: <ISO-8601>   # creation or last-modified date

# PRODUCER FIELDS (any additional field is allowed; consumers must preserve, not reject)
okf_version: "0.1"     # producer may stamp the bundle version (used in index.md)
generated: true         # producer may mark machine-generated bundles
---
```

### 2.3 Concept naming

File path = concept identity. Names should be semantic (e.g., `search-pipeline.md`, `gpu-inference.md`, `flat-community-model.md`) — **never** mechanical fragment IDs (`fragment_1.md`, `fragment_N.md`).

### 2.4 Cross-links

```markdown
The [search pipeline](search-pipeline.md) depends on the [GPU inference](gpu-inference.md) layer.
```

Cross-links use ordinary markdown `[label](path)` syntax. Relative paths within the bundle. All cross-links MUST resolve.

### 2.5 Vocabulary for codebase OKF bundles

The `type` vocabulary is **open and LLM-inferred** (never keyword-matched). Examples for code repos:

`Module` · `Service` · `Command` · `Event` · `Policy` · `Process` · `DataModel` · `Endpoint` · `Invariant` · `Pattern` · `Pipeline` · `Protocol` · `Configuration`

Unknown types must be preserved (Postel); consumers must not error on unfamiliar `type` values.

---

## 3. Robustness rule (Postel's Law)

Producers may freely extend frontmatter. Consumers MUST:
- **Preserve** unknown frontmatter fields (round-trip-safe)
- **Not reject** files with missing optional fields
- **Not reject** files with unknown `type` values
- Accept any valid YAML frontmatter with at least `type:` present

---

## 4. RSE's OKF implementation (`vendor/okf`)

| Property | Implementation |
|---|---|
| Generator | `vendor/okf/src/okf/generate.py` — `generate(project_path, out_dir=None) → dict` |
| Driver | `claude -p` (LLM-native; no tree-sitter; no regex) — reads repo source directly, identifies semantic concept units, infers `type`, synthesizes body with `[code: file:line]` citations |
| Output path | `<project>/docs/okf/` |
| RSE adapter | `src/rag_search/kb/okf.py` — thin wrapper; kill-switch: `RSE_OKF=0` (off ⇒ no output) |
| Trigger | CLI `rag-search okf <project>` or dashboard; **never** MCP, never auto-enrich sweep |
| CI/offline | No deterministic generator; golden fixture bundles in `src/tests/live/fixtures/okf_golden/` |

---

## 5. Conformance checklist

| Property | Check |
|---|---|
| `type` present in every file | All `.md` files except `index.md`/`log.md` have `type:` in frontmatter |
| Reserved fields well-typed | `resource` is a URI string; `tags` is a list; `timestamp` is ISO-8601 |
| `index.md` present at bundle root | `index.md` with `okf_version` + `generated: true` exists |
| Concept-named paths | No `fragment_N.md` names; all filenames are semantic |
| Cross-links resolve | Every markdown `[label](path)` link resolves to an existing file in the bundle |
| Every `[code: file:line]` citation resolves | Plain file read + content-substring match (citation-resolution gate) |
| Unknown-field round-trip preserved | Re-parsing a serialized bundle preserves unknown frontmatter fields |
| No absolute paths or company names | `Path.home()` not present in any artifact |
| No tree-sitter import on doc-tooling path | `import tree_sitter` / `from tree_sitter` absent from `vendor/okf/` |
| Kill-switch works | `RSE_OKF=0` → `run_okf()` returns without generating any files |

---

## See also

- `vendor/okf/src/okf/generate.py` — OKF generator
- `src/rag_search/kb/okf.py` — RSE adapter
- `src/tests/live/test_okf.py` — live conformance tests
- `docs/reference/llm-drivers.md` — `claude -p` driver doctrine
- `docs/reference/information-hierarchy.md` — docgen IH (distinct from OKF concept-graph)
