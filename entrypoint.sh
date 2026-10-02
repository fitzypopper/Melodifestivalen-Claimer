#!/bin/bash
# entrypoint.sh — runs web UI + claimer hourly with token refresh

set -euo pipefail

LOG_LEVEL="${LOG_LEVEL:-INFO}"
TOKEN_FILE="${TOKEN_FILE:-/data/token.json}"
ALBUMS="${ALBUMS:-1..25}"
DELAY="${DELAY:-0.5}"
EMAIL="${EMAIL:-}"
UI_LOG="${UI_LOG:-${LOG_FILE:-/data/claimer.log}}"

log() {
    local level="$1"; shift
    local ts; ts=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
    local line="[$ts] [$level] $*"
    echo "$line"
    if [[ -n "${UI_LOG:-}" ]]; then
        if [[ -f "$UI_LOG" ]] && (( $(stat -c%s "$UI_LOG" 2>/dev/null || echo 0) > 1048576 )); then
            tail -n 500 "$UI_LOG" > "$UI_LOG.tmp" && mv "$UI_LOG.tmp" "$UI_LOG" || true
        fi
        echo "$line" >> "$UI_LOG" || true
    fi
    return 0
}

# expand ALBUMS range (e.g. "1..25" or "1,2,3,5..10")
expand_albums() {
    local spec="$1"
    if [[ "$spec" == *..* ]]; then
        local start=${spec%%..*}
        local end=${spec##*..}
        seq "$start" "$end"
    else
        echo "$spec" | tr ',' '\n'
    fi
}

# refresh access token using refresh_token (pure Python, no curl/jq needed)
refresh_token() {
    if [[ ! -f "$TOKEN_FILE" ]]; then
        log WARN "no token file at $TOKEN_FILE"
        return 1
    fi
    log INFO "refreshing access token..."
    python3 -c "
import json, sys, urllib.request
with open('$TOKEN_FILE') as f:
    tok = json.load(f)
refresh = tok.get('refreshToken')
device = tok.get('deviceId')
if not refresh:
    sys.exit(1)
req = urllib.request.Request(
    'https://auth.prod.uno.svt.se/authentication/v5/token/refresh',
    data=json.dumps({'refreshToken': refresh}).encode(),
    headers={
        'x-device-id': device,
        'x-uno-client': 'melodifestivalen-android',
        'User-Agent': 'Melodifestivalen/android/10.4.0/60385',
        'Content-Type': 'application/json',
    },
    method='POST'
)
try:
    with urllib.request.urlopen(req, timeout=20) as r:
        resp = json.load(r)
except Exception:
    sys.exit(1)
if 'accessToken' in resp:
    tok['accessToken'] = resp['accessToken']
    if 'refreshToken' in resp:
        tok['refreshToken'] = resp['refreshToken']
    tok['savedAt'] = int(__import__('time').time())
    with open('$TOKEN_FILE', 'w') as f:
        json.dump(tok, f, indent=2)
    sys.exit(0)
sys.exit(1)
" && log INFO "token refreshed successfully" || log ERROR "token refresh failed"
}

# run a single claim cycle
run_claim_cycle() {
    log INFO "starting claim cycle (albums: $ALBUMS)"

    # ensure token is valid (exp > now + 5min)
    if [[ -f "$TOKEN_FILE" ]]; then
        local exp
        exp=$(python3 -c "
import json, sys, base64
try:
    tok = json.load(open('$TOKEN_FILE'))
    p = tok['accessToken'].split('.')[1]
    p += '=' * (-len(p) % 4)
    print(json.loads(base64.urlsafe_b64decode(p))['exp'])
except Exception:
    print(0)
" 2>/dev/null || echo 0)
        local now; now=$(date +%s)
        if (( exp - now < 7200 )); then
            log INFO "access token expires within 2h (exp=$exp, now=$now), refreshing..."
            refresh_token || log WARN "refresh failed, will try with existing token"
        fi
    fi

    # run claim for each album
    local total=0
    local failed=0
    for alb in $(expand_albums "$ALBUMS"); do
        log INFO "claiming album $alb..."
        if python3 /app/claim_daily.py --token-file "$TOKEN_FILE" --album "$alb" --delay "$DELAY" 2>&1 | while IFS= read -r line; do
            log INFO "album $alb: $line"
        done; then
            ((total++)) || true
        else
            local code=$?
            if (( code == 2 )); then
                log INFO "album $alb: window already claimed - skipping"
            else
                log ERROR "album $alb: claim failed with exit code $code"
                ((failed++)) || true
            fi
        fi
        sleep "$DELAY"
    done

    log INFO "cycle complete: $total albums claimed, $failed failed"
}

# start web UI in background
log INFO "starting web UI on port 8765..."
python3 /app/webui.py &
WEBUI_PID=$!

# wait for token file or EMAIL env var before starting claimer
log INFO "melo-mitm claimer starting (interval: 1h, albums: $ALBUMS)"

# if no token and no EMAIL, wait for web UI to create token
while [[ ! -f "$TOKEN_FILE" && -z "$EMAIL" ]]; do
    log INFO "no token found, waiting for login via web UI (or set EMAIL env var)..."
    sleep 10
done

# if EMAIL set and no token, do initial login
if [[ -n "$EMAIL" && ! -f "$TOKEN_FILE" ]]; then
    log INFO "no token file, running initial login for $EMAIL"
    python3 /app/claim_daily.py --email "$EMAIL" --login --token-file "$TOKEN_FILE" 2>&1 | while IFS= read -r line; do
        log INFO "login: $line"
    done
fi

while true; do
    run_claim_cycle
    log INFO "sleeping 1 hour until next window..."
    sleep 3600
done