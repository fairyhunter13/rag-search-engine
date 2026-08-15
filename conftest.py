# pytest loads exactly one config file, and this repo's lives in `src/pyproject.toml` — so a run
# started from the repo root loads none and that config's `norecursedirs` never applies. Collection
# then walked into the sample-project corpus (indexing input, not a suite) and imported
# `ledger-standalone/tests` as `tests`, shadowing the real package: `tests.live` vanished behind one
# import error. Duplicating the config here would be a second copy to keep correct; this is not.
collect_ignore_glob = ["src/tests/fixtures/sample_projects/*"]
