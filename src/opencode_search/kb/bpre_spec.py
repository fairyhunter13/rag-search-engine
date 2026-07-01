"""Structural node-kind + ground-truth protocol vocabularies shared by the BPRE generic engine.

P6/HR15 Part B (universal classifier): the per-language `_LANG_SPECS` method-name keyword tables
that used to live here were the last `_SEMANTIC_HEURISTIC_DEBT` entry. They have been retired —
every non-first-class language now classifies via the same four structural/ground-truth signals
(URL-path anchor, `_has_handler_arg` handler-shape, `_V` verb ground-truth, gRPC proto-binding via
`_GRP_SFXS` + discovered `proto_services`) plus `_SCHEMES` receiver-text provenance for non-verb
client idioms. See `bpre_generic.py`/`bpre_paradigms.py`."""
from __future__ import annotations

_V=frozenset({"get","post","put","patch","delete","head","options"})
_CALL_KINDS=frozenset({"call","call_expression","invocation_expression","method_invocation","function_call_expression","member_call_expression","scoped_call_expression","application","method_call","send","funcall","method_call_expression"})
_NOT_CALL=frozenset({"call_suffix"})
def _is_call(k):return k in _CALL_KINDS or(("call" in k or "invocation" in k)and k not in _NOT_CALL)
_NEW_KINDS=frozenset({"object_creation_expression","new_expression","instance_creation_expression","class_instance_creation_expression","constructor_invocation"})
_PARADIGM_KINDS=frozenset({"list_lit","apply","exp_apply","message_expression","command"})
# Structural handler-shape node kinds (function/closure/lambda/block passed as or attached to
# a call) — the route-registration-vs-client-call discriminator. Node-kind map, HR15-exempt,
# same class as _CALL_KINDS/_NEW_KINDS/_PARADIGM_KINDS above (empirically verified per-grammar
# via tree-sitter-language-pack 1.12.1, not guessed).
_HANDLER_KINDS=frozenset({"do_block","lambda_expression","lambda_literal","closure_expression","block","anonymous_subroutine_expression","function_definition"})
_FIRST_CLASS=frozenset({"go","python","typescript","javascript","php"})
# gRPC codegen-contract suffix set — ground truth (not a library guess): a call whose receiver
# text, stripped of one of these suffixes, names a service *discovered* elsewhere in the same
# federation (surface.proto_services) is a gRPC client/stub by construction, regardless of method
# name. Same class as the already-accepted Go `New*Client` / PHP `*Client` binding.
_GRP_SFXS=("ServiceClient","BlockingStub","FutureStub","AsyncStub","Stub","Client","Grpc")
# Closed protocol/URI-scheme vocabulary (ground truth, same class as _V above — not a library-
# name guess list). Each token is a standards-bound protocol/URI noun: http/https (RFC 7230),
# ws/wss (RFC 6455), url/uri (RFC 3986), grpc (the gRPC wire protocol). Used by
# bpre_generic._provenance as the universal non-verb HTTP-client discriminator (P6/HR15 Part B):
# generalizes the already-accepted Go check `"http" in import_path` (bpre_ast.py) from Go's
# import-alias text to every language's call-receiver text.
_SCHEMES=frozenset({"http","https","ws","wss","grpc","url","uri"})
# Import/use-declaration node kinds across non-first-class grammars (P6/HR15 Part C1) — a
# node-kind set, same class as _CALL_KINDS/_NEW_KINDS, not a keyword/vocabulary list. Drives
# _scan_imports (bpre_ast.py): every matched node is a genuine import/use *declaration*, never a
# call-based import idiom (e.g. Lua/Elixir `require(...)`) — those stay receiver-text/type-use
# territory, the documented residue boundary.
_IMPORT_KINDS=frozenset({"import_declaration","import_statement","import_from_statement","import_header","import_or_export","use_declaration","using_directive","use_statement","using_statement","preproc_include","namespace_use_declaration"})
