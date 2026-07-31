# Refusing to reload the daemon under a sweeps lease

**2026-07-29** · guard: RL1/RL2 in `test_p6_daemon.py`

`POST /api/reload` (default, or explicit `?restart=true`) exits non-zero, so the unit's
`Restart=on-failure` policy restarts it via systemd in ~1 s. `POST /api/reload?restart=false`
exits cleanly (0) and intentionally stays down (used by `daemon stop`) — that path needs a manual
`systemctl --user restart rag-search-mcp-daemon` to bring it back up. There is no `daemon reload`
CLI subcommand; only `daemon serve/status/ensure/stop/install-global/install-systemd/bridge-stdio`
exist.

**Both paths now refuse with `409` while a sweeps pause lease is held.** A lease means a live
suite or `scripts/purge_unindexable.py` owns this daemon, and restarting under one is what put a
19-second hole under a CI run: reload accepted 20:15:55, serving again 20:16:14; the suite's CB2
hit `:8765` at 20:16:01 and went red naming neither party.

The reply carries `lease_remaining_s`. `?force=true` overrides, which is what
`test_api_reload_returns_reloading` passes because it is the one caller that genuinely means to
restart mid-suite. The lease self-expires after 30 min, so a dead client cannot wedge the route
shut.

**This is the collision class the pytest-vs-pytest gate does not cover** — see
[one live suite at a time](2026-07-30-one-live-suite-at-a-time.md). The colliding party there was
a bare `curl`, not a second suite, so a gate keyed on contending pytest processes could never
have seen it.
