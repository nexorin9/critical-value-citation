# 常见故障自助排查

> 值班台半夜响的时候翻这一页。每条按 **症状 → 可能原因 → 处理动作 → 何时转人工** 四段写，
> 处理动作里的命令可以直接复制执行。
>
> **一条底线**：任何自动路径都不会把危急值判成「已完成」。系统没把握的时候，
> 会把事件转人工并写明原因 —— **转人工不是故障，是设计**。人工处理完再在值班台点确认。

---

## 0. 先跑这一条（30 秒定位）

```bash
python3 -m critval.cli doctor
```

体检会直接告诉你：库完不完整、并发加固开没开、事件卡在哪一步、有多少待办、
现在用的是哪版配置与哪版规则。下面 9 条按体检结论对号入座。

| 体检里看到 | 去查 |
|------------|------|
| 事件总数 0 | 第 1 条（入站不通） |
| 大量「已接收未判定」 | 第 7 条（规则集没生效） |
| 大量「已判定未出卡」 | 第 7 条 |
| 大量「已推送待回执」 | 第 3 条（回写 / 确认） |
| 大量「回写失败待补写」 | 第 3 条 |
| 大量「推送重试耗尽待人工」 | 第 2 条（推送失败） |
| 大量「转人工待接手」 | 第 8 条（空召回 / 名单问题） |
| `journal_mode` 不是 WAL | 第 9 条 |
| 库完整性检查不通过 | 第 9 条（**立刻转人工**） |

---

## 1. 入站不通：LIS 推了，系统收不到

**症状**：LIS 侧说「危急值已推送」，但值班台事件台账里没有新事件；
或推送接口返回 401 / 404 / 连接超时。

**可能原因**

| 原因 | 怎么确认 |
|------|---------|
| LIS 用的推送地址不对 | 问 LIS 侧他们推的 URL，和 `CRITVAL_LISTEN_BASE_URL` 对一遍 |
| 凭据不对 | 推送返回 401；比 `LIS_CRITICAL_TOKEN` 与 LIS 侧配的是否一致 |
| 端口没监听 | `curl -sS http://127.0.0.1:8760/healthz` 无响应 |
| 入站通道一条都没配 | `python3 -m critval.cli config-check` 报「入站通道组 lis_inbound 全空」 |
| 防火墙挡了 | 从 LIS 机器上 `telnet <本机> 8760` 或 `curl` 试 |
| 帧投递目录没权限 | 目录属主与守护进程运行账号不一致 |

**处理动作**

```bash
# ① 配置面：入站通道组至少配一条（MLLP 监听 或 LIS REST 回调）
python3 -m critval.cli config-check

# ② 进程在不在、端口通不通
systemctl status critval
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8760/healthz

# ③ 手工投一条样例帧，确认本机这条入站路是通的
cp samples/hl7/示例ORU_R01_正常.hl7 data/inbound_frames/probe.hl7
sleep 2 && python3 -m critval.cli doctor     # 看事件总数有没有涨
```

**何时转人工**：手工投帧都进不来 → 转信息科查进程与网络；
LIS 侧说推了但抓包看不到请求 → 转 LIS 厂商。

---

## 2. 推送失败：处置卡出不来 / 值班群收不到

**症状**：台账里事件停在「已出卡未推送」；`notify-queue` 里死信条数 > 0；
值班群没消息。

**可能原因**

| 原因 | 怎么确认 |
|------|---------|
| 企微 webhook 没配 | `config-check` 报缺 `CRITVAL_WECOM_WEBHOOK_URL`；队列条目里写「未配置企微群机器人 webhook」 |
| webhook 地址过期 | 群里机器人被删了；重新建机器人拿新 key |
| 网络不通 | `notify-queue` 显示投递尝试记录里有连接错误 |
| 值班群被限流 | 同一分钟推送太密（`CRITVAL_INBOUND_RATE_LIMIT_PER_MINUTE` 管的是入站，不是这个） |

