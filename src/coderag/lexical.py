"""Identifier splitting and FTS5 query building.

The tokenizer and the splitter are complementary, not alternatives, and the
store needs both.

`tokenize="unicode61 ... tokenchars '_$.'"` keeps `snake_case_name`, `$var` and
`pkg.Method` as single terms, which is the exact-identifier recall dense
retrieval misses. But it also keeps `parseUserConfig` as one term, so a search
for `user config` -- how a person actually asks -- matches nothing at all.

So chunks carry a second indexed column holding the *parts*: `parse user
config`. Whole identifiers stay findable through `text`, their pieces through
`tokens`, and neither column can do the other's job.
"""

from __future__ import annotations

import re

# A term as the FTS5 tokenizer sees it, `_$.` included so the two agree on
# what a token is.
_TERM = re.compile(r"[A-Za-z0-9_$.]+")

# camelCase and PascalCase, with the acronym case handled: HTTPServer splits to
# HTTP + Server rather than H + T + T + P + Server.
_CAMEL = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+")


def split_identifier(word: str) -> list[str]:
    """The lowercase parts of one identifier, without the identifier itself.

    Returns nothing for a word that has no parts: a single lowercase word is
    already indexed under `text`, and repeating it in `tokens` would double its
    term frequency and quietly bias BM25 toward chunks dense in short names.
    """
    pieces = [p for chunk in re.split(r"[_$.\-/]+", word) for p in _CAMEL.findall(chunk)]
    if len(pieces) < 2:
        return []
    return [p.lower() for p in pieces]


def identifier_tokens(text: str) -> str:
    """The `tokens` column for a chunk: every identifier's parts, deduped.

    Deduped because a chunk that calls `getUserName` eleven times should weigh
    the same for `user` as one that calls it once -- the whole-identifier
    column already carries the repetition.
    """
    seen: dict[str, None] = {}
    for word in _TERM.findall(text):
        for part in split_identifier(word):
            seen.setdefault(part, None)
    return " ".join(seen)


def fts_query(query: str) -> str:
    """A user string as an FTS5 MATCH expression.

    Every term is double-quoted and internal quotes are doubled, because FTS5
    treats bare `AND`, `NOT`, `*`, `:` and `^` as syntax: an unquoted query
    containing a colon raises rather than returning nothing, and a query
    containing `NOT` silently returns the wrong rows. Quoting means a user
    never has to know FTS5 exists.

    Terms are OR-ed. AND is wrong here: retrieval ranks, and one unmatched word
    in a five-word question should cost a chunk rank, not eliminate it.
    """
    terms: dict[str, None] = {}
    for word in _TERM.findall(query):
        terms.setdefault(word.lower(), None)
        for part in split_identifier(word):
            terms.setdefault(part, None)
    if not terms:
        return ""
    return " OR ".join(f'"{t.replace(chr(34), chr(34) * 2)}"' for t in terms)
