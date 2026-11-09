#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
UPGRADE=0

usage() {
  cat <<'EOF'
Usage: scripts/setup_macos.sh [--upgrade]

Install missing macOS prerequisites, set up the local database schema, and
install Python and UI dependencies. Existing Homebrew packages are not
upgraded unless --upgrade is supplied.
EOF
}

for arg in "$@"; do
  case "$arg" in
    --upgrade) UPGRADE=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $arg" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This setup script supports macOS only. See docs/technical/OPERATIONS.md." >&2
  exit 2
fi

if command -v brew >/dev/null 2>&1; then
  BREW="$(command -v brew)"
elif [[ -x /opt/homebrew/bin/brew ]]; then
  BREW=/opt/homebrew/bin/brew
elif [[ -x /usr/local/bin/brew ]]; then
  BREW=/usr/local/bin/brew
else
  echo "Homebrew is required. Install it from https://brew.sh/, then rerun this script." >&2
  exit 1
fi
eval "$("$BREW" shellenv)"

if [[ "$UPGRADE" == 1 ]]; then
  "$BREW" update
fi

ensure_formula() {
  local formula="$1"
  if ! "$BREW" list --versions "$formula" >/dev/null 2>&1; then
    echo "Installing $formula"
    "$BREW" install "$formula"
  elif [[ "$UPGRADE" == 1 ]]; then
    local outdated
    outdated="$("$BREW" outdated --quiet "$formula" || true)"
    if [[ -n "$outdated" ]]; then
      echo "Upgrading $formula"
      "$BREW" upgrade "$formula"
    else
      echo "$formula is current"
    fi
  else
    echo "$formula is installed (use --upgrade to update Homebrew packages)"
  fi
}

ensure_formula uv
ensure_formula node
ensure_formula redis
if ! "$BREW" tap | grep -Fxq mongodb/brew; then
  "$BREW" tap mongodb/brew
fi
if command -v mongod >/dev/null 2>&1; then
  mongo_major="$(mongod --version | sed -n 's/^db version v\([0-9][0-9]*\).*/\1/p' | head -1)"
  if [[ -z "$mongo_major" || "$mongo_major" -lt 8 ]]; then
    echo "Found an unsupported MongoDB version; refusing to replace its data files." >&2
    echo "Back up the database and upgrade MongoDB deliberately, then rerun setup." >&2
    exit 1
  fi
  echo "Using installed MongoDB $(mongod --version | sed -n 's/^db version //p' | head -1)"
else
  ensure_formula mongodb/brew/mongodb-community@8.0
fi
if ! command -v mongosh >/dev/null 2>&1; then
  ensure_formula mongodb/brew/mongosh
fi

uv python install 3.13
uv sync --frozen --directory "$ROOT"
npm ci --prefix "$ROOT/apps/ui"

CLICKHOUSE_VERSION=26.10.2.103
CLICKHOUSE_BIN="$ROOT/infra/bin/clickhouse"
CLICKHOUSE_CURRENT=""
if [[ -x "$CLICKHOUSE_BIN" ]]; then
  CLICKHOUSE_CURRENT="$("$CLICKHOUSE_BIN" --version 2>/dev/null || true)"
fi

if [[ "$CLICKHOUSE_CURRENT" != *"$CLICKHOUSE_VERSION"* ]]; then
  if [[ -n "$CLICKHOUSE_CURRENT" && "$UPGRADE" != 1 ]]; then
    echo "Found $CLICKHOUSE_CURRENT; expected ClickHouse $CLICKHOUSE_VERSION." >&2
    echo "Rerun with --upgrade to replace the project-local binary." >&2
    exit 1
  fi

  case "$(uname -m)" in
    arm64|aarch64)
      ASSET=clickhouse-macos-aarch64
      EXPECTED_SHA256=7f43a3c4d5f41e42ae6ec275c1531ca44b6018caae15217394fd85181ca1df59
      ;;
    x86_64)
      ASSET=clickhouse-macos
      EXPECTED_SHA256=8bdb041c299a849ebd9bbba72ffca925a24482abec690ae698be63818eece1ab
      ;;
    *) echo "Unsupported macOS architecture: $(uname -m)" >&2; exit 2 ;;
  esac

  mkdir -p "$(dirname "$CLICKHOUSE_BIN")"
  tmp="$(mktemp "${TMPDIR:-/tmp}/clickhouse.XXXXXX")"
  trap 'rm -f "$tmp"' EXIT
  url="https://github.com/ClickHouse/ClickHouse/releases/download/v${CLICKHOUSE_VERSION}-stable/${ASSET}"
  echo "Downloading official ClickHouse ${CLICKHOUSE_VERSION} for $(uname -m)"
  curl --fail --location --retry 3 "$url" --output "$tmp"
  actual_sha256="$(shasum -a 256 "$tmp" | awk '{print $1}')"
  if [[ "$actual_sha256" != "$EXPECTED_SHA256" ]]; then
    echo "ClickHouse checksum mismatch; refusing to install the binary." >&2
    exit 1
  fi
  chmod 755 "$tmp"
  mv "$tmp" "$CLICKHOUSE_BIN"
  trap - EXIT
else
  echo "ClickHouse $CLICKHOUSE_VERSION is installed"
fi

mkdir -p "$ROOT/data/logs" "$ROOT/data/clickhouse" "$ROOT/data/mongo"
"$ROOT/infra/validate_config.sh"

if ! redis-cli ping 2>/dev/null | grep -qx PONG; then
  "$BREW" services start redis
fi

"$ROOT/infra/services.sh" up

ready=0
for _ in {1..60}; do
  if curl --fail --silent --max-time 2 http://127.0.0.1:8123/ping >/dev/null; then
    ready=1
    break
  fi
  sleep 1
done
if [[ "$ready" != 1 ]]; then
  echo "ClickHouse did not become ready; inspect data/logs/clickhouse*.log." >&2
  exit 1
fi

ready=0
for _ in {1..60}; do
  if mongosh --quiet --eval 'db.runCommand({ping:1}).ok' 2>/dev/null | grep -qx 1; then
    ready=1
    break
  fi
  sleep 1
done
if [[ "$ready" != 1 ]]; then
  echo "MongoDB did not become ready; inspect data/logs/mongod*.log." >&2
  exit 1
fi

replica_set="$(mongosh --quiet --eval 'db.hello().setName' 2>/dev/null || true)"
if [[ "$replica_set" != rs0 ]]; then
  mongosh --quiet --eval 'rs.initiate({_id:"rs0",members:[{_id:0,host:"127.0.0.1:27017"}]})'
fi

"$ROOT/infra/apply_schema.sh"
"$ROOT/infra/apply_schema.sh" "$ROOT/infra/clickhouse/schema_v2.sql"
npm run build --prefix "$ROOT/apps/ui"

cat <<EOF

Setup complete.
Start the app:      $ROOT/scripts/run.sh start
Run the workflow:   $ROOT/scripts/run.sh workflow
Check services:     $ROOT/scripts/run.sh status
EOF