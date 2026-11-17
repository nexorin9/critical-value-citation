# 危急值处置依据检索系统 · 部署与运维手册

> 这份手册给**信息科**和**检验科**的接手人：怎么把系统装到值班机上、怎么起、怎么停、
> 怎么看它还活着、配置项逐条是什么意思。
> 值班每班交接该确认什么，看 [handover_checklist.md](handover_checklist.md)；
> 出问题了先查 [troubleshooting.md](troubleshooting.md)。

---

## 1. 部署形态

**单机部署，零外部中间件。** 不装数据库服务、不装消息队列、不装向量库，
一台值班机（或一台常开的小机器）跑一个常驻进程就够了。

| 组件 | 是什么 | 跑在哪 | 数据落在哪 |
|------|--------|--------|-----------|
| 守护进程 `python -m critval.daemon` | 主路径：收危急值 → 判定 → 召回依据 → 出处置卡 → 推送 → 等确认 → 回写 | 值班机常驻（systemd `Restart=always`） | 台账 SQLite + 处置卡快照 |
| 值班台（同一进程内的 HTTP 服务） | 值班人看事件台账、点确认 | 守护进程内起，默认 `127.0.0.1:8760` | 读台账 |
| 入站端点（同一进程内） | LIS 用 HTTP 推危急值过来 | 同一个端口 | 写台账 |
| 运维 CLI `python -m critval.cli` | 排障、导出、对账、补写 | 需要时手动跑 | 只读为主，带 `--resolve` 的会写并留审计 |
| 台账 SQLite | 事件、判定、处置卡、审计流水、幂等键 | `data/critval.db`（WAL 模式） | 本地文件 |
| 材料投递目录 | LIS/院内共享目录投递阈值表 xlsx、制度 docx | `data/materials` | 指纹变了自动重装规则集 |

**不需要什么**：不需要装 Redis / RabbitMQ / Elasticsearch / 向量数据库；
不需要连公网；不需要 GPU；不需要数据库服务进程。

**两台机器怎么办**：值班机跑守护进程 + 值班台，院内另一台可以只跑 CLI
（`--db` 指到共享存储上的库）做对账与导出。但**不要两台机器同时跑同一个数据目录的守护进程**
—— 单实例锁会让第二个进程明确报错退出（这是有意的，别去加 `--no-lock`）。

---

## 2. 目录与文件布局

装完之后项目目录长这样（`data/`、`logs/`、`reports/` 是运行期目录，已在 `.gitignore` 里）：

```
critical-value-citation/
├── .env                     # 院内配置（凭据位；不入库）
├── .env.example             # 配置样例（入库）
├── critval/                 # 源码
├── docs/                    # 本手册与交接、排障、材料规范
├── samples/                 # 示例材料与示例帧（演示用）
├── scripts/                 # 演示与自检脚本
├── tests/                   # 测试
├── data/                    # 【运行期】台账库 / 快照 / 投递目录 / 心跳 / 锁
├── logs/                    # 【运行期】按大小滚动的日志
└── reports/                 # 【运行期】解析报告 / 复核报表 / 配置快照
```

### 运行期目录各管什么

| 路径 | 配置项 | 里面是什么 | 丢了会怎样 |
|------|--------|-----------|-----------|
| `data/critval.db` | `CRITVAL_DB_PATH` | 台账主库（事件 / 判定 / 处置卡 / 审计 / 幂等键） | 历史查不到，幂等去重失效（重复推送挡不住） |
| `data/card_snapshots/` | `CRITVAL_SNAPSHOT_DIR` | 每张处置卡的**不可变快照**（当时推送的原文） | 复核时拿不到「当时发了什么」，只能重新渲染 |
| `data/materials/` | `CRITVAL_MATERIAL_DIR` | 投递进来的阈值表 / 制度文件 | 换版不生效 |
| `data/inbound_frames/` | `CRITVAL_FRAME_DIR` | LIS 用文件方式投递的 ER7 帧 | 走不通文件投递这条入站路 |
| `data/heartbeat.json` | `CRITVAL_HEARTBEAT_PATH` | 心跳（原子写，随时可读） | 外部监控判不出「进程还在动吗」 |
| `data/critval.lock` | `CRITVAL_LOCK_PATH` | 单实例锁 | 第二个守护进程会误启动 |
| `data/local_notifications.jsonl` | `CRITVAL_LOCAL_NOTIFICATION_PATH` | 企微推不出去时的降级落点 | 推送失败的事件无处可查 |
| `logs/` | `CRITVAL_LOG_DIR` | 按大小滚动的运行日志 | 事后复核没有运行轨迹 |
| `reports/` | `CRITVAL_CONFIG_SNAPSHOT_DIR` 等 | 配置快照 / 解析报告 / 复核报表 | 事后查不清「当时按哪版配置判的」 |

