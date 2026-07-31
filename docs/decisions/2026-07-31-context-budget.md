# Paying for context once: the CLAUDE.md trim and the deduplicated MCP block

**2026-07-31** · guards: `test_public_hygiene.py::test_claude_md_stays_an_instruction_file`,
`test_p21_integration_parity.py::test_canonical_body_is_served_over_mcp_not_copied_into_claude_md`

`/context` reported project memory as this repo's largest context consumer. The cause was **role,
not verbosity**: `CLAUDE.md` had become an append-only decision log. 4,492 B (06-26) → 9,481
(07-01) → 15,967 (07-09) → 18,510 (07-28) → 23,008 (07-30) — monotonic, never once shrinking,
+8.5 KB in the final three days. Two sections were 67 % of the file, and roughly 60 % of the text
changed nothing an agent would do; much of it said so in its own words (`~~HR36~~ Retired`, "the
convergence prescription that used to sit here is retired", "recorded so a future pass doesn't
re-flag it", and a full section on the `slow` marker that no longer existed).

## Measure the whole of context before trimming any of it

Only `rag-search` is configured as an MCP server, it exposes four tools, and this Claude Code
version reports MCP tools as *loaded on-demand* — so there was **no tool-schema bloat to cut**, a
plausible suspect ruled out by measurement rather than by argument. Agent frontmatter (439) and the
built-in skill descriptions (~2k) are harness-level, not this repo's. Memory files were the whole
of the available win.

Measured with `/context` in a real session (`tmux` + `claude`), before and after, by swapping the
old files back in for one run rather than extrapolating from byte counts:

| | before | after |
|---|---|---|
| project `CLAUDE.md` | **9.5k tok** | **2.3k tok** |
| global `~/.claude/CLAUDE.md` | 662 | 156 |
| `MEMORY.md` index | 431 | 431 |
| **Memory files total** | **10.6k (5.3 %)** | **2.9k (1.5 %)** |
| whole context at session start | 28.5k (14 %) | 20.9k (10 %) |

**~7.7k tokens per session, every session, every profile.** Note the byte-count estimate that
justified the work (~5,750 tok for the old file) was low by 40 % — the file is dense with code
fences and paths, ~2.4 B/token, not the ~4 B/token English default. Estimate to decide, measure to
report. In the before-run Claude Code raised its own memory-size warning ("Memory files using 10.6k
tokens → save ~3.2k"); it does not appear in the after-run.

Byte-wise: 23,170 → 5,488 B in `CLAUDE.md`, and 2,078 → 612 B in the global file (all three
profiles share one file, so the block came out of each).

## The on-demand channel existed and was inert

`.claude/skills/` held nine flat `<name>.md` files. Claude Code discovers a skill only at
`.claude/skills/<name>/SKILL.md` with `name:`/`description:` frontmatter, so **none of the nine had
ever loaded** — `scripts/gen_world_model_skills.py` was generating files nobody could read. That is
the likely reason this content kept landing in `CLAUDE.md` instead: the cheap channel looked
broken, so everything went down the expensive one.

Fixing it surfaced a second failure that is silent by construction: **an unquoted `description:`
containing `": "` parses as a nested mapping**, and the skill vanishes from the listing with no
error anywhere. Two of the nine (`phase`, `verify-engine` — "loop: detect", "loop: probe") were
lost this way on the first regeneration. `_frontmatter()` now always quotes.

## The doctrine block was billed twice

`scripts/integrations/canonical.py::CANONICAL_BODY` was written into a sentinel block in every
profile's `CLAUDE.md`, while `daemon/global_prompt.py::_PROMPT` served **the identical text** as
MCP server instructions. One set of rules, two copies, both loaded every session.

`configure_integrations.py` now repairs toward *absence*: it removes a stale block instead of
installing one, and `_verify_claude_md` reports `already_ok` when the block is gone. The parity
test inverted to match, and gained the assertion the removal actually rests on — `_PROMPT` must
still equal `CANONICAL_BODY`, or deleting the copy would silently drop live rules.

This does make the rules conditional on the daemon being reachable. That is the right coupling:
every rule in the block is an instruction about how to call that server, so with the daemon down
there is nothing for them to govern.

## Why a byte ceiling and not a review habit

The growth curve ran for five weeks across many commits, each individually reasonable. Nothing in
the process noticed, because no single append looked like the problem. `_CLAUDE_MD_MAX_BYTES =
8_000` lives in `test_public_hygiene.py` — already a whole-tree hygiene scan, and `live-fast` runs
it on **every push**, whereas `check_world_model.py` is not run by CI at all, so a guard there
would not have gated anything. The failure message names the fix rather than the number: narrative
to `docs/decisions/`, invariants to `docs/world-model/model.yaml`.

Raise the ceiling only if the added text changes what an agent does next.
