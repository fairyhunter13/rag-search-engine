---
type: Defect
resource: src/coderag/filters.py
title: The secret filter had a dash-shaped hole, and three files went through it
description: "`*.env` needs a literal dot, so `laravel-env` was indexed with 287 value-bearing KEY=value lines, `env-template` with 197 and `.env-example` with 9. `*.template` does not match `env-template` either, so the two templates were missed rather than exempted. Indexed by accident, which is the same outcome as the one that is not."
tags: [filters, secrets, indexing]
status: stable
generated: { by: claude/opus-5, at: 2026-08-21T00:00:00Z }
---

# What was in the stores

`_SECRET_GLOBS` already carried `.env.*`, `*.env` and `*.env.*` — three patterns because `fnmatch`
anchors the whole name and one of them was never enough. All three need a **literal dot**. The dash
spellings are as common and matched none of them:

| indexed file | chunks | value-bearing `KEY=value` lines |
|---|---|---|
| `laravel-env` | 39 | 287 |
| `env-template` | 25 | 197 |
| `.env-example` | 1 | 9 |

`*-env`, `*-env.*`, `env-*` and `*.env-*` close it.

# The part that is not about the dash

The lower two rows are templates, and templates are *supposed* to index. `_SECRET_EXEMPT` exists
because a `.env.example` is frequently the only documentation of what a service needs. But
`*.template` does not match `env-template` any more than `*.env` does, so neither reached the
exemption. They were indexed because nothing looked at them, not because something decided they
were safe. Those read
identically from the store and differently from the code. `_SECRET_EXEMPT` gained the dash forms in
the same change, so the two files now index on a decision.

By contrast the 797 assignments inside correctly-named `.env.example` and `.dist` files were the
exemption working exactly as designed, and are not a finding.

# Why the scanner denylists were not copied wholesale

`shhgit` flags `settings.py`, `config.php`, `database.yml`, `LocalSettings.php` and `*.sql` as
secret-bearing. Every one of them is source, and a repo whose Django settings module is
unsearchable is worse off than one that indexed a key-name template. `gitleaks` ships no filename
denylist at all, which is the same judgment from the other end. What both agree on is the key,
keystore, and env family.

Plus the shell and REPL histories, which is where a pasted token actually lands. Nobody writes one
there deliberately, so nobody redacts one. That intersection is what went in.

This is the one filter in the file whose failure re-indexing does not undo. That is why it is
worth a concept, and the image and binary lists are not.
