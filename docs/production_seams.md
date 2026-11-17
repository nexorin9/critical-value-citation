# 落生产核对清单 · 危急值处置依据检索系统

> 本清单由 `python3 scripts/audit_production.py` 扫产品代码生成，**不手写**。每条接缝都能在代码里找到出处；核对的人可以逐条点开看。

生成口径：产品代码目录 `critval/`；替身只允许出现在 `tests/fixtures/`；凭据一律从环境变量读，代码内不出现密钥。

## 一、总览

| 方向 | 条数 | 对端 |
|------|------|------|
| 出站 | 4 | 排班 / 值班名单系统、院内危急值登记 / 护理执行平台、企业微信值班群机器人、LIS 侧危急值登记接口 |
| 入站 | 2 | LIS / 检验科信息系统、LIS |

本产品**入站只读 LIS 推送**（不写 LIS），**出站只写院内登记系统与值班群**，**读排班系统**取当班人；绝不代签、绝不写回患者可识别明细。

### ① 排班 / 值班名单系统 · GET

| 项 | 内容 |
|----|------|
| 方向 | 出站 |
| 对端系统 | 排班 / 值班名单系统 |
| 协议 / 方法 | HTTP（REST） / GET |
| 目标地址模板 | `{NURSE_ROSTER_BASE_URL}/api/v1/duty-roster?dept_code=…` |
| 凭据位（环境变量） | `NURSE_ROSTER_BASE_URL`、`ROSTER_TOKEN` |
| 口径要点 | 读取失败时降级为带时间戳的缓存；缓存过期即转人工，不拿过期名单冒充当前当班人。 |
| 本轮是否接主路径 | 是 |

**字段规范**

| 字段 | 方向 | 必填 | 说明 |
|------|------|------|------|
| `dept_code` | 请求 | 是 | 按科室查当班名单 |
| `Authorization` | 请求 | 是 | Bearer $ROSTER_TOKEN |
| `姓名 / name` | 响应 | 是 | 当班人姓名 |
| `工号 / staff_id` | 响应 | 否 | 同层级择人时的稳定排序键 |
| `岗位 / role` | 响应 | 否 | 角色优先级：主责 > 备班 > 科主任 |
| `shift_start` | 响应 | 否 | 班次起点（墙钟） |
| `shift_end` | 响应 | 否 | 班次终点（墙钟，24:00 认作当天结束） |

**代码证据**

- `critval/roster/client.py:68` — `ROSTER_PATH = "/api/v1/duty-roster"`
- `critval/roster/client.py:321` — `request = urllib.request.Request(`
- `critval/roster/client.py:326` — `"Authorization": f"Bearer {self.token}",`

**凭据位在代码里怎么读**

- `NURSE_ROSTER_BASE_URL` ← `critval/daemon.py:1450` — `base_url=Config.get_env("NURSE_ROSTER_BASE_URL") or "",`
- `ROSTER_TOKEN` ← `critval/roster/client.py:221` — `token = Config.get_env("ROSTER_TOKEN") or ""`

### ② 院内危急值登记 / 护理执行平台 · POST

| 项 | 内容 |
|----|------|
| 方向 | 出站 |
| 对端系统 | 院内危急值登记 / 护理执行平台 |
| 协议 / 方法 | HTTP（REST） / POST |
| 目标地址模板 | `{CRITVAL_BASE_URL}/api/v1/critical-events` |
| 凭据位（环境变量） | `CRITVAL_BASE_URL`、`CRITVAL_TOKEN` |
| 口径要点 | 只有台账状态为 acknowledged（值班人已确认）才发起；其余状态一次出站调用都不发。 |
| 本轮是否接主路径 | 是 |

**字段规范**

| 字段 | 方向 | 必填 | 说明 |
|------|------|------|------|
| `message_control_id` | 请求 | 是 | 幂等键 —— 登记系统按它覆盖同一条 |
| `item_code` | 请求 | 是 | 检验项目码 |
| `value` | 请求 | 是 | 观察值 |
| `threshold_row` | 请求 | 是 | 阈值依据行定位串（文件名 · 行号 · 版本） |
| `notified_dept` | 请求 | 是 | 已通知科室 |
| `duty_person` | 请求 | 否 | 当班人（岗位口径，不含患者信息） |
| `due_minutes` | 请求 | 否 | 上报时限（分钟） |
| `receipt_status` | 请求 | 是 | 回执状态 |
| `receipt_at` | 请求 | 否 | 回执时刻 |
| `Authorization` | 请求 | 是 | Bearer $CRITVAL_TOKEN |

**代码证据**

