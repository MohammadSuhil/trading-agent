#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This runner supports macOS only. See docs/technical/OPERATIONS.md." >&2
  exit 2
fi

usage() {
  cat <<'EOF'
Usage: scripts/run.sh <command> [arguments]

Commands:
  start       Start local databases and serve the dashboard/API on port 8000
  workflow    Run `ta daily --days 7`; extra arguments are passed to the CLI
  status      Show service health and database status
  build       Build the dashboard bundle
  ui          Start the Vite dashboard on port 5173

Examples:
  scripts/run.sh start
  scripts/run.sh workflow
  scripts/run.sh workflow --end 2026-11-05 --days 7
  scripts/run.sh ui

Stop the foreground API with Ctrl-C. Stop the local datastores with
`infra/services.sh down` when they are no longer needed.
EOF
}

wait_for_clickhouse() {
  for _ in {1..60}; do
    if curl --fail --silent --max-time 2 http://127.0.0.1:8123/ping >/dev/null; then
      return 0
    fi
    sleep 1
  done
  echo "ClickHouse is not responding at 127.0.0.1:8123. Run scripts/setup_macos.sh first." >&2
  return 1
}

wait_for_mongo() {
  for _ in {1..60}; do
    if mongosh --quiet --eval 'db.runCommand({ping:1}).ok' 2>/dev/null | grep -qx 1; then
      return 0
    fi
    sleep 1
  done
  echo "MongoDB is not responding at 127.0.0.1:27017. Run scripts/setup_macos.sh first." >&2
  return 1
}

ensure_redis() {
  if ! redis-cli ping 2>/dev/null | grep -qx PONG; then
    if ! command -v brew >/dev/null 2>&1; then
      echo "Redis is not responding and Homebrew is unavailable to start it." >&2
      return 1
    fi
    brew services start redis
  fi
}

command_name="${1:-help}"
if [[ "$#" -gt 0 ]]; then shift; fi

case "$command_name" in
  start)
    "$ROOT/infra/services.sh" up
    wait_for_clickhouse
    wait_for_mongo
    ensure_redis
    "$ROOT/infra/apply_schema.sh" >/dev/null
    "$ROOT/infra/apply_schema.sh" "$ROOT/infra/clickhouse/schema_v2.sql" >/dev/null
    if [[ ! -f "$ROOT/apps/ui/dist/index.html" ]]; then
      npm run build --prefix "$ROOT/apps/ui"
    fi
    cd "$ROOT"
    exec uv run --frozen python -m ta_api.serve
    ;;
  workflow|daily)
    "$ROOT/infra/services.sh" up
    wait_for_clickhouse
    "$ROOT/infra/apply_schema.sh" >/dev/null
    "$ROOT/infra/apply_schema.sh" "$ROOT/infra/clickhouse/schema_v2.sql" >/dev/null
    if [[ "$#" == 0 ]]; then set -- --days 7; fi
    cd "$ROOT"
    exec uv run --frozen ta daily "$@"
    ;;
  status)
    "$ROOT/infra/services.sh" status
    cd "$ROOT"
    uv run --frozen ta status
    ;;
  build)
    npm run build --prefix "$ROOT/apps/ui"
    ;;
  ui)
    npm run dev --prefix "$ROOT/apps/ui"
    ;;
  help|-h|--help)
    usage
    ;;
  *)
    echo "Unknown command: $command_name" >&2
    usage >&2
    exit 2
    ;;
esac