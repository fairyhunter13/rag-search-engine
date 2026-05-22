# Development

Canonical local workflow for this repository:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip setuptools wheel
.venv/bin/python -m pip install -e 'src[dev,gpu]'
./scripts/validate-local-gpu.sh
```

Rules for local validation:

- GPU is mandatory. CPU fallback is forbidden.
- `./scripts/validate-local-gpu.sh` is the canonical validation entrypoint.
- Validation is strict: any skipped test fails the run.
- The script uses the repo-local `.venv` automatically when present.

What validation runs:

- runtime dependency presence checks
- GPU provider validation through ONNX Runtime
- `ruff`
- Python bytecode compilation
- the full Python test suite
- the real CLI end-to-end smoke script

Reproducibility:

- Use the checked-in lock file [requirements-lock-py312-linux-gpu.txt](/home/user/git/github.com/fairyhunter13/opencode-search-engine/requirements-lock-py312-linux-gpu.txt) for this Python 3.12 Linux GPU environment
- Refresh it from the repo-local `.venv` with:

```bash
./scripts/refresh-lock.sh
```

Reindex and migration rules:

- Run `opencode-search index <project> --tier <tier> --force` after changing schema, chunking, language detection, embedding model, embedding dimensions, or tier.
- Mixed-tier federated search is intentionally rejected. Reindex projects to the same tier when they need to be searched together.
- Per-project indexes live under `<project>/.opencode/index_<tier>/`; removing that directory and running `index --force` is the clean rebuild path.

Release checklist:

- Refresh the lock file if dependencies changed.
- Run `./scripts/validate-local-gpu.sh`.
- Confirm the validation output has zero skipped tests.
- Smoke MCP usage from the target assistant config if the release changes MCP wiring.
