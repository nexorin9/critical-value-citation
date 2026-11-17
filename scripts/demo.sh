#!/usr/bin/env bash
# 完整演示：安装自检 → 起守护进程 → 投递样例材料 → 触发一条样例危急值 →
# 判定 → 出卡 → 落盘 → 导出。
#
#   scripts/demo.sh                 # 完整演示（守护进程 + 值班台，约 20 秒）
#   scripts/demo.sh --seconds 60    # 守护进程多跑一会儿
#   scripts/demo.sh --card-only     # 只出卡不常驻（最窄的一条主路径）
#   scripts/demo.sh --skip-install  # 跳过 ⓪ 安装自检（只想看处置卡时）
#
# 演示用的是**仓库内样例材料与样例帧**；出站通道（企微 / 排班 / 登记系统）
# 没配凭据位时自动降级：处置卡落到本机通知队列（值班台可见），推送记录里
# 如实写明「未配置企微群机器人 webhook」。
#
# 这条命令同时是**发布前的可重复验证入口**：从干净 clone 跑一遍，就能确认
# 「装得上 → 投得进 → 判得出 → 出得了卡 → 落得了盘 → 导得出表」整条链是通的。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE/.."

PYTHON_BIN="${PYTHON_BIN:-python3}"
SECONDS_TO_RUN=20
CARD_ONLY=0
SKIP_INSTALL=0

while [ $# -gt 0 ]; do
  case "$1" in
    --seconds) SECONDS_TO_RUN="$2"; shift 2 ;;
    --card-only) CARD_ONLY=1; shift ;;
    --skip-install) SKIP_INSTALL=1; shift ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
done

# ---- 只出卡不常驻：原来那条最窄主路径 ---------------------------------------
if [ "$CARD_ONLY" = "1" ]; then
  exec "$PYTHON_BIN" scripts/demo_card.py "$@"
fi

RUN_DIR="data/demo"
FRAME_DIR="$RUN_DIR/inbound_frames"
MATERIAL_DIR="$RUN_DIR/materials"
SNAPSHOT_DIR="$RUN_DIR/card_snapshots"
DB_PATH="$RUN_DIR/critval.db"
LOG_PATH="$RUN_DIR/daemon.log"
mkdir -p "$FRAME_DIR" "$MATERIAL_DIR" "$SNAPSHOT_DIR" "$RUN_DIR"
# 每次演示都从干净状态开始（旧的库 / 快照 / 日志留着只会让输出对不上）
rm -rf "$RUN_DIR/critval.db" "$SNAPSHOT_DIR" "$LOG_PATH"
mkdir -p "$SNAPSHOT_DIR"
: > "$LOG_PATH"

# 值班台端口：挑一个当前空闲的
PORT="$("$PYTHON_BIN" - <<'PY'
import socket
with socket.socket() as probe:
    probe.bind(("127.0.0.1", 0))
    print(probe.getsockname()[1])
PY
)"

THRESHOLD="samples/threshold/示例危急值阈值表.xlsx"
POLICY="samples/policy/示例危急值报告制度.docx"
FRAME="samples/hl7/示例ORU_R01_多项目.hl7"

echo "== 危急值处置依据检索系统 · 完整演示 =="
echo "运行目录：$RUN_DIR ｜ 值班台端口：$PORT"
echo

# ---- ⓪ 安装自检（依赖装得上 / 配置位认得出 / 目录可写 / 台账库能写） --------
# 有阻断项就直接停 —— 装不上的话后面每一步的失败都不好看。
if [ "$SKIP_INSTALL" = "0" ]; then
  echo "⓪ 安装自检（依赖 / 配置位 / 目录权限 / 台账库可写）"
  if ! INSTALL_OUT="$("$PYTHON_BIN" scripts/check_install.py --skip-materials --json 2>&1)"; then
    echo "$INSTALL_OUT" >&2
    echo "安装自检有阻断项，先把上面标 error 的条目处理掉再跑演示" >&2
    exit 1
  fi
  echo "$INSTALL_OUT" | "$PYTHON_BIN" -c "import json,sys; r=json.load(sys.stdin); print('   阻断项 %d 项 · 提醒项 %d 项 · 依赖就绪，可以继续' % (r['errors'], r['warnings']))"
  echo
fi

# ---- ① 投递材料（阈值表 + 制度）到材料目录 --------------------------------
cp "$THRESHOLD" "$MATERIAL_DIR/"
cp "$POLICY" "$MATERIAL_DIR/"
echo "① 材料已投递：$(basename "$THRESHOLD") / $(basename "$POLICY")"

