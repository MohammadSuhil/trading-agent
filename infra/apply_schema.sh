#!/bin/zsh
# Apply schema.sql statement-by-statement so a failure names the statement.
CH="http://127.0.0.1:8123/"
ROOT="$(cd -- "$(dirname -- "$0")/.." && pwd)"
F=${1:-"$ROOT/infra/clickhouse/schema.sql"}
ok=0; fail=0
# split on ';' at end of line, keeping multi-line statements intact
if ! statements=$(python3 - "$F" <<'PY'
import sys, re, base64
sql = open(sys.argv[1]).read()
sql = re.sub(r'--[^\n]*', '', sql)
for s in sql.split(';'):
    s = s.strip()
    if s:
        print(base64.b64encode(s.encode()).decode())
PY
); then
  echo "FAIL  could not read or parse schema file: $F" >&2
  exit 1
fi
while IFS= read -r stmt; do
  decoded=$(printf '%s' "$stmt" | base64 -d)
  name=$(printf '%s' "$decoded" | head -1 | cut -c1-60)
  if ! response=$(curl --fail --silent --show-error --data-binary "$decoded" "$CH"); then
    echo "  FAIL  $name (request failed)"
    fail=$((fail+1))
  elif [[ "$response" == *Exception* ]]; then
    echo "  FAIL  $name"
    printf '%s\n' "$response" | head -2
    fail=$((fail+1))
  else
    echo "  ok    $name"
    ok=$((ok+1))
  fi
done <<< "$statements"
echo "Schema statements: $ok ok, $fail failed"
[[ "$fail" == 0 ]]
