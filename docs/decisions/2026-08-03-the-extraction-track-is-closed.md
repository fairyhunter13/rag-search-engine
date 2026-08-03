# The extraction track is closed: the last two items were measured and declined

**Date:** 2026-08-03 · **Stamp:** none — nothing shipped · **Items:** Tier-2 `$ref` resolvers, and
the `anon_dropped` residue

Two items remained in the extraction track after e12. Both were sized before being built, both came
back decisively negative, and both are recorded here so they are not proposed again from the same
headline numbers that motivated them.

## Tier-2 closed-namespace resolvers: ~40 files exist

The plan ranked `$ref` resolution first of the Tier-2 tier on one argument: *"the fleet is 41% JSON
symbols and no product in the survey does it."* The first half is true and the second is probably
true, and **neither is a statement about `$ref`.** Those JSON symbols are lockfiles, config and
data. Measured over every indexed json/yaml file in the fleet:

| | |
|---|---:|
| indexed json/yaml files | 11,894 |
| **files containing a `$ref` at all** | **12** (8 yaml, 4 json) |
| local refs (`#/components/...`, intra-file) | 467 |
| **external refs (an actual file edge)** | **8** |
| external targets that are indexed files | 8 (100%) |

The resolver would be correct — every external target resolves — and it would emit **eight edges**
across 208 repositories. The 467 local refs are intra-file and so are not a file-to-file relation at
all.

The rest of the tier is the same story, and it is a corpus fact rather than an engineering one:

| Tier-2 candidate | files in the fleet |
|---|---:|
| protobuf | **6** |
| terraform + hcl | **22** |
| graphql | **0** |

**Declined.** Not because the resolution is hard — §5a is right that it is spec-defined and
P6-clean — but because the fleet has essentially none of these files. This is the same failure mode
as the RepoGraph citation behind #8: a number that is real elsewhere, imported as if it described
*this* corpus. The difference is that #8 was measured locally first and survived; this one did not.

Revisit only if a repo lands that is actually schema-driven — the check is one query against
`file_extraction`, not a research pass.

## The `anon_dropped` residue is anonymous by construction

The headline was 17,701 discarded symbols. An earlier probe had already cut it by 74.1% — most
anonymous structure entries are covered by a symbol e7's S5 arm emits anyway — and relocated the
residue from javascript to typescript. What was never asked is **where a name for the remainder
would come from.** Measured over 400 ts/tsx files carrying `anon_count > 0` (all 400 present on
disk), finding every function-shaped node not covered by an emitted symbol's span:

| parent node kind of the uncovered function | count |
|---|---:|
| **`arguments`** | **1,677 (96.4%)** |
| `array` | 28 |
| `jsx_expression` | 23 |
| `class_body` | 11 |
| `return_statement` | 1 |

**96.4% of the residue is a callback passed as a call argument** — `app.use(() => {})`,
`items.map(x => …)`, `describe('…', () => {})`. There is no binding to read a name from, which is
precisely why S5 does not reach them: S5 names a function *value bound to a name*, and these are
bound to nothing.

The only names within reach are the enclosing call's identifier (`map`, `describe`) or a sibling
string-literal argument. Both are framework conventions — a test name is a name because a test
runner says so — and writing either down is the mapping table **HR15** exists to keep out. The
`array` / `jsx_expression` / `return_statement` rows have no name available under any rule.

**Declined.** These functions are anonymous in the source, and `anon_count` recording them is the
correct outcome: the ladder's contract is that no file disappears without a recorded reason, not
that every span acquires a name.

## What this closes

The extraction track opened with seven items. Four shipped (#4 pack upgrade e9, #7's extractor half
e10, #8 import edges e11, #3's javascript arm e12), one was built and reverted (#10
`_error_byte_ratio`), and these two are declined on measurement. **Nothing further in this repo's
extraction path has evidence behind it.** The remaining ranked work is the embedding track, which
touches no grammar and moves no stamp.
