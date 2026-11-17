# 处置链回放样例 —— 这条停在哪一步、为什么

> 本文件是 `critval trace` 的**真实输出**（不是示意图）：处置链跑的时候逐节点记一条
> 轨迹并随事件落库，回放读的是**落库的轨迹**，不是现场重跑 —— 事后复核看到的与
> 当初发生的完全一致。

生成命令：

```bash
python3 scripts/flow_demo.py --db /tmp/trace.db --break card_built
python3 -m critval.cli trace --event evt-flow-0001 --db /tmp/trace.db
```

---

## 一、注入节点异常后的回放

```text
事件 evt-flow-0001 · 处置链轨迹（节点级） · 当前状态 transferred_to_human
────────────────────────────────────────────────────────────────────
  1. received            成功        0.4ms
      结论 · 阶段 received
      输入 · 事件=evt-flow-0001 · 项目=718-7 · 结果=25 · 单位=g/L · 科室=ICU · 时刻=2026-01-02 08:30:00+00:00
      产出 · stage=received
  2. judged              成功        5.2ms
      结论 · 判定结论 critical
      输入 · 事件=evt-flow-0001 · 项目=718-7 · 结果=25 · 单位=g/L · 科室=ICU · 时刻=2026-01-02 08:30:00+00:00 · 27 条阈值行
      产出 · decision=critical · item_code=718-7 · rule_id=示例危急值阈值表.xlsx · 第 2 行 · 示例 v1.0
  3. retrieved           成功        0.0ms
      结论 · retrieve 环节完成
      输入 · 事件=evt-flow-0001 · 项目=718-7 · 结果=25 · 单位=g/L · 科室=ICU · 时刻=2026-01-02 08:30:00+00:00
      产出 · clause_found=True · citations=1
  4. card_built          失败        0.3ms
      结论 · 节点异常，已回落转人工
      输入 · 事件=evt-flow-0001 · 项目=718-7 · 结果=25 · 单位=g/L · 科室=ICU · 时刻=2026-01-02 08:30:00+00:00 · 结论=critical · 项目=718-7 · 规则=示例危急值阈值表.xlsx · 第 2 行 · 示例 v1.0 · 理由=判定为危急值：低于低危急值 30 g/L（依据 示例危急值阈值表.xlsx · 第 2 行 ·
      报错 · RuntimeError: 演示注入：节点 card_built 处理失败
  5. transfer_to_human   回落转人工    0.1ms
      结论 · 已回落转人工：事件留在台账里等值班人接手
      输入 · 事件=evt-flow-0001 · 项目=718-7 · 结果=25 · 单位=g/L · 科室=ICU · 时刻=2026-01-02 08:30:00+00:00
      产出 · transferred=True · event_id=evt-flow-0001 · error=RuntimeError: 演示注入：节点 card_built 处理失败 · next_action=请值班人接手：核对依据与时限，处置后手工回写登记系统
      报错 · RuntimeError: 演示注入：节点 card_built 处理失败
  6. notified            跳过            —
      结论 · 未执行（链在第 4 步 card_built 停下了）
  7. acknowledged        跳过            —
      结论 · 未执行（链在第 4 步 card_built 停下了）
  8. registered          跳过            —
      结论 · 未执行（链在第 4 步 card_built 停下了）
  9. archived            跳过            —
      结论 · 未执行（链在第 4 步 card_built 停下了）
────────────────────────────────────────────────────────────────────
停在哪一步：card_built（第 4 步）
为什么      ：RuntimeError: 演示注入：节点 card_built 处理失败
这一环能不能重跑：不能（重跑还是同一个结果）
兜底出口    ：已回落转人工（事件留在台账里等人接）
```

**读法**：

| 行 | 回答什么 |
|----|---------|
| 第 4 行「失败」 | 停在了**出卡**这一环，不是推送、不是回写 |
| 「报错」行 | 失败的原始原因，一字不改 —— 不转写成「系统异常」这种看不懂的话 |
| 第 5 行「回落转人工」 | 兜底出口接住了，事件没丢，留在台账里等人接 |
| 第 6–9 行「跳过」 | 后面四环**为什么没跑**（链在第 4 步停下了），不留空白让人猜 |
| 最后四行 | 停在哪一步 / 为什么 / **这一环能不能重跑** / 兜底用没用上 |

**「能不能重跑」是按节点契约说的**，不是猜的：推送与回写是幂等的 → 重跑安全；
判定与召回是纯计算 → 重跑还是同一个答案，重试没有意义。

---

## 二、机器可读版（值班台接这条接口时用同一份数据）

```bash
python3 -m critval.cli trace --event evt-flow-0001 --db /tmp/trace.db --json
```

```json
{
  "event_id": "evt-flow-0001",
  "status": "transferred_to_human",
  "trace_count": 9,
  "stopped_at": "card_built",
  "fallback_used": true,
  "traces": [
    {
      "seq": 1,
      "node": "received",
      "component": "received",
      "status": "success",
      "status_label": "成功",
      "conclusion": "阶段 received",
      "elapsed_ms": 0.4,
      "input_summary": "事件=evt-flow-0001 · 项目=718-7 · 结果=25 · 单位=g/L · 科室=ICU · 时刻=2026-01-02 08:30:00+00:00",
      "output_summary": "stage=received",
      "error": "",
      "retryable": false,
      "at": "2026-10-30T12:20:11+00:00"
    }
  ]
}
```

（`traces` 数组此处只列第一环作形状示意，实际输出为 9 条完整轨迹。）

---

## 三、脱敏口径

轨迹随事件**长期留在台账里**，是最容易漏掉患者明细的地方，所以摘要过三道闸门：

| 闸门 | 口径 |
|------|------|
| 按**字段名**挡 | 患者标识层字段名（`patient_ref` / `patient_name` / `id_card` / `admission_no` / `住院号` …）不进摘要 |
| 按**标签**挡 | 文本里出现「姓名 / 身份证 / 住院号 / 病案号 / 住院流水号 / PID-3」等标签 → **整段**脱敏（不是逐个替换 —— 局部替换容易漏下一个） |
| 落库再挡一道 | `LedgerStore.record_flow_trace()` 写库前对每段文字再脱敏一次 |

复核方式（轨迹全文搜患者标识，应为「无」）：

```bash
python3 -c "
import sys; sys.path.insert(0, '.')
from critval.ledger.store import LedgerStore
s = LedgerStore('/tmp/trace.db')
blob = ' '.join(str(r) for r in s.flow_traces_of('evt-flow-0001'))
bad = [k for k in ('姓名','身份证','住院号','住院流水号','PID-3','00012345') if k in blob]
print('命中患者标识：', bad or '无')
s.close()"
```

---

## 四、与 `ledger-trace` 的分工

| 命令 | 回答什么 | 数据来源 |
|------|---------|---------|
| `critval ledger-trace --event <id>` | **四问**：按哪条依据 / 通知了谁 / 几分钟内 / 谁确认的 | 已生效事实（事件行 + 引用表 + 通知记录 + 审计流水） |
| `critval trace --event <id>` | **过程**：走过哪些环节 / 停在哪一步 / 为什么停 / 这一环能不能重跑 | 节点级轨迹（`flow_traces` 表） |

两者可交叉使用：先用 `ledger-trace` 确认「这条按哪条依据、通知了谁」，再用 `trace`
确认「它是怎么一步步走到这一步的」。
