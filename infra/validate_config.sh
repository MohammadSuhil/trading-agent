#!/bin/zsh
# XML comments may not contain '--'. ClickHouse fails SILENTLY on a malformed
# config: it logs nothing useful and exits during shutdown. Always validate.
ROOT="$(cd -- "$(dirname -- "$0")/.." && pwd)"
rc=0
for f in "$ROOT"/infra/clickhouse/*.xml; do
  if python3 -c "import xml.etree.ElementTree as ET,sys; ET.parse(sys.argv[1])" "$f" 2>/dev/null; then
    echo "OK   $(basename $f)"
  else
    echo "FAIL $(basename $f)"; python3 -c "import xml.etree.ElementTree as ET,sys; ET.parse(sys.argv[1])" "$f" 2>&1 | tail -1
    rc=1
  fi
done
exit $rc
