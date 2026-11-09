#!/bin/zsh
# M0 smoke test: all three datastores accept writes, honour their memory caps,
# and the machine is not swapping. Exits non-zero on any failure.
export PATH="/opt/homebrew/bin:$PATH"
CH="http://127.0.0.1:8123/"
q()  { curl -s --data-binary "$1" "$CH"; }          # POST = read/write
fail=0
chk() { if [ "$2" = "$3" ]; then echo "  PASS  $1"; else echo "  FAIL  $1 (got '$2' want '$3')"; fail=1; fi }

echo "== ClickHouse =="
chk "ping"            "$(curl -s --max-time 3 ${CH}ping)" "Ok."
chk "memory cap 5GiB" "$(q "SELECT value FROM system.server_settings WHERE name='max_server_memory_usage'" | tr -d '\n')" "5368709120"
q "CREATE DATABASE IF NOT EXISTS smoke" >/dev/null
q "DROP TABLE IF EXISTS smoke.t" >/dev/null
q "CREATE TABLE smoke.t (d Date, sym LowCardinality(String), close Float64) ENGINE=MergeTree ORDER BY (sym,d)" >/dev/null
q "INSERT INTO smoke.t VALUES ('2026-10-29','RELIANCE',1167.40),('2026-10-29','TCS',3050.10)" >/dev/null
chk "write+read"      "$(q "SELECT count() FROM smoke.t" | tr -d '\n')" "2"
chk "value roundtrip" "$(q "SELECT close FROM smoke.t WHERE sym='RELIANCE'" | tr -d '\n')" "1167.4"
q "DROP DATABASE smoke" >/dev/null

echo "== MongoDB =="
chk "ping"      "$(mongosh --quiet --eval 'db.runCommand({ping:1}).ok' 2>/dev/null | tail -1)" "1"
chk "replset"   "$(mongosh --quiet --eval 'db.serverStatus().repl.setName' 2>/dev/null | tail -1)" "rs0"
chk "wt cap 2GB" "$(mongosh --quiet --eval 'print(Number(db.serverStatus().wiredTiger.cache["maximum bytes configured"]).toString())' 2>/dev/null | tail -1)" "2147483648"
chk "write+read" "$(mongosh --quiet smoke --eval 'db.t.drop(); db.t.insertOne({sym:"RELIANCE",close:1167.40}); print(db.t.countDocuments({})); db.dropDatabase()' 2>/dev/null | head -1)" "1"

echo "== Redis =="
chk "ping" "$(redis-cli ping 2>/dev/null)" "PONG"

echo "== Machine =="
swap=$(sysctl -n vm.swapusage | sed -n 's/.*used = \([0-9.]*\)M.*/\1/p')
chk "swap is 0M" "$swap" "0.00"

[ $fail -eq 0 ] && echo "ALL PASS" || echo "FAILURES PRESENT"
exit $fail