**处理动作**

```bash
# ① 看队列 / 死信 / 每次投递尝试
python3 -m critval.cli notify-queue --db data/critval.db

# ② 补上 webhook 后热载入配置（不用重启进程）
python3 -m critval.cli config-check

# ③ 死信里的事件重新推 —— 值班台页面上有「重新推送」，或等到期复查自动重推
```

**何时转人工**：死信里的事件**本班必须人工电话通知当班人**，并在值班台点确认；
企微本身推不出去（群被封、机器人被删）→ 转信息科换推送通道。

---

## 3. 回写失败：值班人确认了，登记系统里没记录

**症状**：台账里事件停在「已推送待回执」或「回写失败待补写」；
`reconcile` 差异清单里出现 `missing_remote`（登记系统缺记录）。

**可能原因**

| 原因 | 怎么确认 |
|------|---------|
| 登记系统地址 / 令牌错 | `config-check` 报缺 `CRITVAL_BASE_URL` / `CRITVAL_TOKEN`；令牌少于 12 位启动就报错 |
| 令牌过期 | 补写时报 401 |
| 登记系统侧字段不匹配 | 补写报 400；用 `reconcile --only field_mismatch` 看差在哪 |
| 网络不通 | 补写报连接错误 |
| 值班人没点确认 | 台账状态还是「已推送待回执」—— 这不是故障，是**等确认** |

**处理动作**

```bash
# ① 只看待写队列（失败分类 / 次数 / 下一步），不补写
python3 -m critval.cli writeback-retry --list

# ② 逐条补写（会记审计，必须带 --actor）
python3 -m critval.cli writeback-retry --event <事件ID> --actor "检验科组长"

# ③ 全量对账，看本地与登记系统差在哪
python3 -m critval.cli reconcile --db data/critval.db --out reports/reconcile_diff.csv
```

**何时转人工**：补写持续 401/400 → 转信息科与登记系统厂商；
差异清单里 `status_mismatch`（状态对不上但记录都在）→ 转检验科组长人工裁决，
**系统不会自动择一改写**。

---

## 4. 当班名单过期 / 解析不出通知对象

**症状**：处置卡上「通知对象」是空的，或写的是「当班人未解析」；
或者通知对象是上一班的人。

**可能原因**

| 原因 | 怎么确认 |
|------|---------|
| 排班系统读不到 | `config-check` 报缺 `NURSE_ROSTER_BASE_URL` / `ROSTER_TOKEN` |
| 名单缓存过期 | 名单来源标成 `cache` 且已超有效期（默认 900 秒） |
| 交班后名单没同步 | 排班系统里本班人员还没更新 |
| 时刻落在班次窗口外 | 交接那一刻正好在两个班次的交界上 |

**处理动作**

```bash
# ① 名单来源与有效期
python3 -m critval.cli roster-resolve --roster <排班响应文件> --at "2026-01-01 08:00"
#    --origin cache 时加 --as-of "<取数时刻>" 复核缓存是否过期

# ② 排班系统通不通
curl -sS -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $ROSTER_TOKEN" \
     "$NURSE_ROSTER_BASE_URL/api/v1/duty-roster"
```

**何时转人工**：名单读不到 → **先电话通知当班人**（处置卡仍会推，只是通知对象空缺），
同时转信息科查排班系统通道。**不要因为名单问题压住危急值不报。**

---

## 5. 阈值未绑定 / 规则集有缺口

**症状**：`rules-summary` 显示「缺口条数 > 0」；某项目的结果不判危急；
处置卡写「无阈值依据」。

**可能原因**

| 原因 | 怎么确认 |
|------|---------|
| 项目码没绑版本 | `rules-summary` 的缺口清单里就是这个项目码 |
| 阈值表里根本没这个项目 | `probe --item <项目码>` 查候选是空的 |
| 阈值表换了新版，绑定没跟上 | `version-check` 报版本空档 |
| 单位不一致且换算表里没有 | 判定报「缺换算」→ 转人工，不猜 |

