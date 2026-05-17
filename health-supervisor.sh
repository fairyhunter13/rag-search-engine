#!/bin/bash
# =============================================================================
# opencode search engine health supervisor v2
# =============================================================================
# Monitors the Python embedder (port 9998) and Rust indexer (Unix socket).
# Auto-restarts the embedder (indexer is managed by opencode's spawn.ts).
# Captures crash evidence for AI diagnosis on fatal failures.
#
# Usage: ./health-supervisor.sh [--oneshot] [--interval SECONDS]
#
# Design:
#   - Indexer: opencode spawns it via spawn.ts — supervisor only monitors + alerts
#   - Embedder: supervisor auto-restarts with backoff (max 5 attempts per hour)
#   - Both: crash evidence captured on fatal (permanent) failures
# =============================================================================

set -euo pipefail

OPENDIR="${HOME}/.opencode"
LOG_DIR="${OPENDIR}/health"
mkdir -p "$LOG_DIR"

OPENCODE_BIN="${OPENDIR}/bin"
INDEXER_SOCKET="@opencode-indexer"
EMBEDDER_PORT="${OPENCODE_EMBED_HTTP_PORT:-9998}"
INDEXER_LOG="${OPENDIR}/indexer.log"
EMBEDDER_LOG="${OPENDIR}/embedder.log"
CRASH_DIR="${LOG_DIR}/crashes"
mkdir -p "$CRASH_DIR"

STATE_FILE="${LOG_DIR}/supervisor-state.json"
NOTIFY_ENABLED="${OPENCODE_NO_NOTIFY:-0}"

HEALTH_CHECK_TIMEOUT=5
EMBEDDER_RESTART_COOLDOWN=120   # seconds between embedder restart attempts
MAX_EMBEDDER_RESTARTS_PER_HOUR=5
FATAL_NOTIFY_THRESHOLD=4

# ── Logging ────────────────────────────────────────────────────────────────

log() {
    local level="$1" msg="$2"
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] [${level}] ${msg}" | tee -a "${LOG_DIR}/supervisor.log"
}

# ── State Management ───────────────────────────────────────────────────────

read_state() {
    cat "$STATE_FILE" 2>/dev/null || echo '{}'
}

write_state() {
    echo "$1" > "$STATE_FILE"
}

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

# ── Crash Evidence (lightweight, no hanging commands) ──────────────────────

capture_crash_evidence() {
    local service="$1"
    local ts
    ts=$(date -u +%Y%m%d-%H%M%S)
    local dir="${CRASH_DIR}/${service}-${ts}"
    mkdir -p "$dir"

    log "INFO" "Capturing crash evidence for ${service} → ${dir}"

    # Copy logs
    if [[ "$service" == "embedder" ]]; then
        cp "$EMBEDDER_LOG" "${dir}/embedder.log" 2>/dev/null || true
        cp "${EMBEDDER_LOG}.1" "${dir}/embedder.log.1" 2>/dev/null || true
    fi

    # Quick system snapshot (avoid hanging commands: no lsof, no pgrep)
    {
        echo "=== Time ==="
        date -u
        echo ""
        echo "=== Memory ==="
        free -h 2>/dev/null || true
        echo ""
        echo "=== GPU ==="
        nvidia-smi 2>/dev/null || echo "nvidia-smi not available"
        echo ""
        echo "=== Last 50 embedder log lines ==="
        tail -50 "$EMBEDDER_LOG" 2>/dev/null || true
        echo ""
        echo "=== Errors from embedder log ==="
        grep -i 'error\|fatal\|traceback\|OOM\|killed\|GPU\|CUDA\|signal' "$EMBEDDER_LOG" 2>/dev/null | tail -20 || true
    } > "${dir}/crash-report.txt"

    log "INFO" "Crash evidence saved to ${dir}"
    echo "$dir"
}

# ── Desktop Notification ───────────────────────────────────────────────────

notify() {
    local title="$1" body="$2" urgency="${3:-normal}"
    [[ "$NOTIFY_ENABLED" == "1" ]] && return
    if command -v notify-send &>/dev/null; then
        notify-send -u "$urgency" -a "opencode-health" "$title" "$body" 2>/dev/null || true
    fi
}

# ── Embedder Restart ───────────────────────────────────────────────────────

restart_embedder() {
    log "INFO" "Restarting embedder..."

    local binary=""
    for candidate in \
        "${OPENCODE_BIN}/opencode-embedder/opencode-embedder" \
        "${OPENCODE_BIN}/opencode-embedder"; do
        if [[ -x "$candidate" ]] && [[ -f "$candidate" ]]; then
            binary="$candidate"
            break
        fi
    done

    if [[ -z "$binary" ]]; then
        log "ERROR" "Embedder binary not found"
        return 1
    fi

    # Rotate log
    if [[ -f "$EMBEDDER_LOG" ]]; then
        mv "$EMBEDDER_LOG" "${EMBEDDER_LOG}.1" 2>/dev/null || true
    fi

    # Start embedder WITHOUT parent-pid so it survives supervisor restarts.
    # The embedder's own idle shutdown handles termination when unused.
    OPENCODE_EMBED_HTTP_PORT="$EMBEDDER_PORT" \
    OPENCODE_EMBED_WORKERS="1" \
    OMP_NUM_THREADS="2" \
    nohup "$binary" >>"$EMBEDDER_LOG" 2>&1 &
    local pid=$!
    log "INFO" "Embedder spawned with PID ${pid}"

    # Wait for healthy (up to 90s for model loading)
    local deadline=$(($(date +%s) + 90))
    while [[ $(date +%s) -lt $deadline ]]; do
        if check_embedder; then
            log "INFO" "Embedder healthy on port ${EMBEDDER_PORT}"
            return 0
        fi
        kill -0 "$pid" 2>/dev/null || {
            log "ERROR" "Embedder ${pid} died during startup"
            return 1
        }
        sleep 2
    done

    log "ERROR" "Embedder startup timed out after 90s"
    return 1
}

