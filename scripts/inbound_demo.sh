#!/usr/bin/env bash
# 入站 HTTP 推送端点复核：真起一个服务，用 curl 推两次，看台账长没长。
#
#   bash scripts/inbound_demo.sh
#
# 验收口径：**第一次 curl 台账 +1 条事件；第二次 curl 台账不变**（幂等）。
# 顺带看一眼未授权请求被拒（401）且不落任何业务数据。
set -euo pipefail

cd "$(dirname "$0")/.."

PORT="${PORT:-8791}"
DB="${DB:-data/inbound_demo.db}"
TOKEN="${LIS_CRITICAL_TOKEN:-demo-lis-token-0001}"
THRESHOLD="${THRESHOLD:-samples/threshold/示例危急值阈值表.xlsx}"

rm -f "$DB" "$DB-wal" "$DB-shm"

echo "== 启动值班台服务承载（监听 127.0.0.1:${PORT}） =="
LIS_CRITICAL_TOKEN="$TOKEN" \
CRITVAL_DB_PATH="$DB" \
CRITVAL_THRESHOLD_PATH="$THRESHOLD" \
  python3 -m critval.console.app --host 127.0.0.1 --port "$PORT" \
  >/tmp/critval-inbound-demo.log 2>&1 &
SERVER_PID=$!
trap 'kill "$SERVER_PID" 2>/dev/null || true' EXIT

# 等端口起来
for _ in $(seq 1 40); do
  if curl -sS -o /dev/null "http://127.0.0.1:${PORT}/api/v1/critical-value-events" 2>/dev/null; then
    break
  fi
  sleep 0.25
done

PAYLOAD='{"event_id":"LIS-EVT-DEMO-0001","item_code":"718-7","value":25,"unit":"g/L","critical_level":"HH","raised_at":"2026-01-02T08:30:00","dept_code":"ICU"}'

count_events() {
  python3 - "$DB" <<'PY'
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
print(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0])
PY
}

# 判定记录条数（任务 id 36）：入站判定一次就落一条判定链。
count_records() {
  python3 - "$DB" <<'PY'
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
print(conn.execute("SELECT COUNT(*) FROM judgement_records").fetchone()[0])
PY
}

echo
echo "== 第一次推送（正常） =="
curl -sS -X POST "http://127.0.0.1:${PORT}/api/v1/critical-value-events" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d "${PAYLOAD}"
echo
echo "台账事件数：$(count_events)"
echo "判定记录数：$(count_records)"

echo
echo "== 第二次推送（同一条，幂等） =="
curl -sS -X POST "http://127.0.0.1:${PORT}/api/v1/critical-value-events" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d "${PAYLOAD}"
echo
echo "台账事件数：$(count_events)"
echo "判定记录数：$(count_records)"

echo
echo "== 未授权推送（无 Bearer） =="
curl -sS -o /dev/null -w "HTTP %{http_code}\n" -X POST \
  "http://127.0.0.1:${PORT}/api/v1/critical-value-events" \
  -H "Content-Type: application/json" \
  -d "${PAYLOAD}"
echo "台账事件数：$(count_events)"
echo "判定记录数：$(count_records)"

echo
echo "== 台账明细 =="
python3 - "$DB" <<'PY'
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
conn.row_factory = sqlite3.Row
for row in conn.execute("SELECT event_id, item_code, value, unit, status, source_channel FROM events"):
    print(" · ".join(f"{k}={row[k]}" for k in row.keys()))
print("审计：")
for row in conn.execute("SELECT action, detail FROM audit ORDER BY seq"):
    print(f"  [{row['action']}] {row['detail']}")
PY

echo
echo "== 判定回放（这条为什么被判为危急，逐步复算） =="
DEMO_EVENT_ID="$(python3 - "$DB" <<'PY'
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
print(conn.execute("SELECT event_id FROM events ORDER BY created_at LIMIT 1").fetchone()[0])
PY
)"
python3 -m critval.cli judge-replay \
  --event "$DEMO_EVENT_ID" \
  --db "$DB" \
  --threshold "$THRESHOLD"
