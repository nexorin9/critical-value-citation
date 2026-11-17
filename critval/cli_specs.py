"""CLI 子命令参数表 —— 由子命令注册表统一管理（``critval/cli_registry.py``）。

每个子命令的「有哪些参数」登记在这里，解析器不再散落成一长串
``if name == ...`` 分支。新增子命令时：在 ``critval/cli.py`` 用
``@register_command`` 登记名字与帮助文本，在本表登记参数。

``--json`` 由框架统一添加（见 ``cli_registry.CommandRegistry.build_parser``），
本表不重复登记。``parse-queue`` 的 list / confirm / ignore 是嵌套动作，
由 ``NESTED_ARG_BUILDERS`` 单独接线。

材料默认路径按**包位置**解析 —— 从任何目录跑 CLI 都指向同一份样例材料。
"""

from __future__ import annotations

import argparse
from pathlib import Path

from critval.cli_registry import ArgSpec

#: 仓库根目录（按包位置解析，从任何目录跑都对）。
PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: 样例材料与语料的默认位置。
DEFAULT_THRESHOLD = str(PROJECT_ROOT / "samples" / "threshold" / "示例危急值阈值表.xlsx")
DEFAULT_POLICY = str(PROJECT_ROOT / "samples" / "policy" / "示例危急值报告制度.docx")
DEFAULT_FRAMES = str(PROJECT_ROOT / "data" / "synth" / "oru")