# ── Main Health Check ──────────────────────────────────────────────────────

run_health_check() {
    local state indexer_ok embedder_ok
    state=$(read_state)

    # ── Indexer: monitor only (opencode's spawn.ts handles restarts) ───
    if check_indexer; then
        indexer_ok=true
    else
        indexer_ok=false
    fi

    local idx_failures
    idx_failures=$(get_field "$state" "indexer_failures" 0)
    if $indexer_ok; then
        idx_failures=0
    else
        idx_failures=$((idx_failures + 1))
    fi

    # ── Embedder: auto-restart with backoff ────────────────────────────
    if check_embedder; then
        embedder_ok=true
    else
        embedder_ok=false
    fi

    local emb_failures emb_restarts last_restart
    emb_failures=$(get_field "$state" "embedder_failures" 0)
    emb_restarts=$(get_field "$state" "embedder_restarts" 0)
    last_restart=$(get_field "$state" "embedder_last_restart" 0)

    if $embedder_ok; then
        emb_failures=0
        emb_restarts=0
    else
        emb_failures=$((emb_failures + 1))
        log "WARN" "Embedder down (failure #${emb_failures})"

        local now elapsed
        now=$(date +%s)
        if [[ "$last_restart" -gt 0 ]]; then
            elapsed=$((now - last_restart))
        else
            elapsed=999
        fi

        if [[ "$emb_restarts" -lt "$MAX_EMBEDDER_RESTARTS_PER_HOUR" && "$elapsed" -ge "$EMBEDDER_RESTART_COOLDOWN" ]]; then
            emb_restarts=$((emb_restarts + 1))

            notify "opencode: Embedder Down" \
                "Auto-restarting (attempt ${emb_restarts}/${MAX_EMBEDDER_RESTARTS_PER_HOUR})..." \
                "critical"

            if restart_embedder; then
                embedder_ok=true
                emb_failures=0
                emb_restarts=0
                log "INFO" "Embedder auto-restart succeeded"
            else
                log "ERROR" "Embedder auto-restart FAILED"
                local report_dir
                report_dir=$(capture_crash_evidence "embedder")
                notify "opencode: Embedder Fatal" \
                    "Restart failed. Crash report: ${report_dir}/crash-report.txt" \
                    "critical"
            fi
        elif [[ "$emb_failures" -ge "$FATAL_NOTIFY_THRESHOLD" ]]; then
            notify "opencode: Embedder Down" \
                "Embedder unreachable for ${emb_failures} checks. Restart limit reached." \
                "critical"
        fi
    fi

    # ── Persist state ─────────────────────────────────────────────────
    state=$(echo "$state" | python3 -c "
import sys, json
d = json.load(sys.stdin)
d['indexer_failures'] = ${idx_failures}
d['embedder_failures'] = ${emb_failures}
d['embedder_restarts'] = ${emb_restarts}
d['embedder_last_restart'] = ${last_restart}
d['indexer_last_ok'] = '$(date -u +%Y-%m-%dT%H:%M:%SZ)'
print(json.dumps(d))
" 2>/dev/null || echo "$state")
    write_state "$state"

    log "INFO" "Health: indexer=$indexer_ok embedder=$embedder_ok"
}

# ── Periodic state reset (prevents permanent lockout) ──────────────────────

reset_hourly_state() {
    local state hour
    state=$(read_state)
    hour=$(get_field "$state" "last_reset_hour" "")
    local now_hour
    now_hour=$(date -u +%H)

    if [[ "$hour" != "$now_hour" ]]; then
        log "INFO" "Hourly state reset — clearing restart counters"
        echo '{"last_reset_hour":"'"$now_hour"'"}' > "$STATE_FILE"
    fi
}

# ── Entry Point ────────────────────────────────────────────────────────────

ONEShot=false
INTERVAL="${OPENCODE_HEALTH_INTERVAL:-60}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --oneshot) ONEShot=true; shift ;;
        --interval) INTERVAL="$2"; shift 2 ;;
        *) log "ERROR" "Unknown arg: $1"; exit 1 ;;
    esac
done

if $ONEShot; then
    run_health_check
    exit 0
fi

log "INFO" "Health supervisor v2 started (interval=${INTERVAL}s)"
trap 'log "INFO" "Stopped"; exit 0' INT TERM

while true; do
    reset_hourly_state
    run_health_check
    sleep "$INTERVAL"
done