- `critval/writeback/client.py:57` — `"RECEIPT_FIELDS",`
- `critval/writeback/client.py:67` — `RECEIPT_PATH = "/api/v1/critical-events"`
- `critval/writeback/client.py:70` — `RECEIPT_FIELDS: tuple[str, ...] = (`
- `critval/writeback/client.py:130` — `extra = {k: v for k, v in data.items() if k not in RECEIPT_FIELDS}`
- `critval/writeback/client.py:300` — `request = urllib.request.Request(url, data=data, method=method, headers=headers)`
- `critval/writeback/client.py:374` — `"""台账事件行 → 回写载荷（字段按 :data:`RECEIPT_FIELDS`）。`
- `critval/writeback/registry.py:57` — `RECEIPT_FIELDS,`
- `critval/writeback/registry.py:84` — `"RECEIPT_FIELDS",`

**凭据位在代码里怎么读**

- `CRITVAL_BASE_URL` ← `critval/daemon.py:1454` — `base_url=Config.get_env("CRITVAL_BASE_URL") or "",`
- `CRITVAL_TOKEN` ← `critval/writeback/client.py:234` — `token = Config.get_env("CRITVAL_TOKEN") or ""`

### ③ 企业微信值班群机器人 · POST

| 项 | 内容 |
|----|------|
| 方向 | 出站 |
| 对端系统 | 企业微信值班群机器人 |
| 协议 / 方法 | HTTPS（webhook） / POST |
| 目标地址模板 | `{CRITVAL_WECOM_WEBHOOK_URL}` |
| 凭据位（环境变量） | `CRITVAL_WECOM_WEBHOOK_URL` |
| 口径要点 | webhook 完整地址本身即凭据；日志与文案只出现脱敏形式（前 4 后 4 + 长度）。 |
| 本轮是否接主路径 | 是 |

**字段规范**

| 字段 | 方向 | 必填 | 说明 |
|------|------|------|------|
| `msgtype` | 请求 | 是 | 固定 markdown |
| `markdown.content` | 请求 | 是 | 处置卡正文（含出处行号 + 确认链接） |
| `errcode` | 响应 | 是 | 0 才算推送成功；HTTP 200 不等于推送成功 |
| `errmsg` | 响应 | 否 | 失败原因 |

**代码证据**

- `critval/notify/wecom.py:18` — `{"msgtype": "markdown", "markdown": {"content": "<处置卡正文>"}}`
- `critval/notify/wecom.py:210` — `payload = {"msgtype": "markdown", "markdown": {"content": text}}`
- `critval/notify/wecom.py:266` — `request = urllib.request.Request(`
- `critval/notify/wecom.py:298` — `payload = {"msgtype": "markdown", "markdown": {"content": text}}`
- `critval/notify/wecom.py:354` — `errcode, errmsg, retryable = self._read_errcode(text)`
- `critval/notify/wecom.py:367` — `def _read_errcode(text: str) -> tuple[int, str, bool]:`

**凭据位在代码里怎么读**

- `CRITVAL_WECOM_WEBHOOK_URL` ← `critval/daemon.py:271` — `url = Config.get_env("CRITVAL_WECOM_WEBHOOK_URL") or ""`

### ④ LIS 侧危急值登记接口（部分厂商的接口引擎要求回调） · POST

| 项 | 内容 |
|----|------|
| 方向 | 出站 |
| 对端系统 | LIS 侧危急值登记接口（部分厂商的接口引擎要求回调） |
| 协议 / 方法 | HTTP（REST） / POST |
| 目标地址模板 | `{LIS_BASE_URL}/api/v1/critical-value-events` |
| 凭据位（环境变量） | `LIS_BASE_URL`、`LIS_CRITICAL_TOKEN` |
| 口径要点 | 条件启用：与 MLLP 至少配一条入站通道；本轮未接入守护进程主循环。 |
| 本轮是否接主路径 | 否（条件启用） |

**字段规范**

| 字段 | 方向 | 必填 | 说明 |
|------|------|------|------|
| `event_id` | 请求 | 是 | 取事件的消息控制号（LIS 侧认识的号） |
| `item_code` | 请求 | 是 | 检验项目码 |
| `value` | 请求 | 是 | 观察值 |
| `unit` | 请求 | 否 | 单位 |
| `critical_level` | 请求 | 否 | 危急级别 |
| `raised_at` | 请求 | 是 | 结果时刻 |
| `dept_code` | 请求 | 否 | 科室 |
| `Authorization` | 请求 | 是 | Bearer $LIS_CRITICAL_TOKEN |

**代码证据**

- `critval/hl7/rest_channel.py:52` — `LIS_REST_EVENT_PATH = "/api/v1/critical-value-events"`
- `critval/hl7/rest_channel.py:178` — `request = urllib.request.Request(`
- `critval/hl7/rest_channel.py:184` — `"Authorization": f"Bearer {self.token}",`

**凭据位在代码里怎么读**

