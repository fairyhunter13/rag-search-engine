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

**Read `/healthz` as its own command before that restart.** `systemctl --user restart` bypasses the
409 that `POST /api/reload` returns while a sweeps pause lease is held, and it clears the lease with
no refusal and no log line naming the run it just unpaused. If `sweeps_pause_lease_s` was non-zero,
re-`POST /api/sweeps/pause` afterwards — nothing else will tell you it is gone.

Verify what the *running process* actually got — not what the unit says it would set on the
next start — with `tr '\0' '\n' < /proc/$(systemctl --user show rag-search-mcp-daemon.service
-p MainPID --value)/environ`. That distinction has produced a false alarm here before.

These files are versioned because they were the whole configuration of the daemon while existing
only under `~/.config`, and losing one went unnoticed by the test suite until SE1/SE2
(`src/tests/live/test_systemd_effective_config.py`) started comparing them to the running process.

## The federation exclusion is no longer one of them

`federation-exclude.conf` used to carry `RSE_FEDERATION_EXCLUDE=*/_worktrees/*`, the pattern that
keeps 541,718 duplicate worktree chunks out of a one-core daemon. An environment variable reaches
only the processes systemd handed it to, so the CLI, the live suite and every script ran
`register_all_members()` without it and upserted the excluded members straight back as
`enabled=True` — 58 of them, measured 2026-07-30, with the drop-in installed and in force.

Layout patterns now live in the federation root's `.rse-index.yaml`, which every process reads:

```yaml
federation:
  exclude:
    - '*/_worktrees/*'
```

The variable stays for **host-specific absolute paths only** — the ones a tracked file must not
carry (HR34). Nothing versioned here declares it, so write the drop-in yourself:

```sh
cat > ~/.config/systemd/user/rag-search-mcp-daemon.service.d/federation-exclude.conf <<'EOF'
[Service]
Environment=RSE_FEDERATION_EXCLUDE=%h/git/github.com/<owner>/<repo>
EOF
```

`%h` *is* expanded inside `Environment=` in a drop-in file (probed 2026-07-30), though not for
`systemd-run -p Environment=`. Join multiple paths with `:`. SE3 gates the file half, FE8 the union.
