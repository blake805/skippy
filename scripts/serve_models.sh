#!/usr/bin/env bash
#
# Start/stop the local MLX inference fleet by role.
#
#   ./scripts/serve_models.sh all              # fast + compressor + heavy
#   ./scripts/serve_models.sh start fast
#   ./scripts/serve_models.sh solo heavy       # unload everything else first
#   ./scripts/serve_models.sh status
#   ./scripts/serve_models.sh stop all
#
# Role -> URL/model mapping comes from skippy_llm.py, so this script and the
# server always agree on which weights back which port.
#
# Memory rule: only one heavy model may be resident. `solo heavy` is the escape
# hatch for long-context GLM sessions that need the whole machine.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_DIR="${SKIPPY_RUN_DIR:-/tmp/skippy_models}"
LOG_DIR="$RUN_DIR/logs"
ROLES=(fast compressor heavy)
HEAVY_ROLES=(heavy)

mkdir -p "$RUN_DIR" "$LOG_DIR"

die() { echo "error: $*" >&2; exit 1; }

# Prints "<host> <port> <model>" for a role, straight out of the Python registry.
role_config() {
  local role="$1"
  PYTHONPATH="$REPO_ROOT" python3 - "$role" <<'PY'
import sys
from urllib.parse import urlparse

import skippy_llm

role = sys.argv[1]
try:
    endpoint = skippy_llm.endpoint(role)
except skippy_llm.ModelError as exc:
    sys.exit(str(exc))
parsed = urlparse(endpoint.url)
print(parsed.hostname or "127.0.0.1", parsed.port or 80, endpoint.model)
PY
}

pid_file() { echo "$RUN_DIR/$1.pid"; }

is_running() {
  local file
  file="$(pid_file "$1")"
  [[ -f "$file" ]] || return 1
  local pid
  pid="$(cat "$file")"
  [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

heavy_running() {
  local role
  for role in "${HEAVY_ROLES[@]}"; do
    if is_running "$role"; then
      echo "$role"
      return 0
    fi
  done
  return 1
}

start_role() {
  local role="$1"

  if is_running "$role"; then
    echo "[$role] already running (pid $(cat "$(pid_file "$role")"))"
    return 0
  fi

  local host port model
  read -r host port model <<<"$(role_config "$role")"

  local resident
  if [[ " ${HEAVY_ROLES[*]} " == *" $role "* ]] && resident="$(heavy_running)"; then
    die "'$resident' is already resident; only one heavy model at a time. Run 'stop $resident' first."
  fi

  echo "[$role] starting $model on $host:$port"
  local log="$LOG_DIR/$role.log"
  nohup python3 -m mlx_lm.server \
    --model "$model" \
    --host "$host" \
    --port "$port" \
    >>"$log" 2>&1 &
  echo $! >"$(pid_file "$role")"
  echo "[$role] pid $(cat "$(pid_file "$role")"), logging to $log"
}

stop_role() {
  local role="$1"
  if ! is_running "$role"; then
    rm -f "$(pid_file "$role")"
    echo "[$role] not running"
    return 0
  fi
  local pid
  pid="$(cat "$(pid_file "$role")")"
  echo "[$role] stopping pid $pid"
  kill "$pid" 2>/dev/null || true
  for _ in $(seq 1 30); do
    kill -0 "$pid" 2>/dev/null || break
    sleep 1
  done
  kill -9 "$pid" 2>/dev/null || true
  rm -f "$(pid_file "$role")"
}

status() {
  local role host port model
  printf '%-12s %-9s %-22s %s\n' ROLE STATE ENDPOINT MODEL
  for role in "${ROLES[@]}"; do
    read -r host port model <<<"$(role_config "$role")"
    if is_running "$role"; then
      printf '%-12s %-9s %-22s %s\n' "$role" "up" "$host:$port" "$model"
    else
      printf '%-12s %-9s %-22s %s\n' "$role" "down" "$host:$port" "$model"
    fi
  done
}

valid_role() {
  local role="$1" known
  for known in "${ROLES[@]}"; do
    [[ "$known" == "$role" ]] && return 0
  done
  return 1
}

command="${1:-status}"
target="${2:-}"

case "$command" in
  start)
    [[ -n "$target" ]] || die "usage: $0 start <${ROLES[*]}>"
    valid_role "$target" || die "unknown role '$target'"
    start_role "$target"
    ;;
  stop)
    if [[ "$target" == "all" || -z "$target" ]]; then
      for role in "${ROLES[@]}"; do stop_role "$role"; done
    else
      valid_role "$target" || die "unknown role '$target'"
      stop_role "$target"
    fi
    ;;
  restart)
    [[ -n "$target" ]] || die "usage: $0 restart <${ROLES[*]}>"
    stop_role "$target"
    start_role "$target"
    ;;
  all)
    start_role fast
    start_role compressor
    start_role heavy
    ;;
  solo)
    [[ -n "$target" ]] || die "usage: $0 solo <${ROLES[*]}>"
    valid_role "$target" || die "unknown role '$target'"
    for role in "${ROLES[@]}"; do
      [[ "$role" == "$target" ]] || stop_role "$role"
    done
    start_role "$target"
    ;;
  status)
    status
    ;;
  *)
    die "usage: $0 {start|stop|restart|solo} <role> | all | status"
    ;;
esac
