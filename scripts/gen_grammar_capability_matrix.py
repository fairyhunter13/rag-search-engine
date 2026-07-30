#!/usr/bin/env python3
"""Generate `docs/reference/grammar-capability-matrix.md` — the extraction ladder's ceiling.

Part 2 of W1-A's audit apparatus. "Adaptive to any language" cannot be proven; what can be
published is the **per-language ceiling**, so a language that reaches only rung 6 is an expected
fact in the repo rather than a surprise each incident rediscovers.

Generated, never hand-written, so its drift is reviewable in a diff: the pack ships new grammars
and new query files between releases, and a hand-maintained table would silently describe the
version it was written against.

The rung a language can *at best* reach, from its query files alone:

  rung 1 `injections`  — needs an injections query that declares the inner language **statically**
                         (`#set!`). A dynamic `@injection.language` capture is not counted: rung 1
                         deliberately ignores those (see `_injection_blocks`).
  rung 3 `structure`   — not derivable from query files; `process()` either yields structure for a
                         grammar or it does not, and that is a property of the pack's own Rust
                         code. So this table reports the *query-derived* ceiling only, and a
                         language marked rung-6 here may still reach rung 3 or 4 in practice.
  rung 4 `highlights`  — needs a highlights query.
  rung 6 `recorded`    — no query files at all. **This is a true ceiling for rungs 1 and 4** and
                         the number the ladder cannot improve: measured 2026-07-30, groovy and
                         gradle are in it, which is 2,222 fleet files that no rung reaches.

Run: .venv/bin/python scripts/gen_grammar_capability_matrix.py
"""
from __future__ import annotations

import sys
import typing
from collections import Counter
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "docs" / "reference" / "grammar-capability-matrix.md"


def _languages() -> list[str]:
    import tree_sitter_language_pack as pack
    return sorted(typing.get_args(pack.SupportedLanguage))


def _queries(lang: str) -> dict[str, str]:
    import tree_sitter_language_pack as pack
    getters = {"highlights": pack.get_highlights_query, "tags": pack.get_tags_query,
               "injections": pack.get_injections_query, "locals": pack.get_locals_query}
    out = {}
    for name, getter in getters.items():
        try:
            out[name] = getter(lang) or ""
        except Exception:
            out[name] = ""
    return out


def _row(lang: str) -> tuple[str, dict[str, str], bool, str]:
    q = _queries(lang)
    # Static only — the same rule `_injection_blocks` applies, so the table describes the
    # extractor that exists rather than an injections rung nobody built.
    static_inj = bool(q["injections"]) and "#set!" in q["injections"] \
        and "injection.language" in q["injections"]
    if static_inj:
        best = "1 injections"
    elif q["highlights"]:
        best = "4 highlights"
    else:
        best = "6 recorded"
    return lang, q, static_inj, best


def main() -> int:
    try:
        langs = _languages()
    except Exception as exc:  # pragma: no cover - the pack is a hard dependency
        print(f"tree_sitter_language_pack unavailable: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 1
    rows = [_row(lang) for lang in langs]
    have = Counter()
    for _lang, q, static_inj, best in rows:
        for name, text in q.items():
            have[name] += bool(text)
        have["injections(static)"] += static_inj
        have[best] += 1
        have["no query files"] += not any(q.values())

    lines = [
        "# Grammar capability matrix",
        "",
        "**Generated — do not edit.** `scripts/gen_grammar_capability_matrix.py` rewrites this "
        "file; a hand-maintained copy would silently describe the pack version it was written "
        "against. Regenerate after a `tree-sitter-language-pack` upgrade and review the diff.",
        "",
        "This is the extraction ladder's **ceiling**, published rather than rediscovered. "
        "\"Adaptive to any language\" is not a testable proposition; the per-language ceiling is a "
        "fact, and it makes a rung-6 language an expected outcome instead of an incident.",
        "",
        "`best rung` is the **query-derived** ceiling only. Rung 3 (`process()` structure) and "
        "rung 5 (`data_extraction`) come from the pack's own Rust code, not from query files, so "
        "a language listed `6 recorded` here may still reach rung 3, 4 or 5 in practice — the "
        "column bounds what rungs 1 and 4 can do, and nothing else.",
        "",
        "`injections(static)` counts only grammars declaring the inner language with `#set!`. "
        "Rung 1 ignores dynamic `@injection.language` captures on purpose: taking the language "
        "from the document's own text (a markdown fence's info string) reads a token to choose a "
        "grammar, which P6 forbids, and would enrol every fenced README example as a definition.",
        "",
        "## Totals",
        "",
        f"- languages served: **{len(rows)}**",
        f"- highlights: **{have['highlights']}** · tags: **{have['tags']}** · "
        f"injections: **{have['injections']}** (static: **{have['injections(static)']}**) · "
        f"locals: **{have['locals']}**",
        f"- **no query files at all: {have['no query files']}** — the population no rung "
        "reaches. groovy and gradle are in it, which is 2,222 fleet files.",
        f"- ceiling: rung 1 **{have['1 injections']}** · rung 4 **{have['4 highlights']}** · "
        f"rung 6 **{have['6 recorded']}**",
        "",
        "## Per language",
        "",
        "| language | highlights | tags | injections | static inj. | locals | best rung |",
        "|---|---|---|---|---|---|---|",
    ]
    mark = {True: "yes", False: "-"}
    for lang, q, static_inj, best in rows:
        lines.append(
            f"| `{lang}` | {mark[bool(q['highlights'])]} | {mark[bool(q['tags'])]} | "
            f"{mark[bool(q['injections'])]} | {mark[static_inj]} | "
            f"{mark[bool(q['locals'])]} | {best} |")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines) + "\n")
    print(f"wrote {OUT.relative_to(Path.cwd())}: {len(rows)} languages, "
          f"{have['no query files']} with no query files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
