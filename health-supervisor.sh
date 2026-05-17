#!/bin/bash
# =============================================================================
# opencode search engine health supervisor v3
# =============================================================================
# Monitor only. Indexer is spawned by opencode's spawn.ts (lazy).
# Embedder is spawned by the indexer via ensure_embedder() (lazy).
# Both have connection-aware idle shutdown — they die when unused.
#
# This supervisor captures crash evidence on fatal failures and sends
# desktop notifications. It does NOT auto-restart either service.
# =============================================================================

set -euo pipefail

OPENDIR="${HOME}/.opencode"
LOG_DIR="${OPENDIR}/health"
mkdir -p "$LOG_DIR"

INDEXER_SOCKET="@opencode-indexer"
EMBEDDER_PORT="${OPENCODE_EMBED_HTTP_PORT:-9998}"
EMBEDDER_LOG="${OPENDIR}/embedder.log"
CRASH_DIR="${LOG_DIR}/crashes"
mkdir -p "$CRASH_DIR"

STATE_FILE="${LOG_DIR}/supervisor-state.json"
HEALTH_CHECK_TIMEOUT=5
FATAL_NOTIFY_THRESHOLD=5

# ── Logging ────────────────────────────────────────────────────────────────

log() {
    local level="$1" msg="$2"
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] [${level}] ${msg}" | tee -a "${LOG_DIR}/supervisor.log"
}

# ── State ──────────────────────────────────────────────────────────────────

read_state() { cat "$STATE_FILE" 2>/dev/null || echo '{}'; }
write_state() { echo "$1" > "$STATE_FILE"; }

get_field() {
    local state="$1" field="$2" default="${3:-0}"
    echo "$state" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('${field}', ${default}))" 2>/dev/null || echo "$default"
}

# ── Health Checks ──────────────────────────────────────────────────────────

check_indexer() {
    local resp
    resp=$(echo '{"method":"ping","params":{}}' | timeout "$HEALTH_CHECK_TIMEOUT" nc -U "$INDEXER_SOCKET" 2>/dev/null || true)
    echo "$resp" | grep -q '"pong"'
}

check_embedder() {
    local code
    code=$(curl -s -o /dev/null -w "%{http_code}" --max-time "$HEALTH_CHECK_TIMEOUT" "http://127.0.0.1:${EMBEDDER_PORT}/health" 2>/dev/null || echo "000")
    [[ "$code" == "200" ]]
}

# ── Crash Evidence ─────────────────────────────────────────────────────────

capture_crash_evidence() {
    local service="$1"
    local ts dir
    ts=$(date -u +%Y%m%d-%H%M%S)
    dir="${CRASH_DIR}/${service}-${ts}"
    mkdir -p "$dir"

    log "INFO" "Capturing crash evidence for ${service} → ${dir}"

    {
        echo "# Crash: ${service}  $(date -u)"
        echo ""
        echo "## System State"
        echo '```'
        free -h 2>/dev/null || true
        nvidia-smi 2>/dev/null || echo "nvidia-smi not available"
        echo '```'
        echo ""
        echo "## Embedder Log (last 50 lines)"
        echo '```'
        tail -50 "$EMBEDDER_LOG" 2>/dev/null || true
        echo '```'
        echo ""
        echo "## Errors"
        echo '```'
        grep -i 'error\|fatal\|traceback\|OOM\|killed\|signal\|GPU\|CUDA' "$EMBEDDER_LOG" 2>/dev/null | tail -20 || true
        echo '```'
    } > "${dir}/crash-report.md"

    if [[ "$service" == "embedder" ]]; then
        cp "$EMBEDDER_LOG" "${dir}/embedder.log" 2>/dev/null || true
        cp "${EMBEDDER_LOG}.1" "${dir}/embedder.log.1" 2>/dev/null || true
    fi

    log "INFO" "Crash evidence saved to ${dir}"
}

# ── Notification ───────────────────────────────────────────────────────────

notify() {
    local title="$1" body="$2" urgency="${3:-normal}"
    if command -v notify-send &>/dev/null; then
        notify-send -u "$urgency" -a "opencode-health" "$title" "$body" 2>/dev/null || true
    fi
}

# ── Main Check ─────────────────────────────────────────────────────────────

run_health_check() {
    local state indexer_ok embedder_ok
    state=$(read_state)

    # Indexer
    indexer_ok=false
    check_indexer && indexer_ok=true

    local idx_failures
    idx_failures=$(get_field "$state" "indexer_failures" 0)
    $indexer_ok && idx_failures=0 || idx_failures=$((idx_failures + 1))

    if [[ "$idx_failures" -ge "$FATAL_NOTIFY_THRESHOLD" ]] && [[ "$idx_failures" -eq "$FATAL_NOTIFY_THRESHOLD" ]]; then
        notify "opencode: Indexer Down" \
            "Indexer unreachable for ${idx_failures} checks. opencode will restart it on next use." \
            "normal"
    fi

    # Embedder
    embedder_ok=false
    check_embedder && embedder_ok=true

    local emb_failures
    emb_failures=$(get_field "$state" "embedder_failures" 0)
    $embedder_ok && emb_failures=0 || emb_failures=$((emb_failures + 1))

    if [[ "$emb_failures" -ge "$FATAL_NOTIFY_THRESHOLD" ]] && [[ "$emb_failures" -eq "$FATAL_NOTIFY_THRESHOLD" ]]; then
        notify "opencode: Embedder Down" \
            "Embedder unreachable for ${emb_failures} checks. Indexer will restart it on next use." \
            "normal"
        capture_crash_evidence "embedder"
    fi

    # Persist
    state=$(echo "$state" | python3 -c "
import sys, json
d = json.load(sys.stdin)
d['indexer_failures'] = ${idx_failures}
d['embedder_failures'] = ${emb_failures}
print(json.dumps(d))
" 2>/dev/null || echo "$state")
    write_state "$state"

    log "INFO" "Health: indexer=${indexer_ok} embedder=${embedder_ok}"
}

# ── Entry Point ────────────────────────────────────────────────────────────

ONEShot=false
INTERVAL="${OPENCODE_HEALTH_INTERVAL:-60}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --oneshot) ONEShot=true; shift ;;
        --interval) INTERVAL="$2"; shift 2 ;;
        *) log "ERROR" "Unknown: $1"; exit 1 ;;
    esac
done

if $ONEShot; then
    run_health_check
    exit 0
fi

log "INFO" "Health supervisor v3 started (monitor-only, interval=${INTERVAL}s)"
trap 'log "INFO" "Stopped"; exit 0' INT TERM

while true; do
    run_health_check
    sleep "$INTERVAL"
done