#: 子命令名 → 参数声明列表。
COMMAND_ARG_SPECS: dict[str, tuple[ArgSpec, ...]] = {
    'rules-summary': (
        ArgSpec(('--threshold',), {'help': '院内阈值表 xlsx 路径（编译成规则集的材料来源）', 'required': True}),
    ),
    'rules-reload': (
        ArgSpec(('--threshold',), {'help': '院内阈值表 xlsx 路径（编译成规则集的材料来源）', 'required': True}),
    ),
    'config-check': (
        ArgSpec(('--no-persist',), {'help': '不把本次配置快照落盘到 reports/config_snapshots/', 'dest': 'persist', 'action': 'store_false'}),
    ),
    'export': (
        ArgSpec(('--event',), {'help': '事件 ID（取该事件最近一份处置卡快照）', 'default': ''}),
        ArgSpec(('--scope',), {'help': '复核报表口径：daily=日报（当天）/ weekly=周报（截至 --date 的 7 天）', 'choices': ['', 'daily', 'weekly'], 'default': ''}),
        ArgSpec(('--date',), {'help': '观察日期 YYYY-MM-DD（默认今天）—— 报表按观察日期切，不按导出时刻', 'default': ''}),
        ArgSpec(('--db',), {'help': 'SQLite 台账路径（默认取 CRITVAL_DB_PATH）'}),
        ArgSpec(('--out',), {'help': '导出文件路径（csv=明细 CSV；xlsx=工作簿路径）', 'default': ''}),
        ArgSpec(('--critical-only',), {'help': '只统计判定为危急的事件（默认全量入表）', 'action': 'store_true'}),
        ArgSpec(('--format',), {'help': '输出渠道：text=离线纯文本 / markdown=企微推送原文 / html=值班台片段；导出报表时 text=人读摘要 / csv=Excel 直开 / xlsx=带列说明的工作簿', 'choices': ['text', 'markdown', 'html', 'csv', 'xlsx'], 'default': 'text'}),
        ArgSpec(('--snapshot-dir',), {'help': '处置卡快照目录（默认 data/card_snapshots）', 'default': 'data/card_snapshots'}),
        ArgSpec(('--verify',), {'help': '输出前先校验快照完整性（内容指纹 + 各渠道正文哈希）', 'action': 'store_true'}),
    ),
    'reconcile': (
        ArgSpec(('--db',), {'help': 'SQLite 台账路径（默认取 CRITVAL_DB_PATH）'}),
        ArgSpec(('--out',), {'help': '差异清单 CSV 落盘路径（默认 reports/reconcile_diff.csv）', 'default': 'reports/reconcile_diff.csv'}),
        ArgSpec(('--event',), {'help': '只对账这些事件（事件 ID，逗号分隔；默认全量在范围内的事件）', 'default': ''}),
        ArgSpec(('--only',), {'help': '只看某一类差异（逗号分隔）：missing_local / missing_remote / status_mismatch / field_mismatch', 'default': ''}),
        ArgSpec(('--since',), {'help': '只看观察时刻在该 ISO 文本之后的事件', 'default': ''}),
        ArgSpec(('--limit',), {'help': '最多对账多少条本地事件（0 = 不限）', 'type': int, 'default': 0}),
        ArgSpec(('--resolve',), {'help': '显式执行建议动作（补写 / 重放 / 标记人工处理）；不加则只读出清单', 'action': 'store_true'}),
        ArgSpec(('--actor',), {'help': '处理人角色（如「检验科组长」）—— --resolve 时必填', 'default': ''}),
    ),
    'writeback-retry': (
        ArgSpec(('--db',), {'help': 'SQLite 台账路径（默认取 CRITVAL_DB_PATH）'}),
        ArgSpec(('--event',), {'help': '显式补写这些事件（事件 ID，逗号分隔）', 'default': ''}),
        ArgSpec(('--all',), {'help': '补写待写队列里的全部事件', 'action': 'store_true'}),
        ArgSpec(('--list',), {'help': '只看待写队列（失败分类 / 次数 / 下一步），不补写', 'action': 'store_true'}),
        ArgSpec(('--actor',), {'help': '处理人角色（如「检验科组长」）—— 记进审计', 'default': ''}),
        ArgSpec(('--limit',), {'help': '最多处理多少条待写事件（默认 100）', 'type': int, 'default': 100}),
        ArgSpec(('--max-attempts',), {'help': '单条事件内的自动重试次数（只对可重试分类生效，默认 1 = 不自动重试）', 'type': int, 'default': 1}),
        ArgSpec(('--no-probe-remote',), {'help': '补写前不探针核对登记系统那条（默认探针）', 'action': 'store_true'}),
    ),
    'version-check': (
        ArgSpec(('--threshold',), {'help': '院内阈值表 xlsx 路径（可能含多版本共存）', 'required': True}),
        ArgSpec(('--at',), {'help': '观察时刻 YYYY-MM-DD（默认今天）—— 判断哪一版在该时刻生效'}),
        ArgSpec(('--bindings',), {'help': '版本绑定表 JSON 路径（项目码 → 阈值表版本 + 制度版本）'}),
    ),
    'impact-check': (
        ArgSpec(('--old',), {'help': '改版前的阈值表 xlsx 路径', 'required': True}),
        ArgSpec(('--new',), {'help': '改版后的阈值表 xlsx 路径', 'required': True}),
        ArgSpec(('--db',), {'help': 'SQLite 台账路径（默认取 CRITVAL_DB_PATH；台账不存在则只比口径不重算）'}),
        ArgSpec(('--from',), {'help': '重算窗口起 YYYY-MM-DD', 'dest': 'window_from', 'default': ''}),
        ArgSpec(('--to',), {'help': '重算窗口止 YYYY-MM-DD', 'dest': 'window_to', 'default': ''}),
        ArgSpec(('--dept',), {'help': '只重算某个科室的事件（科室码）', 'default': ''}),
        ArgSpec(('--at',), {'help': '口径比对的基准时刻 YYYY-MM-DD（默认今天）'}),
        ArgSpec(('--limit',), {'help': '重算事件条数上限（0 = 不限）', 'type': int, 'default': 0}),
        ArgSpec(('--out',), {'help': '影响面报告 Markdown 落盘路径', 'default': 'reports/threshold_impact.md'}),
        ArgSpec(('--csv',), {'help': '翻转事件清单 CSV 落盘路径', 'default': 'reports/threshold_impact_flipped.csv'}),
    ),
    'console-serve': (
        ArgSpec(('--host',), {'help': '监听地址（默认 127.0.0.1）', 'default': '127.0.0.1'}),
        ArgSpec(('--port',), {'help': '监听端口（默认 8760）', 'type': int, 'default': 8760}),
        ArgSpec(('--db',), {'help': 'SQLite 台账路径（默认取 CRITVAL_DB_PATH）'}),
        ArgSpec(('--threshold',), {'help': '院内阈值表 xlsx 路径（默认取 CRITVAL_THRESHOLD_PATH）'}),
    ),
    # parse-queue 的参数在嵌套动作里接线（见 NESTED_ARG_BUILDERS），
    # 顶层只有框架统一加的 --json。
    'parse-queue': (
    ),
    'policy-summary': (
        ArgSpec(('--policy',), {'help': '院内危急值报告制度文件路径（docx / pdf）', 'required': True}),
    ),
    'policy-diff': (
        ArgSpec(('--old',), {'help': '改版前的制度文件路径', 'required': True}),
        ArgSpec(('--new',), {'help': '改版后的制度文件路径', 'required': True}),
        ArgSpec(('--csv',), {'help': '把受影响条款清单写成 CSV（默认不落盘）'}),
    ),
    'bind': (
        ArgSpec(('--bindings',), {'help': '版本绑定表 JSON 路径（读写同一份）', 'default': 'samples/bindings/版本绑定表.json'}),
        ArgSpec(('--item',), {'help': '检验项目码（写一条绑定）', 'default': ''}),
        ArgSpec(('--threshold-version',), {'help': '该项目的阈值表版本', 'default': ''}),
        ArgSpec(('--policy-version',), {'help': '该项目的制度版本', 'default': ''}),
        ArgSpec(('--note',), {'help': '备注（如「检验科 2026-02 确认」）', 'default': ''}),
        ArgSpec(('--list',), {'help': '只看当前绑定表，不改任何东西', 'action': 'store_true'}),
        ArgSpec(('--derive',), {'help': '按观察时刻从阈值表与制度推导绑定表', 'action': 'store_true'}),
        ArgSpec(('--threshold',), {'help': '推导用的阈值表 xlsx 路径（配合 --derive）', 'default': ''}),
        ArgSpec(('--policy',), {'help': '推导用的制度文件路径（配合 --derive）', 'default': ''}),
        ArgSpec(('--at',), {'help': '推导用的观察时刻 YYYY-MM-DD（默认今天）', 'default': ''}),
    ),
    'kb-watch': (
        ArgSpec(('--dir',), {'help': '材料投递目录', 'required': True}),
        ArgSpec(('--apply',), {'help': '真的执行重建（默认只报事件，不改任何东西）', 'action': 'store_true'}),
        ArgSpec(('--queue',), {'help': '确认队列 SQLite 路径（默认 data/parse_queue.db）', 'default': 'data/parse_queue.db'}),
        ArgSpec(('--catalog',), {'help': '材料台账路径（默认 data/material_catalog.json）', 'default': 'data/material_catalog.json'}),
        ArgSpec(('--report-dir',), {'help': '解析报告目录（默认 reports/parse_reports）', 'default': 'reports/parse_reports'}),
    ),
    'recall-merge': (
        ArgSpec(('--threshold',), {'help': '院内阈值表 xlsx 路径（结构化路的精确查表材料）', 'required': True}),
        ArgSpec(('--policy',), {'help': '院内危急值报告制度 docx 路径（结构化路按条款点名的项目码命中）', 'required': True}),
        ArgSpec(('--query',), {'help': '词法路的自然语言查询（默认「危急值 上报时限」）', 'default': '危急值 上报时限'}),
        ArgSpec(('--item-code',), {'help': '结构化路的检验项目码（默认 718-7 血红蛋白）', 'default': '718-7'}),
        ArgSpec(('--at',), {'help': '观察时刻 YYYY-MM-DD（默认不限，按生效期硬过滤失效版本）'}),
        ArgSpec(('--top-k',), {'help': '词法路候选池大小（默认 20）', 'type': int, 'default': 20}),
        ArgSpec(('--structured-weight',), {'help': '结构化精确路权重（与词法路权重之和须为 1，默认 0.5）', 'type': float, 'default': 0.5}),
        ArgSpec(('--lexical-weight',), {'help': '词法 BM25 路权重（与结构化路权重之和须为 1，默认 0.5）', 'type': float, 'default': 0.5}),
        ArgSpec(('--raw-scores',), {'help': '跳过归一化直接融合（自检用：应被归一化契约拦下并报出违规路与分值）', 'action': 'store_true'}),
    ),
    'recall': (
        ArgSpec(('--threshold',), {'help': '院内阈值表 xlsx 路径（结构化精确路的查表材料）', 'required': True}),
        ArgSpec(('--policy',), {'help': '院内危急值报告制度 docx 路径（制度条款材料）', 'required': True}),
        ArgSpec(('--query',), {'help': '词法路的自然语言查询（默认「危急值 上报时限」）', 'default': '危急值 上报时限'}),
        ArgSpec(('--item-code',), {'help': '结构化路的检验项目码（默认 718-7 血红蛋白）', 'default': '718-7'}),
        ArgSpec(('--at',), {'help': '观察时刻 YYYY-MM-DD（默认不限，按生效期硬过滤失效版本）'}),
        ArgSpec(('--bindings',), {'help': '版本绑定表 JSON 路径（项目码 → 阈值表版本 + 制度版本）'}),
        ArgSpec(('--top-k',), {'help': '候选池大小（默认 64）', 'type': int, 'default': 64}),
        ArgSpec(('--top-n',), {'help': '返回条数（默认 8）', 'type': int, 'default': 8}),
        ArgSpec(('--similarity-threshold',), {'help': '综合分硬闸门：低于该分数的依据一律截断（默认 0.2）', 'type': float, 'default': 0.2}),
        ArgSpec(('--keywords-weight',), {'help': 'BM25 词法路权重（结构化精确路权重 = 1 - 本值，默认 0.5）', 'type': float, 'default': 0.5}),
        ArgSpec(('--rerank-id',), {'help': '规则重排档位（default / field_first / coverage_first，默认 default）', 'default': ''}),
    ),
    'recall-conflict': (
        ArgSpec(('--threshold',), {'help': '院内阈值表 xlsx 路径（结构化精确路的查表材料）', 'required': True}),
        ArgSpec(('--policy',), {'help': '院内危急值报告制度 docx 路径（制度条款材料）', 'required': True}),
        ArgSpec(('--query',), {'help': '词法路的自然语言查询（默认「危急值 上报时限」）', 'default': '危急值 上报时限'}),
        ArgSpec(('--item-code',), {'help': '结构化路的检验项目码（默认 718-7 血红蛋白）', 'default': '718-7'}),
        ArgSpec(('--at',), {'help': '观察时刻 YYYY-MM-DD（默认不限，按生效期硬过滤失效版本）'}),
        ArgSpec(('--bindings',), {'help': '版本绑定表 JSON 路径（项目码 → 阈值表版本 + 制度版本）'}),
        ArgSpec(('--top-k',), {'help': '候选池大小（默认 64）', 'type': int, 'default': 64}),
        ArgSpec(('--top-n',), {'help': '返回条数（默认 8）', 'type': int, 'default': 8}),
        ArgSpec(('--similarity-threshold',), {'help': '综合分硬闸门：低于该分数的依据一律截断（默认 0.2）', 'type': float, 'default': 0.2}),
        ArgSpec(('--keywords-weight',), {'help': 'BM25 词法路权重（结构化精确路权重 = 1 - 本值，默认 0.5）', 'type': float, 'default': 0.5}),
    ),
    'recall-fallback': (
        ArgSpec(('--threshold',), {'help': '院内阈值表 xlsx 路径（别名表的项目码 ↔ 项目名来源）', 'required': True}),
        ArgSpec(('--policy',), {'help': '院内危急值报告制度 docx 路径（制度条款材料）', 'required': True}),
        ArgSpec(('--query',), {'help': '词法路的自然语言查询（默认「危急值 上报时限」）', 'default': '危急值 上报时限'}),
        ArgSpec(('--item-code',), {'help': '结构化路的检验项目码（章节级兜底按它定位制度章节）', 'default': '718-7'}),
        ArgSpec(('--at',), {'help': '观察时刻 YYYY-MM-DD（默认不限，按生效期硬过滤失效版本）'}),
        ArgSpec(('--bindings',), {'help': '版本绑定表 JSON 路径（项目码 → 阈值表版本 + 制度版本）'}),
        ArgSpec(('--top-k',), {'help': '候选池大小（默认 64）', 'type': int, 'default': 64}),
        ArgSpec(('--top-n',), {'help': '返回条数（默认 8）', 'type': int, 'default': 8}),
        ArgSpec(('--similarity-threshold',), {'help': '综合分硬闸门：低于该分数的依据一律截断（默认 0.2）', 'type': float, 'default': 0.2}),
        ArgSpec(('--keywords-weight',), {'help': 'BM25 词法路权重（结构化精确路权重 = 1 - 本值，默认 0.5）', 'type': float, 'default': 0.5}),
        ArgSpec(('--rerank-id',), {'help': '规则重排档位（default / field_first / coverage_first，默认 default）', 'default': ''}),
    ),
    'eval-run': (
        ArgSpec(('--eval-set',), {'help': '评测集路径（由 scripts/build_eval_set.py 生成）', 'default': 'data/eval/queries.jsonl'}),
        ArgSpec(('--depth',), {'help': '检索深度（参与命中率判定）', 'type': int, 'default': 10}),
        ArgSpec(('--top-n',), {'help': '前 N 名（参与 Top-N 命中率判定）', 'type': int, 'default': 5}),
        ArgSpec(('--structured-weight',), {'help': '结构化精确路权重', 'type': float, 'default': 0.5}),
        ArgSpec(('--lexical-weight',), {'help': '词法 BM25 路权重', 'type': float, 'default': 0.5}),
        ArgSpec(('--candidate-depth',), {'help': '词法路候选深度', 'type': int, 'default': 50}),
        ArgSpec(('--citation-mode',), {'help': '出处齐备口径：kind_aware=按材料类型判定 / strict=三件套缺一不可', 'choices': ['kind_aware', 'strict'], 'default': 'kind_aware'}),
        ArgSpec(('--out',), {'help': 'Markdown 报告落盘路径', 'default': 'reports/eval_report.md'}),
        ArgSpec(('--json-out',), {'help': 'JSON 报告落盘路径', 'default': 'reports/eval_report.json'}),
    ),
    'recall-baseline': (
        ArgSpec(('--eval-set',), {'help': '评测集路径（由 scripts/build_eval_set.py 生成）', 'default': 'data/eval/queries.jsonl'}),
        ArgSpec(('--depth',), {'help': '检索深度（参与命中率判定）', 'type': int, 'default': 10}),
        ArgSpec(('--top-n',), {'help': '前 N 名（参与 Top-N 命中率判定）', 'type': int, 'default': 5}),
        ArgSpec(('--similarity-threshold',), {'help': '融合侧综合分硬闸门（基线不吃这一项）', 'type': float, 'default': 0.2}),
        ArgSpec(('--structured-weight',), {'help': '结构化精确路权重', 'type': float, 'default': 0.5}),
        ArgSpec(('--lexical-weight',), {'help': 'BM25 词法路权重', 'type': float, 'default': 0.5}),
        ArgSpec(('--candidate-depth',), {'help': '两侧词法路候选深度', 'type': int, 'default': 50}),
        ArgSpec(('--out',), {'help': 'Markdown 对比表落盘路径', 'default': 'docs/examples/recall_baseline_compare.md'}),
        ArgSpec(('--csv-out',), {'help': 'CSV 对比表落盘路径', 'default': 'reports/recall_baseline_compare.csv'}),
    ),
    'judge-baseline': (
        ArgSpec(('--threshold',), {'help': '院内阈值表 xlsx 路径（样例集由它确定性生成）', 'default': 'samples/threshold/示例危急值阈值表.xlsx'}),
        ArgSpec(('--out',), {'help': 'Markdown 对比表落盘路径', 'default': 'docs/examples/judge_baseline_compare.md'}),
        ArgSpec(('--csv-out',), {'help': 'CSV 对比表落盘路径', 'default': 'reports/judge_baseline_compare.csv'}),
    ),
    'judge-replay': (
        ArgSpec(('--event',), {'help': '事件 ID（回放这条事件的判定链路）', 'required': True}),
        ArgSpec(('--db',), {'help': '台账 SQLite 路径（默认取 CRITVAL_DB_PATH）', 'default': ''}),
        ArgSpec(('--threshold',), {'help': '院内阈值表 xlsx 路径（复算用的材料）', 'default': 'samples/threshold/示例危急值阈值表.xlsx'}),
    ),
    'ledger-trace': (
        ArgSpec(('--event',), {'help': '事件 ID（回答四问）', 'required': True}),
        ArgSpec(('--db',), {'help': 'SQLite 台账路径（默认取 CRITVAL_DB_PATH）'}),
    ),
    'trace': (
        ArgSpec(('--event',), {'help': '事件 ID（回放这条事件的处置链）', 'required': True}),
        ArgSpec(('--db',), {'help': 'SQLite 台账路径（默认取 CRITVAL_DB_PATH）'}),
    ),
    'doctor': (
        ArgSpec(('--db',), {'help': 'SQLite 台账路径（默认取 CRITVAL_DB_PATH）'}),
        ArgSpec(('--threshold',), {'help': '院内阈值表 xlsx 路径（可选；给了就装载并报当前规则快照 ID）'}),
    ),
    'audit-stats': (
        ArgSpec(('--db',), {'help': 'SQLite 台账路径（默认取 CRITVAL_DB_PATH）'}),
        ArgSpec(('--since',), {'help': '起始时间（含）ISO 文本', 'default': ''}),
        ArgSpec(('--until',), {'help': '截止时间（含）ISO 文本', 'default': ''}),
        ArgSpec(('--dept',), {'help': '按科室过滤', 'default': ''}),
        ArgSpec(('--export',), {'help': '把流水导出成 CSV（给定路径）', 'default': ''}),
    ),
    'notify-queue': (
        ArgSpec(('--db',), {'help': 'SQLite 台账路径（默认取 CRITVAL_DB_PATH）'}),
        ArgSpec(('--queue',), {'help': '本机通知队列文件路径（默认取 CRITVAL_LOCAL_NOTIFICATION_PATH）'}),
        ArgSpec(('--event',), {'help': '只看某个事件（留空看全部：队列 / 死信 / 投递记录 / 投递轮次）', 'default': ''}),
    ),
    'audit-verify': (
        ArgSpec(('--db',), {'help': 'SQLite 台账路径（默认取 CRITVAL_DB_PATH）'}),
        ArgSpec(('--export',), {'help': '把流水连链哈希一起导出成 CSV（给定路径，便于异地复核）', 'default': ''}),
    ),
    'roster-resolve': (
        ArgSpec(('--roster',), {'help': '排班名单 JSON 文件路径（排班系统响应原样）', 'required': True}),
        ArgSpec(('--dept',), {'help': '科室代码（写进结果，便于归档）', 'default': ''}),
        ArgSpec(('--at',), {'help': '判定时刻 YYYY-MM-DD HH:MM（默认现在）—— 决定落在哪个班次窗口', 'default': ''}),
        ArgSpec(('--origin',), {'help': '名单来源：live=实时读到 / cache=带时间戳的缓存（会复核有效期）', 'choices': ['live', 'cache'], 'default': 'live'}),
        ArgSpec(('--as-of',), {'help': '名单取数时刻（--origin cache 时必填，用于复核缓存是否过期）', 'default': ''}),
        ArgSpec(('--cache-ttl',), {'help': '缓存有效期（秒，默认 900）', 'type': int, 'default': 900}),
    ),
    'ingest': (
        ArgSpec(('--threshold',), {'help': '阈值表 xlsx 路径（解析出解析报告）', 'default': ''}),
        ArgSpec(('--policy',), {'help': '院内制度 docx / pdf 路径', 'default': ''}),
        ArgSpec(('--frames',), {'help': 'ER7 帧目录（批量灌进台账）', 'default': ''}),
        ArgSpec(('--dir',), {
            'help': '批量入库目录（ER7 帧目录）—— 批处理模式：默认启用断点续跑游标，中断后重跑从游标继续',
            'default': '',
        }),
        ArgSpec(('--db',), {'help': '台账库路径', 'default': './data/critval.db'}),
        ArgSpec(('--cursor',), {
            'help': '断点续跑游标路径（给了才启用续跑；--dir 模式默认用 data/ingest_cursor.json）',
            'default': '',
        }),
        ArgSpec(('--rate-limit',), {'help': '限速（帧/秒，0 = 不限）', 'type': float, 'default': 0.0}),
    ),
    'probe': (
        ArgSpec(('--threshold',), {'help': '阈值表 xlsx 路径', 'default': DEFAULT_THRESHOLD}),
        ArgSpec(('--policy',), {'help': '院内制度 docx / pdf 路径', 'default': DEFAULT_POLICY}),
        ArgSpec(('--query',), {'help': '检索式', 'default': '危急值上报时限'}),
        ArgSpec(('--item',), {
            'help': '检验项目码（结构化精确路；与 --item-code 同义，给了 --item 以 --item 为准）',
            'default': '',
        }),
        ArgSpec(('--item-code',), {'help': '检验项目码（结构化精确路）', 'default': '718-7'}),
        ArgSpec(('--value',), {
            'help': '观察值（可写成「6.2 mmol/L」，单位跟在值后面）—— 给了就按院内阈值表试判这条结果',
            'default': '',
        }),
        ArgSpec(('--unit',), {'help': '观察值单位（不给则从 --value 里拆；都没有则按阈值表单位比）', 'default': ''}),
        ArgSpec(('--at',), {'help': '观察时刻 YYYY-MM-DD HH:MM（试判时按此刻选生效的阈值表版本）', 'default': ''}),
        ArgSpec(('--similarity-threshold',), {'help': '综合分硬闸门（低于此分不作为依据）', 'type': float, 'default': 0.2}),
        ArgSpec(('--keywords-weight',), {'help': '词法 BM25 路权重', 'type': float, 'default': 0.5}),
        ArgSpec(('--top-n',), {'help': '返回条数', 'type': int, 'default': 8}),
        ArgSpec(('--top-k',), {'help': '候选池大小', 'type': int, 'default': 64}),
    ),
    'replay': (
        ArgSpec(('--frames',), {'help': 'ER7 帧目录', 'default': DEFAULT_FRAMES}),
        ArgSpec(('--threshold',), {'help': '阈值表 xlsx 路径（判定权威）', 'default': DEFAULT_THRESHOLD}),
        ArgSpec(('--db',), {'help': '台账库路径', 'default': './data/critval.db'}),
        ArgSpec(('--cursor',), {'help': '断点续跑游标路径', 'default': ''}),
        ArgSpec(('--rate-limit',), {'help': '限速（帧/秒，0 = 不限）', 'type': float, 'default': 0.0}),
    ),
    'bench': (
        ArgSpec(('--frames',), {'help': 'ER7 帧目录', 'default': DEFAULT_FRAMES}),
        ArgSpec(('--db',), {'help': '台账库路径', 'default': './data/bench_ingest.db'}),
        ArgSpec(('--mode',), {'help': '跑哪几个阶段（all = 再加幂等重放）', 'choices': ['parse', 'ingest', 'all'], 'default': 'all'}),
        ArgSpec(('--limit',), {'help': '最多跑多少帧（0 = 全部）', 'type': int, 'default': 0}),
    ),
}


