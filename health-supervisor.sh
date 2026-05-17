#!/bin/bash
# =============================================================================
# opencode search engine health supervisor
# =============================================================================
# Monitors the Rust indexer and Python embedder services, auto-restarts them
# for known-recoverable issues, captures crash evidence for AI diagnosis,
# and sends desktop notifications for fatal/unrecoverable problems.
#
# Usage: ./health-supervisor.sh [--oneshot] [--interval SECONDS]
#   --oneshot    Run once and exit (for cron/systemd timer)
#   --interval   Seconds between checks (default: 30, daemon mode only)
#
# Environment:
#   OPENCODE_NO_NOTIFY=1        Disable desktop notifications
#   OPENCODE_HEALTH_INTERVAL    Override check interval (seconds)
# =============================================================================

set -euo pipefail

# ── Configuration ──────────────────────────────────────────────────────────

OPENDIR="${HOME}/.opencode"
LOG_DIR="${OPENDIR}/health"
mkdir -p "$LOG_DIR"

INDEXER_SOCKET="@opencode-indexer"
EMBEDDER_PORT="${OPENCODE_EMBED_HTTP_PORT:-9998}"
INDEXER_LOG="${OPENDIR}/indexer.log"
EMBEDDER_LOG="${OPENDIR}/embedder.log"
CRASH_DIR="${LOG_DIR}/crashes"
mkdir -p "$CRASH_DIR"

STATE_FILE="${LOG_DIR}/supervisor-state.json"
NOTIFY_ENABLED="${OPENCODE_NO_NOTIFY:-0}"

# Health check thresholds
HEALTH_CHECK_TIMEOUT=5
INDEXER_STARTUP_TIMEOUT=30
MAX_AUTO_RESTARTS=3
RESTART_COOLDOWN=60        # seconds between restart attempts
FATAL_NOTIFY_THRESHOLD=5   # consecutive failures before notification

# ── Logging ────────────────────────────────────────────────────────────────

log() {
    local level="$1" msg="$2"
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] [${level}] ${msg}" | tee -a "${LOG_DIR}/supervisor.log"
}

# ── State Management ───────────────────────────────────────────────────────

read_state() {
    if [[ -f "$STATE_FILE" ]]; then
        cat "$STATE_FILE" 2>/dev/null || echo '{}'
    else
        echo '{}'
    fi
}

write_state() {
    echo "$1" > "$STATE_FILE"
}

get_state_field() {
    local state="$1" field="$2" default="${3:-0}"
    echo "$state" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('${field}', ${default}))" 2>/dev/null || echo "$default"
}

# ── Health Checks ──────────────────────────────────────────────────────────

check_indexer_health() {
    # Probe via abstract Unix socket: POST /ping expects "pong"
    local resp
    resp=$(echo '{"method":"ping","params":{}}' | timeout "$HEALTH_CHECK_TIMEOUT" nc -U "$INDEXER_SOCKET" 2>/dev/null || true)
    if echo "$resp" | grep -q '"pong"'; then
        return 0
    fi
    return 1
}

check_embedder_health() {
    # Probe via HTTP: GET /health expects 200 with JSON
    local http_code
    http_code=$(curl -s -o /dev/null -w "%{http_code}" --max-time "$HEALTH_CHECK_TIMEOUT" "http://127.0.0.1:${EMBEDDER_PORT}/health" 2>/dev/null || echo "000")
    if [[ "$http_code" == "200" ]]; then
        return 0
    fi
    return 1
}

# ── Crash Evidence Capture ─────────────────────────────────────────────────