---

## 3. 安装与离线自检

### 3.1 装依赖

```bash
cd critical-value-citation
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

只需要三个解析库（openpyxl 读 xlsx、python-docx 读 docx、pypdf 读 pdf），都是纯解析库，
不编译、不联网拉模型。

### 3.2 离线自检（一条命令）

```bash
python3 scripts/check_install.py
```

自检覆盖五件事，**全程离线**（不连任何院内系统、不发任何请求）：

| 检查项 | 看什么 | 不通过时 |
|--------|--------|---------|
| 运行环境 | Python ≥ 3.10、三个解析依赖装没装、版本够不够 | 照提示重装依赖 |
| 配置项 | 必填凭据位齐不齐、格式对不对（**只报键名，不打印凭据值**） | 按提示补 `.env` |
| 目录权限 | 九个运行期目录能不能建、能写 | 换路径或给权限 |
| 台账库 | 在库文件同目录开临时库写一写读一读（**不碰真库**） | 查磁盘与权限 |
| 院内材料 | 阈值表 xlsx、制度 docx/pdf 能不能解析、认出来几行 | 照 [material_spec.md](material_spec.md) 改材料 |

退出码：`0` 无阻断项 / `1` 有阻断项 / `2` 参数错。
机器可读输出加 `--json`；**上线闸门**用 `--strict`（凭据没配齐也算不通过）。

```bash
# 上线前最后一道闸门：任何一项没配齐都不放行
python3 scripts/check_install.py --strict
echo "闸门退出码：$?"   # 必须是 0
```

---

## 4. 启动与停止

### 4.1 前台启动（先试跑）

```bash
cd critical-value-citation
source .venv/bin/activate
python3 -m critval.daemon --console-host 127.0.0.1 --console-port 8760
```

前台跑的时候日志直接打在屏幕上。想跑够多少秒就退，加 `--run-seconds 60`。

### 4.2 交给 systemd 常驻（推荐）

`/etc/systemd/system/critval.service`：

```ini
[Unit]
Description=危急值处置依据检索系统 · 守护进程
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=critval
WorkingDirectory=/opt/critical-value-citation
EnvironmentFile=/opt/critical-value-citation/.env
ExecStart=/opt/critical-value-citation/.venv/bin/python -m critval.daemon
Restart=always
RestartSec=5
KillSignal=SIGTERM
# 停机宽限期：在途事件处理不完就强制结束（默认 30 秒）
TimeoutStopSec=45

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now critval
sudo systemctl status critval
sudo journalctl -u critval -f
```

`EnvironmentFile` 指向 `.env` —— systemd 会把里面的 `KEY=VALUE` 逐条注入环境变量，
这正是本系统读配置的方式（代码里不出现任何密钥，也不读 `.env` 文件本身）。

### 4.3 停止

```bash
sudo systemctl stop critval        # 正常停：SIGTERM，守护进程等在途事件处理完
```

守护进程收到停机信号后按 `CRITVAL_SHUTDOWN_GRACE_SECONDS`（默认 30 秒）宽限期收尾：
把手上事件处理完、把心跳写完、把锁文件清掉。超过宽限期强制结束 —— 这时未处理完的事件
**不会丢**，下次启动时按幂等键续跑（见 [troubleshooting.md](troubleshooting.md) 第 7 条）。

### 4.4 离线升级

```bash
sudo systemctl stop critval
cd /opt && tar xzf critical-value-citation-<版本>.tar.gz   # 换新目录，不覆盖旧目录
cp /opt/critical-value-citation-<版本>/.env.example /opt/critical-value-citation-<版本>/.env
# 把旧目录的 .env 拷过去（凭据位不进版本库），data/ 与 logs/ 建议保留
cd /opt/critical-value-citation-<版本>
source .venv/bin/activate && pip install -r requirements.txt
python3 scripts/check_install.py --strict
sudo systemctl daemon-reload && sudo systemctl restart critval
```

**升级前的台账备份**（一定要做，审计要用）：

```bash
sqlite3 data/critval.db ".backup '/备份路径/critval-$(date +%Y%m%d).db'"
```

---

## 5. 健康检查

### 5.1 进程还活着吗（最简）

```bash
# ① 心跳文件：守护进程每个心跳周期原子写一次，随时可读
cat data/heartbeat.json

