# systemd units and drop-ins

`rag_search.daemon.systemd.install()` writes the **base unit**
(`~/.config/systemd/user/rag-search-mcp-daemon.service`, name from `systemd.UNIT_NAME`).
Everything in `rag-search-mcp-daemon.service.d/` here is an **operator drop-in** and is *not*
installed by that function — deliberately, so an `install()` from a checkout can never quietly
overwrite host-tuned values.

Install or refresh the drop-ins by hand:

```sh
install -d ~/.config/systemd/user/rag-search-mcp-daemon.service.d
cp scripts/systemd/rag-search-mcp-daemon.service.d/*.conf \
   ~/.config/systemd/user/rag-search-mcp-daemon.service.d/
systemctl --user daemon-reload && systemctl --user restart rag-search-mcp-daemon
```

Verify what the *running process* actually got — not what the unit says it would set on the
next start — with `tr '\0' '\n' < /proc/$(systemctl --user show rag-search-mcp-daemon.service
-p MainPID --value)/environ`. That distinction has produced a false alarm here before.

These files are versioned because they were the whole configuration of the daemon while
existing only under `~/.config`: `federation-exclude.conf` alone is what keeps 541,718
duplicate worktree chunks out of a one-core daemon, and losing it went unnoticed by the test
suite until `test_fe8`–`test_fe11` (`src/tests/live/test_federation_exclude.py`) started
reading the live process's environment. Host-specific paths use systemd's `%h`, which *is*
expanded inside `Environment=` in a drop-in file (probed 2026-07-30) though not for
`systemd-run -p Environment=`.