# ---- ② 起守护进程 ----------------------------------------------------------
"$PYTHON_BIN" -m critval.daemon \
  --db "$DB_PATH" \
  --threshold "$MATERIAL_DIR/$(basename "$THRESHOLD")" \
  --policy "$MATERIAL_DIR/$(basename "$POLICY")" \
  --frame-dir "$FRAME_DIR" \
  --material-dir "$MATERIAL_DIR" \
  --snapshot-dir "$SNAPSHOT_DIR" \
  --console-host 127.0.0.1 \
  --console-port "$PORT" \
  --heartbeat-seconds 2 \
  --timer-interval-seconds 10 \
  --run-seconds "$SECONDS_TO_RUN" \
  --log-file "$LOG_PATH" &
DAEMON_PID=$!

cleanup() {
  if kill -0 "$DAEMON_PID" 2>/dev/null; then
    kill "$DAEMON_PID" 2>/dev/null || true
    wait "$DAEMON_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT

# ---- ③ 触发一条样例危急值（ER7 帧投到帧投递目录） -------------------------
sleep 1
cp "$FRAME" "$FRAME_DIR/sample.hl7"
echo "② 样例危急值已投递：$(basename "$FRAME")（钾 / 钠两条结果）"
echo

# ---- ④ 等它处理完 ----------------------------------------------------------
EVENT_ID=""
for _ in $(seq 1 60); do
  EVENT_ID="$("$PYTHON_BIN" - <<PY
import sqlite3
conn = sqlite3.connect("$DB_PATH")
row = conn.execute("SELECT event_id FROM events ORDER BY observed_at DESC LIMIT 1").fetchone()
conn.close()
print(row[0] if row else "")
PY
)"
  [ -n "$EVENT_ID" ] && break
  sleep 0.5
done

if [ -z "$EVENT_ID" ]; then
  echo "演示没跑通：台账里没有事件。守护进程日志见 $LOG_PATH" >&2
  tail -20 "$LOG_PATH" >&2 || true
  exit 1
fi

echo "③ 台账里已经有事件：$EVENT_ID"
echo

# ---- ④ 判定结果（从落库的判定记录里读出来，不重新渲染）--------------------
OBS_DATE="$("$PYTHON_BIN" - <<PY
import sqlite3
conn = sqlite3.connect("$DB_PATH")
row = conn.execute("SELECT observed_at FROM events ORDER BY observed_at DESC LIMIT 1").fetchone()
conn.close()
print((row[0] or "")[:10] if row else "")
PY
)"
echo "④ 判定结果（台账里已落库的事实）"
"$PYTHON_BIN" - <<PY
import sqlite3
conn = sqlite3.connect("$DB_PATH")
for item, value, unit, decision, reason in conn.execute(
    "SELECT item_name, value, unit, judgement_decision, judgement_reason "
    "FROM events ORDER BY observed_at DESC LIMIT 5"
):
    print(f"   · {item} {value} {unit} → {decision}｜{reason}")
conn.close()
PY
echo

# ---- ⑤ 打印心跳与处置卡 ---------------------------------------------------
# 等一次心跳落盘（心跳周期 2 秒），免得这一节空着
sleep 3
echo "---- 守护进程心跳 ----"
grep "心跳" "$LOG_PATH" | tail -2 || true
echo

echo "---- 处置卡（值班群原文 · 离线纯文本）----"
"$PYTHON_BIN" -m critval.cli export \
  --event "$EVENT_ID" \
  --db "$DB_PATH" \
  --snapshot-dir "$SNAPSHOT_DIR" \
  --format text \
  --verify || true
echo

# ---- ⑥ 导出复核台账（次日复核要用的日报明细 + 汇总）----------------------
echo "---- 复核台账导出（按观察日期切日，供次日复核）----"
"$PYTHON_BIN" -m critval.cli export \
  --scope daily \
  --date "$OBS_DATE" \
  --db "$DB_PATH" \
  --out "$RUN_DIR/复核台账_日报.csv" \
  --format csv || true
echo

echo "---- 值班台 ----"
echo "事件台账： http://127.0.0.1:$PORT/events"
echo "确认页面： http://127.0.0.1:$PORT/events/$EVENT_ID/ack"
echo "（配了 CONSOLE_TOKEN 时，链接后加 ?token=… ）"
echo
echo "守护进程最多跑 $SECONDS_TO_RUN 秒；日志：$LOG_PATH"
wait "$DAEMON_PID" 2>/dev/null || true
trap - EXIT
echo "== 演示结束 =="