**处理动作**

```bash
# ① 看缺口与生效版本
python3 -m critval.cli rules-summary --threshold <本院阈值表路径>
python3 -m critval.cli version-check --threshold <本院阈值表路径>

# ② 试判一条，看它到底卡在哪
python3 -m critval.cli probe --item <项目码> --value "<观察值>" --unit <单位>

# ③ 按观察时刻推导绑定表（确认后写入）
python3 -m critval.cli bind --derive --threshold <阈值表> --policy <制度> --at 2026-01-01
```

**何时转人工**：项目码两边对不上（LIS 报的码与阈值表的码不是一回事）→ 转检验科；
单位换算缺失 → 转检验科确认换算关系，**系统不会自己猜一个换算系数**。

---

## 6. 材料版本不生效 / 换版没切过去

**症状**：换了新的阈值表或制度，系统还在按旧的判；
`rules-summary` 的规则快照 ID 没变。

**可能原因**

| 原因 | 怎么确认 |
|------|---------|
| 材料没放进投递目录 | `kb-watch --dir data/materials` 报「无变化」 |
| 文件内容没变 | 指纹（sha256）没变 → 不会重装；改内容才会 |
| 生效期还没到 | `version-check --at <观察时刻>` 看哪一版生效 |
| 版本空档 | 观察时刻落在旧版失效日与新版生效日之间 |

**处理动作**

```bash
# ① 扫材料目录，比对指纹
python3 -m critval.cli kb-watch --dir data/materials        # 只看事件
python3 -m critval.cli kb-watch --dir data/materials --apply  # 确认后重建

# ② 看生效版本与空档
python3 -m critval.cli version-check --threshold <阈值表> --at 2026-01-01

# ③ 换版影响面（只读复核，不改写历史）
python3 -m critval.cli impact-check --old <旧表> --new <新表>
```

**何时转人工**：版本空档要有业务决定（空档期按哪版判）→ 转检验科组长与医务科，
**系统不会替医院决定**。

---

## 7. 事件卡在队列里不动

**症状**：台账里大量事件停在「已接收未判定」或「已判定未出卡」；心跳里队列深度一直涨。

**可能原因**

| 原因 | 怎么确认 |
|------|---------|
| 规则集没装载 | `doctor` 报「未装载规则集 —— 判定引擎没有生效版本」 |
| 阈值表路径不对 | `config-check` 里的 `CRITVAL_THRESHOLD_PATH` 指向示例文件或不存在路径 |
| 队列满了 | 心跳里队列深度 ≥ `CRITVAL_QUEUE_SIZE`（默认 256），满了就拒收并留痕 |
| 处理线程卡住 | 心跳不再更新（心跳周期默认 30 秒） |

**处理动作**

```bash
# ① 确认规则集装上了
python3 -m critval.cli rules-summary --threshold <阈值表路径>

# ② 看心跳与队列深度
cat data/heartbeat.json

# ③ 重启守护进程（未处理完的按幂等键续跑，不丢）
sudo systemctl restart critval
```

**何时转人工**：心跳不更新 → 进程卡死，**先保留日志再重启**
（`cp -r logs /tmp/logs-$(date +%H%M)`），把日志转给信息科。

---

## 8. 召回不到依据 / 处置卡缺「依据」

**症状**：处置卡的依据行是空的，或写着「召回 0 条，转人工」。

**可能原因**

| 原因 | 怎么确认 |
|------|---------|
| 制度里那段话写法太自由 | `policy-summary` 显示该段没认出来 |
| 综合分没过硬闸门 | `probe` 输出的候选都在 `similarity-threshold` 之下 |
| 项目码对不上 | `probe --item <项目码>` 的结构化路没命中 |
| 制度根本没写这项 | 确实没有依据 —— 系统不编 |

