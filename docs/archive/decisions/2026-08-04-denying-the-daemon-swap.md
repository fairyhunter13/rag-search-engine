# Denying the daemon swap

**2026-08-04.** `MemorySwapMax=0` is now a drop-in
(`scripts/systemd/rag-search-mcp-daemon.service.d/no-swap.conf`), applied and verified. It is a
preference for loud failure, not a throughput result, and this records it as such so nobody later
quotes it as one.

## What was already bound, and what was not

`MemoryHigh=4G`, `MemoryMax=6G`, `OOMPolicy=stop`, `Delegate=no` and a 1-core CPU quota were all
set. What was unbound is the region *between* `MemoryHigh` and `MemoryMax`: a spike past 4 GiB
degraded into swap, where the daemon keeps answering — slowly, with nothing in `/healthz` naming the
cause. A 1.5 GB resident embedder paged out mid-sweep presents as an unrelated latency problem.

With swap denied, that same spike is an OOM kill and `Restart=always` returns the daemon. The trade
is one cold GPU start in exchange for a class of silent degradation becoming visible. It is the same
doctrine as GPU-only (P0): a fallback must raise rather than quietly degrade.

## It was not hypothetical

The outgoing process's own accounting, on the restart that applied this:

```
rag-search-mcp-daemon.service: Consumed 44min 2.844s CPU time, 3.1G memory peak, 16.7M memory swap peak.
```

**16.7 MB of swap peak** over a 46-minute fleet re-derive. Small, and it is the mechanism rather
than the magnitude that matters — nothing reported it, and nothing would have reported ten times as
much.

## Verified at the cgroup, not at the unit

`systemctl show` reports what the unit *would* set on the next start. The check that matters is what
the running process got:

```
$ cat /sys/fs/cgroup/.../rag-search-mcp-daemon.service/memory.swap.max
0
```

with `MemoryHigh=4294967296`, `MemoryMax=6442450944`, `OOMPolicy=stop` unchanged, `NRestarts=0`, and
`/healthz` returning `ok` at 1057 MB RSS afterwards. That unit-versus-process distinction has
produced a false alarm in this repo before, which is why the drop-in README already insists on it.

## One cost worth knowing

The daemon took **~2.5 minutes** to bind :8765 after the restart — `active` and logging
(`watcher: armed 150 roots`) well before the port existed. So `systemctl --user is-active` is not a
readiness check, and neither is the absence of errors. Poll the port or `/healthz`.

## When this was applied, and why the timing was not incidental

After the e13 fleet re-derive reached `stale 0 / 150`, not during it. The change alters OOM behaviour
under exactly the memory pressure a re-derive produces, and a restart mid-derive would also have
cleared any sweeps pause lease silently. `/healthz` was read as its own command first
(`sweeps_pause_lease_s: 0.0` — nothing to clear, so nothing to restore).