- `LIS_CRITICAL_TOKEN` ← `critval/console/app.py:144` — `resolved_token = token if token is not None else (Config.get_env("LIS_CRITICAL_T`

### ⑤ LIS / 检验科信息系统（HL7 v2 ORU^R01） · LISTEN

| 项 | 内容 |
|----|------|
| 方向 | 入站 |
| 对端系统 | LIS / 检验科信息系统（HL7 v2 ORU^R01） |
| 协议 / 方法 | MLLP over TCP（帧 \x0b…\x1c\r） / LISTEN |
| 目标地址模板 | `{LIS_MLLP_BIND}（主机:端口）` |
| 凭据位（环境变量） | `LIS_MLLP_BIND` |
| 口径要点 | 一帧完整只看结束符；粘包逐帧切、拆包留缓冲、空帧与帧前垃圾都留痕。 |
| 本轮是否接主路径 | 是 |

**字段规范**

| 字段 | 方向 | 必填 | 说明 |
|------|------|------|------|
| `MSH-9` | 请求 | 是 | 消息类型（ORU^R01） |
| `MSH-10` | 请求 | 是 | 消息控制号 —— 事件幂等键的一部分 |
| `OBR-3` | 请求 | 是 | 检验申请号（承载标本号） |
| `OBX-3` | 请求 | 是 | 观察标识（检验项目码） |
| `OBX-5` | 请求 | 是 | 观察值 |
| `OBX-6` | 请求 | 是 | 单位 |
| `OBX-7` | 请求 | 否 | 参考范围（参与分档） |
| `OBX-8` | 请求 | 否 | 异常标志（只作交叉校验，不作判定权威） |
| `OBX-11 / OBR-25` | 请求 | 否 | 结果状态（C = 修正） |
| `OBX-14` | 请求 | 是 | 观察日期时间 |
| `PV1-7` | 请求 | 否 | 患者所在科室 |
| `PID-3` | 请求 | 否 | 仅透传到事件标识层（落不可逆短哈希） |

**代码证据**

- `critval/hl7/mllp.py:56` — `START_BLOCK = b"\x0b"`
- `critval/hl7/mllp.py:58` — `END_BLOCK = b"\x1c\r"`
- `critval/hl7/mllp.py:129` — `class MllpFramer:`
- `critval/hl7/mllp.py:328` — `class MllpListener:`

**凭据位在代码里怎么读**

- `LIS_MLLP_BIND` ← `critval/daemon.py:151` — `mllp_bind=Config.get_env("LIS_MLLP_BIND") or "",`

### ⑥ LIS（危急值 HTTP 推送回调） · POST

| 项 | 内容 |
|----|------|
| 方向 | 入站 |
| 对端系统 | LIS（危急值 HTTP 推送回调） |
| 协议 / 方法 | HTTP（REST） / POST |
| 目标地址模板 | `{CRITVAL_LISTEN_BASE_URL}/api/v1/critical-value-events` |
| 凭据位（环境变量） | `CRITVAL_LISTEN_BASE_URL`、`LIS_CRITICAL_TOKEN` |
| 口径要点 | 与 MLLP 共用同一套规范化函数 —— 同一件事走两条通道得到同一个事件 ID。 |
| 本轮是否接主路径 | 是 |

**字段规范**

| 字段 | 方向 | 必填 | 说明 |
|------|------|------|------|
| `event_id` | 请求 | 是 | 事件标识 —— 进幂等键 |
| `item_code` | 请求 | 是 | 检验项目码 |
| `value` | 请求 | 是 | 观察值（非数值不换算、不猜数） |
| `unit` | 请求 | 否 | 单位 |
| `critical_level` | 请求 | 否 | 危急级别 |
| `raised_at` | 请求 | 是 | 结果时刻 |
| `dept_code` | 请求 | 否 | 科室 |
| `Authorization` | 请求 | 是 | Bearer $LIS_CRITICAL_TOKEN（常量时间比较） |

**代码证据**

- `critval/console/inbound.py:60` — `"verify_bearer",`
- `critval/console/inbound.py:64` — `INBOUND_PATH = "/api/v1/critical-value-events"`
- `critval/console/inbound.py:124` — `def verify_bearer(headers: Mapping[str, str], expected_token: str) -> bool:`
- `critval/console/inbound.py:460` — `if not verify_bearer(request.headers, self.token):`
- `critval/console/inbound.py:544` — `if not verify_bearer(request.headers, self.console_token):`
- `critval/console/app.py:219` — `class _InboundHandler(BaseHTTPRequestHandler):`

**凭据位在代码里怎么读**

- `CRITVAL_LISTEN_BASE_URL` ← `critval/daemon.py:142` — `listen_base = Config.get_env("CRITVAL_LISTEN_BASE_URL") or ""`
- `LIS_CRITICAL_TOKEN` ← `critval/console/app.py:144` — `resolved_token = token if token is not None else (Config.get_env("LIS_CRITICAL_T`