**处理动作**

```bash
# ① 试检索，看候选与得分
python3 -m critval.cli probe --item <项目码> --query "<检索式>"

# ② 召回分级降级：原样 → 关键词扩展 → 章节级兜底 → 转人工
python3 -m critval.cli recall-fallback --query "<检索式>"

# ③ 两路召回差异归因（口径冲突 / 版本冲突清单，系统不自动择一）
python3 -m critval.cli recall-conflict
```

**何时转人工**：召回 0 条 → **人工写依据**（用制度原文），并在值班台记录处理人；
口径冲突（阈值表与制度说法不一致）→ 转检验科组长裁决。

---

## 9. 台账库异常：database is locked / 完整性不通过

**症状**：日志里刷 `database is locked`；`doctor` 报完整性检查不通过；
事件写不进去。

**可能原因**

| 原因 | 怎么确认 |
|------|---------|
| 并发加固没开 | `doctor` 报 `journal_mode` 不是 WAL 或 `busy_timeout` 为 0 |
| 数据目录在 NFS 上 | WAL 不支持网络文件系统 |
| 两个守护进程抢同一个数据目录 | 单实例锁会报「已有实例在跑」；**不要用 `--no-lock` 绕过** |
| 磁盘满了 | `df -h` |

**处理动作**

```bash
# ① 体检（看并发加固与完整性）
python3 -m critval.cli doctor

# ② 确认只有一个实例
ls -l data/critval.lock && cat data/critval.lock

# ③ 磁盘
df -h .
```

**何时转人工**：**库完整性检查不通过 → 立刻转人工，别自己修库**。
先备份（`sqlite3 data/critval.db ".backup '/备份路径/critval-$(date +%Y%m%d).db'"`），
再让信息科与开发方处理。审计流水断链会让飞检复核失效。

---

## 10. 值班台打不开

**症状**：浏览器打不开 `http://127.0.0.1:8760/events`；或一直跳登录。

**可能原因**

| 原因 | 怎么确认 |
|------|---------|
| 令牌不对 | `CONSOLE_TOKEN` 没配或与访问时填的不一致（≥12 位） |
| 端口不对 | 守护进程启动时 `--console-port` 改过端口 |
| 反向代理没配 | 院内走 nginx / 统一门户代理时，本机通但外部不通 |
| 进程没起 | `systemctl status critval` |

**处理动作**

```bash
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8760/healthz
python3 -m critval.cli config-check
```

**何时转人工**：本机通、外部不通 → 转信息科查反向代理与防火墙。

---

## 11. 审计链断了

**症状**：`audit-verify` 报断链，报出首个断链位置。

**处理动作**

```bash
# 逐条重算链式哈希，定位第一个断点
python3 -m critval.cli audit-verify

# 异地复核：把流水连链哈希一起导出成 CSV
python3 -m critval.cli audit-verify --export reports/audit_chain.csv
```

**何时转人工**：**审计链断了必须转人工**（飞检要看这条链）。
系统只报位置，不自动补链 —— 补出来的链是假的。

---

## 12. 机器要重启 / 升级前

```bash
# ① 先体检，把结论留在日志里
python3 -m critval.cli doctor

# ② 备份台账（审计要用）
sqlite3 data/critval.db ".backup '/备份路径/critval-$(date +%Y%m%d).db'"

# ③ 优雅停机（SIGTERM，在途事件处理完再退）
sudo systemctl stop critval

# ④ 升级后开机自检 + 重跑交接五项
python3 scripts/check_install.py --strict
```

重启后未处理完的事件按幂等键**续跑**，不会重复推送（幂等键是 LIS 的消息控制号）。
如果重启后发现同一条危急值推了两次 → 查 [handover_checklist.md](handover_checklist.md)
第 ① 项（是不是起了两个实例），并转信息科。