# ② HTTP 健康检查（值班台与入站端点在同一个端口上）
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8760/healthz
```

心跳文件里有队列深度、处理计数、最近错误与生效的配置版本号。

### 5.2 台账体检（开机第一问）

```bash
python3 -m critval.cli doctor
```

体检回答**四件能核对的事**：

1. **库还完整吗** —— `PRAGMA integrity_check`；
2. **并发加固开着吗** —— WAL / busy_timeout / 幂等键唯一索引；
3. **事件都走到哪一步了** —— 各状态分布 + 待办计数（等回执的、回写失败的、转人工的）；
4. **现在用的是哪份配置与哪版规则** —— 配置版本号 + 规则集快照 ID。

体检**不替人做决定**：发现的问题分 `error`（必须处理，退出码非零）、`warn`（要盯着）、
`info`（当前状态说明）三级。只有 `error` 会让命令非零退出 —— 「有人待回执」不是故障。

加 `--json` 出机器可读结论，接进监控。

### 5.3 监控建议

| 监控项 | 判据 | 处置 |
|--------|------|------|
| 进程存活 | `systemctl is-active critval` 非 `active` | 重启并查日志 |
| 心跳新鲜度 | `data/heartbeat.json` 修改时间距今 > 3 × `CRITVAL_HEARTBEAT_SECONDS` | 进程卡死，查日志 |
| 体检退出码 | `python3 -m critval.cli doctor` 退出码非 0 | 按 error 项逐条处理 |
| 待办堆积 | `doctor` 的待办计数持续上涨 | 查推送与回写通道 |

---

## 6. 日志与心跳

| 产物 | 位置 | 内容 | 保留 |
|------|------|------|------|
| 运行日志 | `logs/`（`CRITVAL_LOG_DIR`） | 收帧、判定、出卡、推送、回写、处置链每一步 | 按 `CRITVAL_LOG_MAX_BYTES`（默认 5 MiB）滚动，保留 `CRITVAL_LOG_BACKUP_COUNT`（默认 5）份 |
| 心跳 | `data/heartbeat.json`（`CRITVAL_HEARTBEAT_PATH`） | 队列深度 / 处理计数 / 最近错误 / 配置版本号 | 原子覆盖写，实时可读 |
| 处置卡快照 | `data/card_snapshots/` | 每张卡当时推送的原文（多渠道）+ 内容指纹 | 长期保留（复核与飞检要用） |
| 审计流水 | 台账库 `audit` 表 | 每一步动作 + 链式哈希 | 长期保留 |
| 配置快照 | `reports/config_snapshots/` | 每次热载入的键集合 / 来源 / 脱敏值 / 差异 | 长期保留 |

日志里的凭据**只显示首 4 尾 4 与长度**（`aaaa…zzzz (32 chars)`），完整密钥不落任何文本产物。
`logs/` `data/` `reports/` 都在 `.gitignore` 里，不进版本库。

---

## 7. 配置项速查（与 `.env.example` 逐条对应）

配置只有两个来源：**系统环境变量** 与 **`.env` 文件**（由 systemd `EnvironmentFile` 或容器注入）。
代码不读 `.env` 文件、不含任何密钥默认值 —— 凭据位缺就是缺，不给兜底假值。

| 配置项 | 默认值 | 服务哪条通道 | 缺失 / 填错会怎样 |
|--------|--------|-------------|------------------|
| `CRITVAL_WECOM_WEBHOOK_URL` | 必填（无默认值） | 出站：企业微信群机器人推送处置卡 | 处置卡推不出去，落本机通知队列降级 |
| `CRITVAL_BASE_URL` | 必填（无默认值） | 出站：危急值登记系统回写回执 | 回执回写不了，事件停在待补写 |
| `CRITVAL_TOKEN` | 必填（无默认值） | 出站：危急值登记系统回写回执 | 同上（≥12 位，少于 12 位启动即报错） |
| `NURSE_ROSTER_BASE_URL` | 必填（无默认值） | 出站：排班系统读取当班人 | 解析不出当班人，通知对象空缺 |
| `ROSTER_TOKEN` | 必填（无默认值） | 出站：排班系统读取当班人 | 同上（≥12 位） |
| `CRITVAL_LISTEN_BASE_URL` | 必填（`.env.example` 里给 `http://127.0.0.1:8760`） | 入站：LIS 以 HTTP 推送危急值 | 告诉 LIS 的回调地址不对，推送 404 |
| `LIS_CRITICAL_TOKEN` | 必填（无默认值） | 入站：LIS 推送危急值时校验 Bearer 凭据 | 推送被拒（401），事件进不来（≥12 位） |
| `LIS_BASE_URL` | 无默认值 | 入站：LIS REST 回调 | 与 `LIS_MLLP_BIND` **至少配一条**，两条都空则启动自检不通过 |
| `LIS_MLLP_BIND` | 无默认值 | 入站：HL7 v2 MLLP 监听 | 同上（如 `0.0.0.0:2575`） |
| `CONSOLE_TOKEN` | 必填（无默认值） | 入站：值班台访问鉴权 | 值班台页面打不开（≥12 位） |
| `CRITVAL_DB_PATH` | `./data/critval.db` | 本地：台账 / 审计 / 幂等键库文件 | 改路径后历史台账不在新库里 |
| `CRITVAL_CONFIG_SNAPSHOT_DIR` | `./reports/config_snapshots` | 本地：配置快照落盘 | 事后查不清「当时按哪版配置判的」 |
| `CRITVAL_THRESHOLD_PATH` | `./samples/threshold/示例危急值阈值表.xlsx` | 本地：阈值表材料来源 | **上线必须改成本院文件**，否则按示例阈值判 |
| `CRITVAL_INBOUND_MAX_BODY_BYTES` | `65536` | 入站：推送请求体上限 | 超了直接拒（危急值推送是一条结果，64 KiB 足够） |
| `CRITVAL_INBOUND_RATE_LIMIT_PER_MINUTE` | `120` | 入站：单来源地址限流 | 推得太猛会被限流拒收 |
| `CRITVAL_MERGE_WINDOW_SECONDS` | `300` | 本地：合并通知时间窗 | 同标本多项目合并成一张卡的窗口；设 0 则一条一张卡 |
| `CRITVAL_LOCAL_NOTIFICATION_PATH` | `./data/local_notifications.jsonl` | 本地：推送降级落点 | 企微推不出去时处置卡无处可查 |
| `CRITVAL_NOTIFY_MAX_ATTEMPTS` | `3` | 本地：投递重试次数 | 超过即落死信（事件不丢，等人处理） |
| `CRITVAL_NOTIFY_BACKOFF_BASE_SECONDS` | `0.5` | 本地：退避基数 | 第一次重试等这么久 |
| `CRITVAL_NOTIFY_BACKOFF_FACTOR` | `2` | 本地：退避倍数 | 每重试一次等待乘这个数 |
| `CRITVAL_NOTIFY_MAX_BACKOFF_SECONDS` | `8` | 本地：单次等待上限 | 再长也不该让危急值干等 |
| `CRITVAL_NOTIFY_JITTER_RATIO` | `0.2` | 本地：等待时长抖动 | 避免多台值班机同步重试；0 = 不抖动 |
| `CRITVAL_POLICY_PATH` | `./samples/policy/示例危急值报告制度.docx` | 本地：院内制度材料来源 | **上线必须改成本院文件**，否则召回的是示例依据 |
| `CRITVAL_FRAME_DIR` | `./data/inbound_frames` | 本地：ER7 帧投递目录 | 走不通文件投递这条入站路 |
| `CRITVAL_MATERIAL_DIR` | `./data/materials` | 本地：材料投递目录 | 换版不自动生效 |
| `CRITVAL_SNAPSHOT_DIR` | `./data/card_snapshots` | 本地：处置卡不可变快照 | 复核时拿不到「当时发了什么」 |
| `CRITVAL_QUEUE_SIZE` | `256` | 本地：有界队列容量 | 队列满就拒收并留痕（不无限堆内存） |
| `CRITVAL_WORKERS` | `2` | 本地：并发处理线程数 | 危急值量大时调大；调太大反而抢锁 |
| `CRITVAL_HEARTBEAT_SECONDS` | `30` | 本地：心跳周期 | 监控判活频率跟着它走 |
| `CRITVAL_TIMER_INTERVAL_SECONDS` | `60` | 本地：定时任务周期 | 到期复查重推 + 崩溃续跑的间隔 |
| `CRITVAL_HEARTBEAT_PATH` | `./data/heartbeat.json` | 本地：心跳文件 | 外部监控判不出进程死活 |
| `CRITVAL_LOCK_PATH` | `./data/critval.lock` | 本地：单实例锁 | 第二个守护进程会误启动 |
| `CRITVAL_SHUTDOWN_GRACE_SECONDS` | `30` | 本地：停机宽限期 | 在途事件处理不完就强制结束（未处理的下次续跑） |
| `CRITVAL_LOG_DIR` | `./logs` | 本地：日志目录 | 事后复核没有运行轨迹 |
| `CRITVAL_LOG_MAX_BYTES` | `5242880` | 本地：单日志文件上限（字节） | 超过就滚动 |
| `CRITVAL_LOG_BACKUP_COUNT` | `5` | 本地：保留历史日志份数 | 翻旧日志用（故不自动清空） |