capture_crash_evidence() {
    local service="$1"  # "indexer" or "embedder"
    local timestamp
    timestamp=$(date -u +%Y%m%d-%H%M%S)
    local report_dir="${CRASH_DIR}/${service}-${timestamp}"
    mkdir -p "$report_dir"

    log "WARN" "Capturing crash evidence for ${service} → ${report_dir}"

    # Copy the service log
    if [[ "$service" == "indexer" && -f "$INDEXER_LOG" ]]; then
        cp "$INDEXER_LOG" "${report_dir}/indexer.log" 2>/dev/null || true
        cp "${INDEXER_LOG}.old" "${report_dir}/indexer.log.old" 2>/dev/null || true
    elif [[ "$service" == "embedder" && -f "$EMBEDDER_LOG" ]]; then
        cp "$EMBEDDER_LOG" "${report_dir}/embedder.log" 2>/dev/null || true
        cp "${EMBEDDER_LOG}.1" "${report_dir}/embedder.log.1" 2>/dev/null || true
    fi

    # Capture process tree
    {
        echo "=== Process Tree ==="
        ps auxf 2>/dev/null || ps aux
        echo ""
        echo "=== Memory ==="
        free -h 2>/dev/null || true
        echo ""
        echo "=== GPU (nvidia-smi) ==="
        nvidia-smi 2>/dev/null || echo "nvidia-smi not available"
        echo ""
        echo "=== GPU (rocm-smi) ==="
        rocm-smi --showproductname 2>/dev/null || echo "rocm-smi not available"
        echo ""
        echo "=== Open Files ==="
        lsof -p "$(pgrep -f "opencode-indexer" | head -1)" 2>/dev/null || echo "no indexer process"
        echo ""
        echo "=== Socket State ==="
        ss -xl 2>/dev/null | grep opencode || echo "no opencode sockets"
        echo ""
        echo "=== Disk ==="
        df -h "${OPENDIR}" 2>/dev/null || true
    } > "${report_dir}/system-state.txt" 2>/dev/null

    # Crash summary for AI
    {
        echo "# Crash Report: ${service}"
        echo "**Time:** $(date -u +'%Y-%m-%d %H:%M:%S UTC')"
        echo "**Service:** ${service}"
        echo ""
        echo "## Last 50 Log Lines"
        echo '```'
        if [[ "$service" == "indexer" && -f "$INDEXER_LOG" ]]; then
            tail -50 "$INDEXER_LOG" 2>/dev/null
        elif [[ "$service" == "embedder" && -f "$EMBEDDER_LOG" ]]; then
            tail -50 "$EMBEDDER_LOG" 2>/dev/null
        fi
        echo '```'
        echo ""
        echo "## Error Summary"
        echo '```'
        if [[ "$service" == "indexer" && -f "$INDEXER_LOG" ]]; then
            grep -i 'error\|fatal\|panic\|SIGSEGV\|SIGABRT\|OOM\|killed' "$INDEXER_LOG" 2>/dev/null | tail -20 || echo "no errors found"
        elif [[ "$service" == "embedder" && -f "$EMBEDDER_LOG" ]]; then
            grep -i 'error\|fatal\|traceback\|exception\|SIGSEGV\|SIGABRT\|OOM\|killed\|GPU\|CUDA\|ROCm' "$EMBEDDER_LOG" 2>/dev/null | tail -20 || echo "no errors found"
        fi
        echo '```'
        echo ""
        echo "## Recovery Actions Attempted"
        echo "- Auto-restart attempt: $(get_state_field "$state" "${service}_restarts" 0)"
        echo "- Last restart: $(get_state_field "$state" "${service}_last_restart" 'never')"
    } > "${report_dir}/crash-report.md"

    log "INFO" "Crash evidence saved to ${report_dir}"
    echo "$report_dir"
}

# ── Desktop Notification ───────────────────────────────────────────────────

notify() {
    local title="$1" body="$2" urgency="${3:-normal}"
    if [[ "$NOTIFY_ENABLED" == "1" ]]; then
        return
    fi

    # Try multiple notification methods
    if command -v notify-send &>/dev/null; then
        notify-send -u "$urgency" -a "opencode-health" "$title" "$body" 2>/dev/null || true
    elif command -v osascript &>/dev/null; then
        osascript -e "display notification \"${body}\" with title \"${title}\"" 2>/dev/null || true
    fi
}

# ── Service Restart ────────────────────────────────────────────────────────

restart_indexer() {
    log "INFO" "Attempting to restart indexer daemon..."

    # Kill any existing indexer
    pkill -f "opencode-indexer" 2>/dev/null || true
    sleep 2

    # Find the binary
    local binary=""
    for candidate in \
        "${OPENDIR}/bin/opencode-indexer" \
        "${HOME}/.opencode/bin/opencode-indexer" \
        "/usr/local/bin/opencode-indexer"; do
        if [[ -x "$candidate" ]] && [[ -f "$candidate" ]]; then
            binary="$candidate"
            break
        fi
    done

    if [[ -z "$binary" ]]; then
        log "ERROR" "Cannot restart indexer: binary not found"
        return 1
    fi

    # Rotate log if > 10MB
    if [[ -f "$INDEXER_LOG" ]]; then
        local size
        size=$(stat -c%s "$INDEXER_LOG" 2>/dev/null || echo 0)
        if [[ "$size" -gt 10485760 ]]; then
            mv "$INDEXER_LOG" "${INDEXER_LOG}.old" 2>/dev/null || true
        fi
    fi

    # Start the indexer (matching how spawn.ts does it)
    nohup ionice -c3 nice -n 10 "$binary" \
        --parent-pid "$$" \
        >>"$INDEXER_LOG" 2>&1 &
    local pid=$!
    log "INFO" "Indexer spawned with PID ${pid}"

    # Wait for it to become healthy
    local deadline=$(($(date +%s) + INDEXER_STARTUP_TIMEOUT))
    while [[ $(date +%s) -lt $deadline ]]; do
        if check_indexer_health; then
            log "INFO" "Indexer healthy on socket ${INDEXER_SOCKET}"
            return 0
        fi
        # Check if process died
        if ! kill -0 "$pid" 2>/dev/null; then
            log "ERROR" "Indexer process ${pid} died during startup"
            return 1
        fi
        sleep 1
    done

    log "ERROR" "Indexer startup timed out after ${INDEXER_STARTUP_TIMEOUT}s"
    return 1
}

