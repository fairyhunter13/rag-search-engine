---
type: Policy
resource: tests/test_public_hygiene.py
title: Evidence from a private corpus travels as a measurement, never as an identifier
description: "This repository is public and it is fed by private corpora. A count carries the finding and names nothing, so a count is what gets written down. The ban list cannot enforce this: it holds the names somebody thought to add, and a type name borrowed from a client's tree is never one of them."
tags: [hygiene, publishing, evidence, policy]
status: stable
generated: { by: claude/opus-5, at: 2026-09-22T16:00:00Z }
sources:
  - id: gate
    resource: tests/test_public_hygiene.py
---

# The rule

A finding measured on a private repository is written down as its numbers. 4 declarations,
17 references, 25 module rows, 0 resolved. None of those names anything.

An identifier read out of that tree does not travel. Not a type, not a method, not a file
path, not a package, not a directory. Where the prose needs a name to be readable, it uses an
invented one and says so, or elides it as `'...'`.

A reproduction is written from scratch against a synthetic corpus, built in a `tmp_path`, so the
test that holds a fix never reads a private tree and cannot leak one.

# Why the ban list is not this rule

`test_public_hygiene.py` reads `CODERAG_NAME_BAN` and refuses any tracked file that carries a
banned term. That gate is necessary and it is not sufficient. A ban list holds the terms somebody
thought to add, so it catches a client's name and an organisation's name. It cannot catch a
service type lifted out of that client's source, because nobody knew the type existed until
the moment it was pasted.

That is what happened in the sibling engine: a defect concept quoted a receiver expression from
the corpus it was measured on, and every gate in that repository passed it. The tracked tree was
clean against 53 terms, the history was clean, and the sentence was still wrong to publish.

The rule is written here because this engine is exposed the same way. It is public, and the
projects it federates include private clones reached through a `repositories/` directory. A
measurement taken on that corpus reaches a public bundle by exactly the same route.

So this is a policy and not a test. The reviewer is the gate, and the question to ask of any
sentence about a private corpus is whether it would still read the same with every name
removed. Where it would, the names were never carrying the finding.

# What the numbers may say

A count, a share, a ratio, a file count, a line count, a language name, and the shape of an
expression as a placeholder — `svc.Field.Method(...)` — are all safe, because none of them is
recoverable to a repository. The test that the shape is safe is whether it fits a thousand
other codebases. Where it fits one, it is an identifier wearing a placeholder's clothes.
