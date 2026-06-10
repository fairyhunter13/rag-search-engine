# Pre-Deploy Verification

Run this skill before deploying any change to the local opencode-search device.
It validates that all MCP surfaces, KB health, and system integrity are correct.

## When to run
- Before merging or deploying any code change to this machine
- After major indexing operations (hierarchy build, pipeline runs)
- When the user says "deploy", "push to device", "is everything ready?"

## Verification steps

### 1. Daemon health
```bash
curl -sf http://localhost:8765/healthz | python3 -c "import json,sys; d=json.load(sys.stdin); print('PASS' if d.get('ok') else 'FAIL: '+str(d))"
```

### 2. GPU state (must be active, not CPU)
```bash
nvidia-smi --query-gpu=name,utilization.gpu,memory.used,memory.total --format=csv,noheader
```
GPU must be present. If embedding fails with "CPU fallback" — STOP, do not deploy.

### 3. Fast test suite (no slow LLM tests)
```bash
.venv/bin/pytest src/tests/live/ -m "live and not slow" -q --tb=short 2>&1 | tail -20
```
**All tests must pass. Zero failures, zero skips.**
If any test fails — investigate and fix before proceeding.

### 4. MCP tool surface check (use opencode-search tools)
Run these 5 checks against astro-project:
- `search("payment handler")` → must return results
- `ask("how does auth work", scope="all")` → must return non-empty answer
- `graph("main", relation="callers")` → must return graph nodes
- `overview(what="structure")` → must show 20000+ files, 4500+ communities
- `federation("/home/user/git/github.com/fairyhunter13/astro-project")` → must list 24+ members

### 5. Lint check
```bash
.venv/bin/ruff check src/opencode_search src/tests --quiet 2>&1 | head -20
```
Must be clean (or only pre-existing E501 lines ≤ 5).

### 6. Git state (zero-unpushed policy)
```bash
git -C /home/user/git/github.com/fairyhunter13/opencode-search-engine log --oneline @{u}.. 2>/dev/null | wc -l
```
Must be 0. If unpushed commits exist — push before deploying.

### 7. Systemd service health
```bash
systemctl --user is-active opencode-search-mcp-daemon
```
Must output `active`.

## Pass/fail criteria
- ALL 7 checks must pass
- If any check fails: fix it, re-run the failing step, confirm fix before deploying
- Never skip a step — a silent failure here becomes a production incident

## Final deploy checklist
- [ ] Daemon healthy (healthz OK)
- [ ] GPU active (CUDA, not CPU)
- [ ] Fast tests: 0 failures, 0 skips
- [ ] All 5 MCP tools return valid results
- [ ] Lint clean
- [ ] Zero unpushed commits
- [ ] Systemd service active

Only deploy when all 7 boxes are checked.