restart_embedder() {
    log "INFO" "Attempting to restart embedder..."

    # Find the binary
    local binary=""
    for candidate in \
        "${OPENDIR}/bin/opencode-embedder/opencode-embedder" \
        "${OPENDIR}/bin/opencode-embedder"; do
        if [[ -x "$candidate" ]] && [[ -f "$candidate" ]]; then
            binary="$candidate"
            break
        fi
    done

    if [[ -z "$binary" ]]; then
        log "ERROR" "Cannot restart embedder: binary not found at expected paths"
        return 1
    fi

    # Rotate log
    if [[ -f "$EMBEDDER_LOG" ]]; then
        mv "$EMBEDDER_LOG" "${EMBEDDER_LOG}.1" 2>/dev/null || true
    fi

    # Start embedder with parent PID monitoring
    OPENCODE_EMBEDDER_PARENT_PID="$$" \
    OPENCODE_EMBED_HTTP_PORT="$EMBEDDER_PORT" \
    OPENCODE_EMBED_WORKERS="1" \
    OMP_NUM_THREADS="2" \
    nohup "$binary" >>"$EMBEDDER_LOG" 2>&1 &
    local pid=$!
    log "INFO" "Embedder spawned with PID ${pid}"

    # Wait for it to become healthy
    local deadline=$(($(date +%s) + 90))  # 90s for model loading
    while [[ $(date +%s) -lt $deadline ]]; do
        if check_embedder_health; then
            log "INFO" "Embedder healthy on port ${EMBEDDER_PORT}"
            return 0
        fi
        if ! kill -0 "$pid" 2>/dev/null; then
            log "ERROR" "Embedder process ${pid} died during startup"
            return 1
        fi
        sleep 2
    done

    log "ERROR" "Embedder startup timed out after 90s"
    return 1
}

# ── Main Health Check Loop ─────────────────────────────────────────────────