## 二、凭据位总表

| 环境变量 | 归属接缝 | 性质 | 代码里怎么读 |
|----------|----------|------|--------------|
| `NURSE_ROSTER_BASE_URL` | roster_read | 地址 | 环境变量（`Config.get_env`） |
| `ROSTER_TOKEN` | roster_read | 密钥 | 环境变量（`Config.get_env`，缺则拒绝发请求） |
| `CRITVAL_BASE_URL` | registry_writeback | 地址 | 环境变量（`Config.get_env`） |
| `CRITVAL_TOKEN` | registry_writeback | 密钥 | 环境变量（`Config.get_env`，缺则拒绝发请求） |
| `CRITVAL_WECOM_WEBHOOK_URL` | wecom_push | 密钥 | 环境变量（`Config.get_env`，缺则拒绝发请求） |
| `LIS_BASE_URL` | lis_rest_callback | 地址 | 环境变量（`Config.get_env`） |
| `LIS_CRITICAL_TOKEN` | lis_rest_callback | 密钥 | 环境变量（`Config.get_env`，缺则拒绝发请求） |
| `LIS_MLLP_BIND` | mllp_inbound | 地址 | 环境变量（`Config.get_env`） |
| `CRITVAL_LISTEN_BASE_URL` | http_push_inbound | 地址 | 环境变量（`Config.get_env`） |
| `LIS_CRITICAL_TOKEN` | http_push_inbound | 密钥 | 环境变量（`Config.get_env`，缺则拒绝发请求） |

**缺凭据位的行为**：出站客户端在**发请求之前**就报错（不拿空口令去撞对端，也不拿本地替身顶上去）；入站推送端点未配置共享凭据时**一律拒绝**。

## 三、替身边界（测试数据层 ≠ 最终集成面）

| 替身文件 | 替代的对端 | 协议 |
|----------|------------|------|
| `tests/fixtures/lis_rest_double.py` | LIS 侧危急值登记接口 | HTTP POST |
| `tests/fixtures/registry_double.py` | 院内危急值登记 / 护理执行平台 | HTTP GET/POST |
| `tests/fixtures/roster_double.py` | 排班 / 值班名单系统 | HTTP GET |
| `tests/fixtures/wecom_double.py` | 企业微信值班群机器人 | HTTP POST |

替身替代的**只是对端系统本身**，不是协议：产品代码走的就是院内真实接口的报文格式（真实 URL、真实 `Authorization: Bearer` 头、真实 JSON 体、真实 `errcode` 判读）。替身文件全部位于 `tests/fixtures/` 下，**产品代码不 import 它们**。

## 四、断言结果

| 断言 | 口径 | 结果 | 说明 |
|------|------|------|------|
| A | 凭据位都登记进 `Config.CONFIG_KEYS` | **通过** | 6 条接缝的凭据位全部登记在 Config.CONFIG_KEYS |
| B | 密钥位以 `Config.get_env` 读，代码内无默认值 | **通过** | 全部密钥位经 Config.get_env 读取，代码内无默认值 |
| C | 产品代码无凭据字面量 | **通过** | 产品代码内未出现凭据字面量 |
| D | 出站默认地址不指向本机 / 示例主机 | **通过** | 出站默认地址均来自环境变量，无本机 / 示例地址 |
| E | 协议替身只在 `tests/fixtures/`，产品代码零引用 | **通过** | 替身实现全部位于 tests/fixtures/，产品代码零引用 |
| F | 每条接缝在代码里都有出处 | **通过** | 6 条接缝均能在产品代码里指到出处 |

## 五、本轮未实连的端点（诚实标注）

- **院内真实系统未实连**：本轮跑在**协议替身**上（见上一节），产品代码里的出站客户端（真实 HTTP + 真实 `Authorization: Bearer` + 真实字段规范）已按 spec 落盘，但**没有对着真实登记系统 / 排班系统 / 企微群发过请求** —— 院内地址与凭据到位后，把对应环境变量指过去即可，产品代码一行不改。
- **未接主路径的条件接缝**：`lis_rest_callback`（LIS 侧 REST 回调）按院内厂商约定选用，本轮未接入守护进程主循环；`LIS_BASE_URL` 留空即不启用。
- **入站两条通道**：`mllp_inbound` 与 `http_push_inbound` 共用同一套规范化函数；本轮验证到**协议与字段级**（真实 socket / 真实 HTTP 报文），未对着院内 LIS 实发。

---

本清单只描述**集成面**：对端是谁、凭据从哪来、没配会怎样。产品能力的完整口径见项目 `spec.md` 与 `README.md`。