def _add_parse_queue_args(sub) -> None:
    """``parse-queue`` 的嵌套动作接线（list / confirm / ignore）。

    嵌套动作没法用扁平的 ``ArgSpec`` 表达，单独写一个接线函数挂到
    ``NESTED_ARG_BUILDERS`` 上。三条动作各自带 ``--json``；框架在
    ``parse-queue`` 顶层再补一个 ``--json``，因此
    ``parse-queue --json list`` 与 ``parse-queue list --json`` 等价。
    """
    from critval.kb.parse_queue import default_queue_path
    from critval.kb.parse_report import default_report_dir

    actions = sub.add_subparsers(dest="action", metavar="动作")
    queue_default = str(default_queue_path())
    report_default = str(default_report_dir())

    p_list = actions.add_parser("list", help="列出待确认项")
    p_list.add_argument("--queue", default=queue_default, help="确认队列 SQLite 路径（默认 data/parse_queue.db）")
    p_list.add_argument(
        "--status",
        default="open",
        choices=["open", "confirmed", "ignored", "all"],
        help="按状态筛选（默认 open）",
    )
    p_list.add_argument("--source", default="", help="只列某一材料的条目（材料 sha256）")
    p_list.add_argument(
        "--json", action="store_true", default=argparse.SUPPRESS, help="以 JSON 输出（机器可读）"
    )

    p_confirm = actions.add_parser("confirm", help="回填确认并触发重解析")
    p_confirm.add_argument("--id", dest="queue_id", required=True, help="条目 ID")
    p_confirm.add_argument("--value", default="", help="回填值（如补上的项目码）")
    p_confirm.add_argument("--handler", default="", help="处理人")
    p_confirm.add_argument("--note", default="", help="备注")
    p_confirm.add_argument("--queue", default=queue_default, help="确认队列 SQLite 路径（默认 data/parse_queue.db）")
    p_confirm.add_argument(
        "--report-dir",
        default=report_default,
        help="解析报告目录（默认 reports/parse_reports）",
    )
    p_confirm.add_argument(
        "--json", action="store_true", default=argparse.SUPPRESS, help="以 JSON 输出（机器可读）"
    )

    p_ignore = actions.add_parser("ignore", help="忽略一条疑点（须写处理人）")
    p_ignore.add_argument("--id", dest="queue_id", required=True, help="条目 ID")
    p_ignore.add_argument("--handler", default="", help="处理人（必填）")
    p_ignore.add_argument("--note", default="", help="忽略理由")
    p_ignore.add_argument("--queue", default=queue_default, help="确认队列 SQLite 路径（默认 data/parse_queue.db）")
    p_ignore.add_argument(
        "--json", action="store_true", default=argparse.SUPPRESS, help="以 JSON 输出（机器可读）"
    )


#: 子命令名 → 嵌套动作接线函数（扁平参数表表达不了的动作）。
NESTED_ARG_BUILDERS = {
    "parse-queue": _add_parse_queue_args,
}