run_health_check() {
    local state indexer_ok embedder_ok
    state=$(read_state)

    # ── Check Indexer ──────────────────────────────────────────────────
    if check_indexer_health; then
        indexer_ok=true
        state=$(echo "$state" | python3 -c "
import sys, json
d = json.load(sys.stdin)
d['indexer_failures'] = 0
d['indexer_restarts'] = 0
d['indexer_last_ok'] = '$(date -u +%Y-%m-%dT%H:%M:%SZ)'
print(json.dumps(d))
" 2>/dev/null || echo "$state")
    else
        indexer_ok=false
        local failures restarts last_restart elapsed
        failures=$(get_state_field "$state" "indexer_failures" 0)
        restarts=$(get_state_field "$state" "indexer_restarts" 0)
        last_restart=$(get_state_field "$state" "indexer_last_restart" 0)
        failures=$((failures + 1))

        state=$(echo "$state" | python3 -c "
import sys, json
d = json.load(sys.stdin)
d['indexer_failures'] = ${failures}
print(json.dumps(d))
" 2>/dev/null || echo "$state")

        log "WARN" "Indexer health check FAILED (failure #${failures})"

        # Check if we're in a cooldown period
        local now
        now=$(date +%s)
        if [[ "$last_restart" -gt 0 ]]; then
            elapsed=$((now - last_restart))
        else
            elapsed=999
        fi

        # Try auto-restart if within limits
        if [[ "$restarts" -lt "$MAX_AUTO_RESTARTS" && "$elapsed" -ge "$RESTART_COOLDOWN" ]]; then
            restarts=$((restarts + 1))
            state=$(echo "$state" | python3 -c "
import sys, json
d = json.load(sys.stdin)
d['indexer_restarts'] = ${restarts}
d['indexer_last_restart'] = $(date +%s)
print(json.dumps(d))
" 2>/dev/null || echo "$state")

            notify "opencode: Indexer Down" \
                "Indexer is unreachable (failure #${failures}). Auto-restarting (attempt ${restarts}/${MAX_AUTO_RESTARTS})..." \
                "critical"

            local report_dir
            report_dir=$(capture_crash_evidence "indexer")

            if restart_indexer; then
                log "INFO" "Indexer auto-restart succeeded"
                state=$(echo "$state" | python3 -c "
import sys, json
d = json.load(sys.stdin)
d['indexer_failures'] = 0
d['indexer_restarts'] = 0
print(json.dumps(d))
" 2>/dev/null || echo "$state")
            else
                log "ERROR" "Indexer auto-restart FAILED"
                notify "opencode: Indexer Restart Failed" \
                    "Auto-restart attempt ${restarts} failed. Crash report: ${report_dir}/crash-report.md" \
                    "critical"
            fi

        elif [[ "$failures" -ge "$FATAL_NOTIFY_THRESHOLD" ]]; then
            notify "opencode: Indexer FATAL" \
                "Indexer down for ${failures} consecutive checks (${restarts} restart attempts). Manual investigation needed.\nLog: ${INDEXER_LOG}\nRun: AI inspect ${CRASH_DIR}" \
                "critical"
        fi
    fi

    # ── Check Embedder ─────────────────────────────────────────────────
    if check_embedder_health; then
        embedder_ok=true
        state=$(echo "$state" | python3 -c "
import sys, json
d = json.load(sys.stdin)
d['embedder_failures'] = 0
d['embedder_restarts'] = 0
d['embedder_last_ok'] = '$(date -u +%Y-%m-%dT%H:%M:%SZ)'
print(json.dumps(d))
" 2>/dev/null || echo "$state")
    else
        embedder_ok=false
        local efailures erestarts elast_restart
        efailures=$(get_state_field "$state" "embedder_failures" 0)
        erestarts=$(get_state_field "$state" "embedder_restarts" 0)
        elast_restart=$(get_state_field "$state" "embedder_last_restart" 0)
        efailures=$((efailures + 1))

        state=$(echo "$state" | python3 -c "
import sys, json
d = json.load(sys.stdin)
d['embedder_failures'] = ${efailures}
print(json.dumps(d))
" 2>/dev/null || echo "$state")

        log "WARN" "Embedder health check FAILED (failure #${efailures})"

        local now eelapsed
        now=$(date +%s)
        if [[ "$elast_restart" -gt 0 ]]; then
            eelapsed=$((now - elast_restart))
        else
            eelapsed=999
        fi

        if [[ "$erestarts" -lt "$MAX_AUTO_RESTARTS" && "$eelapsed" -ge "$RESTART_COOLDOWN" ]]; then
            erestarts=$((erestarts + 1))
            state=$(echo "$state" | python3 -c "
import sys, json
d = json.load(sys.stdin)
d['embedder_restarts'] = ${erestarts}
d['embedder_last_restart'] = $(date +%s)
print(json.dumps(d))
" 2>/dev/null || echo "$state")

            notify "opencode: Embedder Down" \
                "Embedder is unreachable (failure #${efailures}). Auto-restarting (attempt ${erestarts}/${MAX_AUTO_RESTARTS})..." \
                "critical"

            local ereport_dir
            ereport_dir=$(capture_crash_evidence "embedder")

            if restart_embedder; then
                log "INFO" "Embedder auto-restart succeeded"
                state=$(echo "$state" | python3 -c "
import sys, json
d = json.load(sys.stdin)
d['embedder_failures'] = 0
d['embedder_restarts'] = 0
print(json.dumps(d))
" 2>/dev/null || echo "$state")
            else
                log "ERROR" "Embedder auto-restart FAILED"
                notify "opencode: Embedder Restart Failed" \
                    "Auto-restart attempt ${erestarts} failed. Crash report: ${ereport_dir}/crash-report.md" \
                    "critical"
            fi

        elif [[ "$efailures" -ge "$FATAL_NOTIFY_THRESHOLD" ]]; then
            notify "opencode: Embedder FATAL" \
                "Embedder down for ${efailures} consecutive checks (${erestarts} restart attempts). Manual investigation needed.\nLog: ${EMBEDDER_LOG}\nRun: AI inspect ${CRASH_DIR}" \
                "critical"
        fi
    fi

    write_state "$state"

    # Log summary
    local idx_status="${indexer_ok:-unknown}"
    local emb_status="${embedder_ok:-unknown}"
    log "INFO" "Health check: indexer=${idx_status} embedder=${emb_status}"
}

# ── Entry Point ────────────────────────────────────────────────────────────

ONEShot=false
INTERVAL="${OPENCODE_HEALTH_INTERVAL:-30}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --oneshot) ONEShot=true; shift ;;
        --interval) INTERVAL="$2"; shift 2 ;;
        *) log "ERROR" "Unknown arg: $1"; exit 1 ;;
    esac
done

if [[ "$ONEShot" == "true" ]]; then
    run_health_check
    exit 0
fi

log "INFO" "Health supervisor started (interval=${INTERVAL}s)"
trap 'log "INFO" "Health supervisor stopped"; exit 0' INT TERM

while true; do
    run_health_check
    sleep "$INTERVAL"
done
