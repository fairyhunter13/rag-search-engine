# The span scan was never hot, and bisect does not answer the same question

*2026-07-31*

A backlog item proposed replacing the caller-attribution scan in `_extract_graph`
(`daemon/sweeps.py:676-681`) with a `bisect` over the sorted span list, selecting the
innermost enclosing symbol by greatest start line. It was filed as **"pure speedup,
byte-identical output."** Both halves of that are false, and the second one matters more
than the first.

Withdrawn. Nothing shipped. This records the measurement so it is not re-derived.

## The scan

For each call site, find the symbol whose span encloses it — the caller:

```python
for sl, el, sid in sym_spans:
    if sl <= call_line <= el:
        span = el - sl
        if caller_sid == "" or span < best_span:
            best_span, caller_sid = span, sid
```

Linear over one file's spans, selecting **minimum width**. The proposal was to sort once,
`bisect` to the last span starting at or before the call line, and walk back to the first
enclosing one — selecting **maximum start line**.

## Premise 1: not a hot loop

Measured over all 150 graph stores post-`e8`, spans per file:

| population | files | median | mean | p90 | p99 | max |
|---|---:|---:|---:|---:|---:|---:|
| all files with symbols | 36,086 | 5 | 10.5 | 25 | 65 | 506 |
| files defining ≥1 callable | 25,309 | **4** | 7.1 | 15 | 51 | 506 |

The second row is the closer proxy for the population that matters: the loop only runs for
files present in `calls_by_file`, and a file defining no callable is unlikely to hold call
sites. It is a proxy, not a filter — a top-level script can call without defining — but such
files are few and their span lists are shorter still. **A median of four elements.** 1.02% of
files exceed 64 spans. `bisect` on a four-element list is not a speedup, and the loop it would
replace has no allocation and one comparison per element.

The measured hot spot in extraction was always IPC, and that was addressed separately by
merging the two extraction passes — worth ~11% end to end, itself much smaller than filed.

## Premise 2: not byte-identical

Minimum-width and maximum-start-line agree only when enclosing spans are **properly
nested**. They are not always nested:

| | pairs |
|---|---:|
| partially-overlapping span pairs (one starts inside the other, ends outside) | **374** |
| pairs where the two rules select a different symbol | **249** |
| spans sharing an identical `(start, end)` | **8,390** |

On each of those the change silently moves a call to a different caller — which moves an
edge, which moves a community. That is a behaviour change requiring an `EXTRACTOR_REV` bump
and a full fleet re-derive, sold as a free refactor.

### The identical-span figure needs the restriction to mean anything

Measured over the whole symbol table the identical-span count is **212,812**, 25× larger.
Almost all of it is an artifact: `data` symbols in `.json` and `.csv` files, where every key
and array index on one line shares that line's span. A single one-line `include` array
contributes `n(n-1)/2` pairs by itself; 3,765 such symbols generate the bulk of the 212,812.

Those files contain no call sites, so the scan never visits them. Counting them would have
overstated the disagreement surface by a factor of 25 and made the conclusion look far
stronger than the evidence supports. The honest number is 8,390 — still not zero, still not
"byte-identical".

## What would make this worth revisiting

An identity-preserving variant exists: `bisect` only to bound the prefix of candidate spans,
then keep the min-width scan over that prefix. It preserves the current answer exactly. It
also saves roughly half of a four-element loop, which is why it was not built either.

The rule for the next reader: **a proposal that changes which symbol owns a call is a
correctness change, not an optimization** — regardless of how it is filed. Price it with a
stamp bump and a re-derive, or do not price it at all.

## The other retirement

The `.html` de-duplication item was closed the same day by measurement rather than code —
see [87.8% of the `.html` corpus is byte-identical copies](2026-07-31-html-duplication-is-within-project.md).
Both items are now off the backlog.
