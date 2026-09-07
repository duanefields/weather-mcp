#!/usr/bin/env bash
#
# Poll a running weather-mcp server and report to a dead-man's-switch.
#
# Intended for cron:
#   */10 * * * * /Users/you/Code/weather-mcp/scripts/healthcheck.sh >> /Users/you/.weather-mcp/check.log 2>&1
#
# Configuration comes from ~/.weather-mcp/check.env, if it exists:
#
#   HEALTH_URL=http://127.0.0.1:18791/health
#   PING_URL=https://hc-ping.com/your-uuid-here
#
# chmod 600 that file: the ping URL is a capability, not just an address. Use a
# different healthchecks.io UUID from every other service on the host, or one
# service's silence gets masked by another's pings.
#
# Deliberately not `set -e`: the point is to collect every problem and still
# report, rather than dying on the first one and pinging nothing.
set -uo pipefail

CONFIG="${WEATHER_MCP_CHECK_ENV:-$HOME/.weather-mcp/check.env}"
# shellcheck disable=SC1090
[[ -f "$CONFIG" ]] && source "$CONFIG"

HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:18791/health}"
PING_URL="${PING_URL:-}"

stamp() { date '+%Y-%m-%d %H:%M:%S'; }
log() { echo "[$(stamp)] $*"; }

ping_hc() {
  [[ -z "$PING_URL" ]] && return 0
  local suffix="$1" body="${2:-}"
  curl -fsS -m 10 --data-raw "$body" "${PING_URL}${suffix}" >/dev/null 2>&1 || true
}

ping_hc "/start"

problems=()

# A timeout is meaningful, not just slow: the server could be wedged mid-request
# against Open-Meteo, in which case it answers nothing rather than answering badly.
body="$(curl -fsS -m 15 "$HEALTH_URL" 2>/dev/null)"

if [[ -z "$body" ]]; then
  problems+=("no response from $HEALTH_URL (down, or wedged)")
else
  # Parsed with plutil, which ships with macOS. Deliberately not /usr/bin/python3:
  # that is a Command Line Tools shim, and an OS update can leave it prompting to
  # install developer tools -- which would break this check exactly when an OS
  # update is the thing most likely to have broken something.
  extract() {
    printf '%s' "$body" | plutil -extract "$1" raw -o - - 2>/dev/null
  }

  status="$(extract status)"
  reachable="$(extract openmeteo_reachable)"
  home="$(extract home_configured)"
  err="$(extract error)"
  python_version="$(extract python_version)"

  if [[ "$status" != "ok" ]]; then
    problems+=("health reports status=$status")
  fi

  # There is no credential to be revoked here, so the failure modes are narrower
  # than a server with an account behind it: upstream is down, the free tier is
  # exhausted, or the host lost DNS.
  if [[ "$reachable" != "true" ]]; then
    problems+=("Open-Meteo unreachable (${err:-no detail})")
  fi

  # Not fatal -- every tool still works when a place is named explicitly -- but
  # it silently breaks every question about "here", which is most of them.
  if [[ "$home" != "true" ]]; then
    problems+=("no home location configured; WEATHER_MCP_HOME is unset")
  fi
fi

if (( ${#problems[@]} > 0 )); then
  message="weather-mcp check FAILED"
  for problem in "${problems[@]}"; do
    message+=$'\n'"- $problem"
  done
  log "$message"
  ping_hc "/fail" "$message"
  exit 1
fi

# Logged on every run, not just on failure, so the log doubles as a record of
# how the service has actually been behaving.
log "weather-mcp OK status=$status home=$home python=$python_version"
ping_hc ""
