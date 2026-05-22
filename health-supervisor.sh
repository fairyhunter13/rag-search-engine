#!/bin/bash
# =============================================================================
# opencode search engine health supervisor v4
# =============================================================================
# Monitor-only supervisor for the unified opencode-search Python package.
#
# The opencode-search package runs as:
#   • An MCP stdio child of an AI assistant (owned by the parent, no supervision
#     needed)
#   • A long-running `opencode-search watch <path>` daemon — THIS is what we
#     monitor here.
#
# This supervisor captures crash evidence on fatal failures and sends desktop
# notifications. It does NOT auto-restart any service.
# =============================================================================

set -euo pipefail

OPENDIR="${HOME}/.opencode"
LOG_DIR="${OPENDIR}/health"
mkdir -p "$LOG_DIR"

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

check_opencode_search_watcher() {
    # Detect any long-running `opencode-search watch` daemons.
    # Returns 0 if at least one is running, 1 otherwise.
    pgrep -f "opencode-search.*watch" >/dev/null 2>&1
}

# ── Crash Evidence ─────────────────────────────────────────────────────────

capture_crash_evidence() {
    local service="$1"
    local ts dir
    ts=$(date -u +%Y%m%d-%H%M%S)
    dir="${CRASH_DIR}/${service}-${ts}"
    mkdir -p "$dir"

    log "INFO" "Capturing crash evidence for ${service} → ${dir}"

    local log_file="${OPENDIR}/${service}.log"
    {
        echo "# Crash: ${service}  $(date -u)"
        echo ""
        echo "## System State"
        echo '```'
        free -h 2>/dev/null || true
        nvidia-smi 2>/dev/null || echo "nvidia-smi not available"
        echo '```'
        echo ""
        echo "## Service Log (last 50 lines)"
        echo '```'
        tail -50 "$log_file" 2>/dev/null || true
        echo '```'
        echo ""
        echo "## Errors"
        echo '```'
        grep -i 'error\|fatal\|traceback\|OOM\|killed\|signal\|GPU\|CUDA' "$log_file" 2>/dev/null | tail -20 || true
        echo '```'
    } > "${dir}/crash-report.md"

    cp "$log_file" "${dir}/${service}.log" 2>/dev/null || true
    cp "${log_file}.1" "${dir}/${service}.log.1" 2>/dev/null || true

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
    local state watcher_ok
    state=$(read_state)

    # opencode-search watch daemon
    watcher_ok=false
    check_opencode_search_watcher && watcher_ok=true

    local watcher_failures
    watcher_failures=$(get_field "$state" "watcher_failures" 0)
    $watcher_ok && watcher_failures=0 || watcher_failures=$((watcher_failures + 1))

    if [[ "$watcher_failures" -ge "$FATAL_NOTIFY_THRESHOLD" ]] && [[ "$watcher_failures" -eq "$FATAL_NOTIFY_THRESHOLD" ]]; then
        notify "opencode-search: Watcher Down" \
            "No opencode-search watch daemon detected for ${watcher_failures} checks." \
            "normal"
        capture_crash_evidence "opencode-search"
    fi

    # Persist
    state=$(echo "$state" | python3 -c "
import sys, json
d = json.load(sys.stdin)
d['watcher_failures'] = ${watcher_failures}
print(json.dumps(d))
" 2>/dev/null || echo "$state")
    write_state "$state"

    log "INFO" "Health: watcher=${watcher_ok}"
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

log "INFO" "Health supervisor v4 started (monitor-only, interval=${INTERVAL}s)"
trap 'log "INFO" "Stopped"; exit 0' INT TERM

while true; do
    run_health_check
    sleep "$INTERVAL"
done