改完配置**不用重启进程**：守护进程会热载入并原子切换整份配置快照（判定 / 召回 / 推送在
一次处理内持有同一份快照，不会半程切换）。每次热载入落一份脱敏快照到
`reports/config_snapshots/`，可查「这次改了什么」。

```bash
# 改完先看配置自检结论，再热载入
python3 -m critval.cli config-check
```

---

## 8. 值班台访问

| 项 | 说明 |
|----|------|
| 地址 | 默认 `http://127.0.0.1:8760`（`--console-host` / `--console-port` 可改） |
| 事件台账 | `http://127.0.0.1:8760/events` |
| 确认页面 | `http://127.0.0.1:8760/events/<事件ID>/ack` |
| 访问令牌 | `CONSOLE_TOKEN`；链接后加 `?token=…`，或由反向代理统一鉴权 |
| 健康检查 | `http://127.0.0.1:8760/healthz` |

**值班台不代替电话**：确认动作必须由值班人点，系统不会自动推进状态。
任何自动路径都不会把事件从「待人工确认」推成「已生效事实」。

---

## 9. 相关文档

| 文档 | 什么时候看 |
|------|-----------|
| [handover_checklist.md](handover_checklist.md) | 每班交接、机器重启后开机 |
| [troubleshooting.md](troubleshooting.md) | 出问题了（入站不通 / 推送失败 / 回写失败 / 名单过期 / 阈值未绑定） |
| [material_spec.md](material_spec.md) | 院内要投新的阈值表或新的报告制度 |
| [production_seams.md](production_seams.md) | 要对接院内系统（字段规范、凭据位、失败语义） |
| [dialect_matrix.md](dialect_matrix.md) | LIS 厂商报文格式对不上时 |
