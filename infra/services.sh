#!/bin/zsh
# Start/stop the Trading Agent datastores for THIS session (no login persistence).
#   infra/services.sh up | down | status
export PATH="/opt/homebrew/bin:$PATH"
ROOT="$(cd -- "$(dirname -- "$0")/.." && pwd)"
case "${1:-status}" in
up)
  mkdir -p "$ROOT/data/logs"
  "$ROOT/infra/validate_config.sh" >/dev/null || { echo "ABORT: malformed ClickHouse XML"; "$ROOT/infra/validate_config.sh"; exit 1; }
  if [[ ! -x "$ROOT/infra/bin/clickhouse" ]]; then
    echo "ClickHouse binary missing. Run scripts/setup_macos.sh first." >&2
    exit 1
  fi
  if ! curl -s --max-time 2 http://127.0.0.1:8123/ping >/dev/null 2>&1; then
    (cd "$ROOT" && nohup "$ROOT/infra/bin/clickhouse" server --config-file="$ROOT/infra/clickhouse/config.xml" \
        >> "$ROOT/data/logs/clickhouse.out.log" 2>&1 &) ; echo "clickhouse starting"
  else echo "clickhouse already up"; fi
  if ! mongosh --quiet --eval 'db.runCommand({ping:1}).ok' >/dev/null 2>&1; then
    (cd "$ROOT" && nohup mongod --config "$ROOT/infra/mongo/mongod.conf" >> "$ROOT/data/logs/mongod.out.log" 2>&1 &) ; echo "mongod starting"
  else echo "mongod already up"; fi
  ;;
down)
  pkill -f "$ROOT/infra/bin/clickhouse server" 2>/dev/null && echo "clickhouse stopping"
  pkill -f "mongod --config $ROOT" 2>/dev/null && echo "mongod stopping"
  # ClickHouse holds 9000 briefly after SIGTERM; restarting too soon fails with
  # "Address already in use" and the new process exits silently.
  for i in $(seq 1 30); do
    lsof -nP -iTCP:9000 -sTCP:LISTEN >/dev/null 2>&1 || break
    sleep 1
  done
  echo "ports released"
  ;;
status)
  printf "clickhouse : %s\n" "$(curl -s --max-time 2 http://127.0.0.1:8123/ping 2>/dev/null || echo DOWN)"
  printf "mongod     : %s\n" "$(mongosh --quiet --eval 'db.runCommand({ping:1}).ok' 2>/dev/null | tail -1 || echo DOWN)"
  printf "redis      : %s\n" "$(redis-cli ping 2>/dev/null || echo DOWN)"
  ;;
esac
