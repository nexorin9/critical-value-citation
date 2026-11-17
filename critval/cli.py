"""运维 CLI：子命令注册表 + 统一退出码 + ``--json`` 机器可读输出。

CLI 是运维与批处理通道，**不承担主路径** —— 主路径是常驻守护进程
（``python -m critval.daemon``）。这里做的是把运维侧的问题变成**一条命令**：

* 材料来了 → ``ingest``（解析报告，未识别的行逐行列出来）；
* 这条结果会不会判危急 → ``probe --item <code> --value <v>``（试检索回放：
  参与召回的材料版本 + 候选、得分与出处 + 试判结论）；
* 昨晚的台账对不对 → ``reconcile`` / ``export`` / ``audit-verify``；
* 材料换版了影响谁 → ``version-check`` / ``impact-check``；
* 库里有没有断链 → ``audit-verify`` / ``doctor``。

三个约定由 ``critval/cli_registry.py`` 统一兜住：

* **子命令注册表** —— 名字 / 帮助文本 / 参数 / 处理函数登记在一张表里
  （参数表在 ``critval/cli_specs.py``），解析器与派发都由注册表生成；
* **统一 ``--json`` 输出** —— stdout 上只有一个 JSON 文档，顶层固定带
  ``command`` / ``ok`` / ``exit_code`` 三个保留键；
* **统一退出码** —— ``0`` 成功 / ``1`` 业务异常 / ``2`` 参数错。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from critval.card.provenance import describe_provenance
from critval.cli_registry import (
    EXIT_BUSINESS,
    EXIT_OK,
    EXIT_USAGE,
    CommandRegistry,
    CommandSpec,
    emit_json,
    emit_text,
)
from critval.cli_specs import (
    COMMAND_ARG_SPECS,
    NESTED_ARG_BUILDERS,
)
from critval.config import Config
from critval.index.builder import MaterialIndex, MaterialSource
from critval.judge.registry import RuleRegistry
from critval.card.snapshot import CardSnapshotStore
from critval.kb.binding import (
    BindingTable,
    build_binding_table,
    load_binding_table,
    save_binding_table,
)
from critval.kb.consistency import VersionBinding, load_bindings
from critval.kb.loader import load_threshold_table
from critval.kb.schema import parse_date
from critval.kb.parse_queue import (
    ParseQueue,
    reparse_after_confirm,
)
from critval.kb.parse_report import ParseReportStore
from critval.kb.policy import FACT_LABELS, load_policy_document
from critval.kb.policy_diff import diff_policy_documents, summarize, write_csv
from critval.kb.watcher import MaterialCatalog, MaterialWatcher
from critval.retrieve.conflict import (
    CONFLICT_KIND_LABELS,
    PATH_LEXICAL,
    PATH_STRUCTURED,
    attribute_conflicts,
    classify_conflicts,
    clause_evidence,
    describe_conflict,
    threshold_evidence,
)
from critval.retrieve.engine import HybridRetriever
from critval.retrieve.errors import ParamCheckError
from critval.retrieve.fallback import GradedRetriever
from critval.retrieve.fusion import FusionWeights, fuse
from critval.retrieve.levels import level_label
from critval.retrieve.merge import LEXICAL_PATH, PathCandidate
from critval.retrieve.normalize import ScoreNotNormalizedError, path_label
from critval.retrieve.params import RetrievalParams
from critval.retrieve.paths import lexical_candidates, structured_candidates

__all__ = [
    "EXIT_BUSINESS",
    "EXIT_OK",
    "EXIT_USAGE",
    "build_parser",
    "main",
]

#: 版本体检等命令的明细最多列几项（其余只给计数，避免刷屏）。
_LIST_LIMIT = 8

#: 仓库内样例材料的默认路径 —— 相对**包位置**解析，从任何目录跑都对。
_PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: 批处理模式（``ingest --dir``）默认的断点续跑游标路径。
_DEFAULT_INGEST_CURSOR = "data/ingest_cursor.json"

#: 判定结论 → 人话（试判回放用；与判定层口径一致，不另造说法）。
_PROBE_DECISION_TEXT = {
    "critical": "判为危急",
    "not_critical": "不判为危急",
    "unit_unknown": "不判定（单位换不了 / 阈值表没给单位）",
    "no_threshold": "不判定（阈值表未给出该项目的危急阈值）",
    "ambiguous": "不判定（阈值口径互斥）",
    "ambiguous_observation": "不判定（同一标本同刻出现互斥值）",
}

Handler = Callable[[argparse.Namespace], int]

#: 子命令注册表（框架在 ``critval/cli_registry.py``）。
_REGISTRY = CommandRegistry(
    prog="python -m critval.cli",
    description="危急值处置依据检索系统 · 运维命令行（退出码：0 成功 / 1 业务异常 / 2 参数错）",
)

#: 子命令名 → (帮助文本, 处理函数)。保留这份字典供既有调用方与测试查阅。
_COMMANDS: dict[str, tuple[str, Handler]] = {}


def register_command(name: str, help_text: str) -> Callable[[Handler], Handler]:
    """把一个处理函数登记成 CLI 子命令。

    参数声明不在这里写 —— 参数表集中在 ``critval/cli_specs.py``，
    嵌套动作（如 ``parse-queue`` 的 list / confirm / ignore）在
    ``NESTED_ARG_BUILDERS`` 里接线。
    """

    def decorator(func: Handler) -> Handler:
        spec = CommandSpec(
            name=name,
            help=help_text,
            handler=func,
            args=COMMAND_ARG_SPECS.get(name, ()),
            add_args=NESTED_ARG_BUILDERS.get(name),
        )
        _REGISTRY.register(spec)
        _COMMANDS[name] = (help_text, func)
        return func

    return decorator


def _emit(payload: dict, as_json: bool, text: str, code: int = EXIT_OK) -> int:
    if as_json:
        emit_json(payload)
    else:
        emit_text(text)
    return code


def _parse_moment(text: str) -> Optional[datetime]:
    """解析 ``YYYY-MM-DD HH:MM[:SS]`` 时刻（解析不出来返回 ``None``）。"""
    raw = str(text or "").strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


@register_command("rules-summary", "打印当前生效规则集摘要（规则条数 / 生效版本 / 缺口条数 / 规则快照 ID）")
def _cmd_rules_summary(args: argparse.Namespace) -> int:
    path = Path(args.threshold)
    if not path.exists():
        print(f"阈值表不存在：{path}", file=sys.stderr)
        return EXIT_USAGE
    ruleset = RuleRegistry.init_config(path)
    summary = ruleset.summary()
    summary["mode"] = "summary"
    return _emit(summary, args.json, ruleset.summary_text())


@register_command("rules-reload", "按材料指纹热载入规则集（指纹未变则不换快照 ID）")
def _cmd_rules_reload(args: argparse.Namespace) -> int:
    path = Path(args.threshold)
    if not path.exists():
        print(f"阈值表不存在：{path}", file=sys.stderr)
        return EXIT_USAGE
    RuleRegistry.init_config(path)
    before = RuleRegistry.snapshot_id()
    ruleset = RuleRegistry.reload(path)
    after = RuleRegistry.snapshot_id()
    summary = ruleset.summary() if ruleset else RuleRegistry.summary()
    summary["mode"] = "reload"
    summary["snapshot_before"] = before
    summary["snapshot_after"] = after
    summary["snapshot_changed"] = before != after
    text = ruleset.summary_text() if ruleset else "（未装载规则集）"
    text += f"\n热载入前快照     : {before}\n热载入后快照     : {after}"
    text += "\n结论             : " + ("材料指纹未变，沿用原快照" if before == after else "材料已变更，已切换到新快照")
    return _emit(summary, args.json, text)


@register_command("config-check", "检查必填凭据位与格式，输出缺失清单与当前配置版本号")
def _cmd_config_check(args: argparse.Namespace) -> int:
    """启动自检：从环境变量重新采集一份快照 → 逐项检查 → 落盘快照。"""
    snapshot = Config.reload()
    check = Config.self_check()
    payload = check.to_dict()
    payload["snapshot"] = snapshot.to_dict()
    if args.persist:
        path = Config.persist_snapshot(snapshot)
        payload["snapshot_path"] = str(path)
    text = check.report_text()
    counts = " / ".join(f"{k} {v}" for k, v in sorted(snapshot.source_counts.items()))
    text += "\n──────────────────────────────"
    text += f"\n键来源统计  : {counts}"
    text += f"\n本次快照落盘: {payload.get('snapshot_path', '（未落盘）')}"
    code = EXIT_BUSINESS if check.blocking else EXIT_OK
    if args.json:
        emit_json(payload, command="config-check", ok=code == EXIT_OK, exit_code=code)
    else:
        print(text)
    return code


@register_command(
    "export",
    "导出处置卡快照（按事件取回当时推送的原文，不重新渲染）或复核报表（按日 / 周导出明细与汇总）",
)
def _cmd_export(args: argparse.Namespace) -> int:
    """两条导出路径：

    * ``--event <id>`` —— 取回**当时推送的原文**。复核读**快照**而不是
      重新渲染 —— 阈值表改版、制度改版、配置热载入之后重新渲染出来的
      内容，与当时推给值班人的那份已经不是一回事了；
    * ``--scope daily|weekly`` —— 导出**复核报表**（明细 + 汇总），
      给检验科组长次日复核用。
    """
    if getattr(args, "scope", ""):
        return _cmd_export_report(args)
    if not getattr(args, "event", ""):
        print(
            "要么给 --event（导出处置卡快照），要么给 --scope daily|weekly（导出复核报表）",
            file=sys.stderr,
        )
        return EXIT_USAGE
    if args.format in ("csv", "xlsx"):
        print(
            f"处置卡快照只支持 text / markdown / html，收到 {args.format}",
            file=sys.stderr,
        )
        return EXIT_USAGE

    store = CardSnapshotStore(args.snapshot_dir)
    snapshot = store.find_by_event(args.event)
    if snapshot is None:
        print(
            f"事件 {args.event} 没有处置卡快照（快照目录：{store.root}）",
            file=sys.stderr,
        )
        return EXIT_BUSINESS

    if args.verify:
        verification = store.verify(snapshot.snapshot_id)
        if not verification.ok:
            print(verification.report_text(), file=sys.stderr)
            return EXIT_BUSINESS

    text = snapshot.rendering(args.format)
    if args.json:
        print(
            json.dumps(
                {
                    "snapshot_id": snapshot.snapshot_id,
                    "event_id": snapshot.event_id,
                    "card_id": snapshot.card_id,
                    "created_at": snapshot.created_at,
                    "config_version": snapshot.config_version,
                    "threshold_version": snapshot.threshold_version,
                    "policy_version": snapshot.policy_version,
                    "ruleset_snapshot_id": snapshot.ruleset_snapshot_id,
                    "format": args.format,
                    "content": text,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return EXIT_OK

    print(text)
    if args.verify:
        print()
        print(store.verify(snapshot.snapshot_id).report_text())
    return EXIT_OK


def _cmd_export_report(args: argparse.Namespace) -> int:
    """导出复核报表（日报 / 周报）。

    给检验科组长次日复核用：一份**明细**（每条事件一行）+ 一份**汇总**
    （按科室 / 按项目 / 按依据条款三个视角的分组计数，含超时未确认 /
    待回写 / 失败三类异常计数）。
    """
    from critval.ledger.export import (
        ExportDependencyError,
        export_detail_csv,
        export_report_xlsx,
        export_summary_csv,
    )
    from critval.ledger.report import build_report
    from critval.ledger.store import LedgerStore

    db_path = Path(args.db or Config.get_env("CRITVAL_DB_PATH") or "./data/critval.db")
    if not db_path.exists():
        print(f"台账不存在：{db_path}", file=sys.stderr)
        return EXIT_USAGE

    with LedgerStore(db_path) as store:
        report = build_report(
            store,
            args.scope,
            args.date,
            critical_only=bool(getattr(args, "critical_only", False)),
        )

    payload = report.to_dict()
    written: list[str] = []
    if args.format == "csv":
        detail_path = Path(args.out) if args.out else Path(
            f"reports/ledger_review_{report.scope}_{report.period_end}_明细.csv"
        )
        # 汇总文件名由明细名派生：「X_明细.csv」/「X明细.csv」→「X_汇总.csv」
        stem = detail_path.stem
        base = stem
        for suffix in ("_明细", "明细"):
            if base.endswith(suffix):
                base = base[: -len(suffix)]
                break
        summary_path = detail_path.with_name(f"{base}_汇总.csv")
        export_detail_csv(report, detail_path)
        export_summary_csv(report, summary_path)
        written = [str(detail_path), str(summary_path)]
    elif args.format == "xlsx":
        target = Path(args.out) if args.out else Path(
            f"reports/ledger_review_{report.scope}_{report.period_end}.xlsx"
        )
        try:
            export_report_xlsx(report, target)
        except ExportDependencyError as exc:
            print(str(exc), file=sys.stderr)
            return EXIT_BUSINESS
        written = [str(target)]

    payload["written"] = written
    if args.json:
        emit_json(payload, command="export")
        return EXIT_OK

    if args.format in ("text", ""):
        print(report.to_text())
        if written:
            print()
            print("导出文件：")
            for path in written:
                print(f"  · {path}")
        return EXIT_OK

    print(report.to_text())
    print()
    print("导出文件：")
    for path in written:
        print(f"  · {path}")
    return EXIT_OK


@register_command(
    "reconcile",
    "本地台账与登记系统回执对账（差异清单 + 显式处理）",
)
def _cmd_reconcile(args: argparse.Namespace) -> int:
    """对账：本地台账说已确认 / 已回写，登记系统未必已受理。

    **默认只读** —— 只读两边、只出差异清单，不改任何本地状态，也不发写
    请求。要动状态必须显式 ``--resolve``（并写明处理人 ``--actor``），
    每一次处理都留审计。

    差异分四类（``missing_local`` / ``missing_remote`` / ``status_mismatch``
    / ``field_mismatch``），每行带一个建议动作：补写 / 重放 / 标记人工处理。
    """
    from critval.ledger.store import LedgerStore
    from critval.writeback.client import RegistrationSystemClient
    from critval.writeback.reconcile import Reconciler

    # 配置面从环境变量重采一次（凭据位与基地址都在环境变量里）。
    Config.init_env()

    db_path = Path(
        args.db or Config.get_env("CRITVAL_DB_PATH") or "./data/critval.db"
    )
    if not db_path.exists():
        print(f"台账不存在：{db_path}", file=sys.stderr)
        return EXIT_USAGE

    if getattr(args, "resolve", False) and not str(args.actor or "").strip():
        print("--resolve 必须同时写明处理人（--actor），处理动作一律留痕到审计", file=sys.stderr)
        return EXIT_USAGE

    kinds = tuple(k for k in (args.only or "").split(",") if k)
    event_ids = tuple(e for e in (args.event or "").split(",") if e)

    client = RegistrationSystemClient.from_config()
    with LedgerStore(db_path) as store:
        reconciler = Reconciler(store, client)
        report = reconciler.reconcile(
            event_ids=event_ids,
            since=args.since or "",
            limit=int(args.limit or 0),
        )
        outcomes: list[Any] = []
        if getattr(args, "resolve", False):
            outcomes = reconciler.resolve(
                report,
                actor=args.actor,
                kinds=kinds,
                event_ids=event_ids,
            )

    csv_path = Path(args.out or "reports/reconcile_diff.csv")
    report.write_csv(csv_path)

    payload = report.to_dict()
    payload["csv_path"] = str(csv_path)
    payload["resolved"] = [o.to_dict() for o in outcomes]

    if args.json:
        emit_json(payload, command="reconcile")
        return EXIT_OK

    print(report.to_text())
    print()
    print(f"差异清单：{csv_path}")
    if outcomes:
        print()
        print("显式处理结果：")
        for outcome in outcomes:
            mark = "成功" if outcome.ok else "失败"
            print(f"  · [{mark}] {outcome.message_control_id} {outcome.action} → {outcome.target}")
            print(f"     {outcome.detail}")
    elif getattr(args, "resolve", False):
        print()
        print("显式处理结果：没有符合条件的差异行")
    else:
        print()
        print("本次为只读对账：未改动任何本地状态（要处理请加 --resolve --actor <处理人>）")
    return EXIT_OK


@register_command(
    "writeback-retry",
    "显式补写失败回执（待写队列清单 + 逐条重放）",
)
def _cmd_writeback_retry(args: argparse.Namespace) -> int:
    """补写上一轮没写成的回执。

    **默认只读**（``--list``）：只列待写队列，说明每条**为什么失败**、
    **能不能重试**、**下一步该谁做什么**。要真补写必须显式给
    ``--event <事件号>`` 或 ``--all``。

    三条硬口径：

    1. **只补「回写失败」的** —— 没确认过的回执不在补写范围内（那不是
       补写，那是越权）；
    2. **仍失败就留在队列里并继续告警** —— 不静默丢弃，也不假装补上了；
    3. **冲突不补** —— 同一消息控制号下内容与既有事实不同的，转人工等
       人裁决，补写命令不替人做决定。
    """
    from critval.ledger.store import LedgerStore
    from critval.writeback.client import RegistrationSystemClient
    from critval.writeback.registry import RegistryWriteback

    Config.init_env()

    db_path = Path(args.db or Config.get_env("CRITVAL_DB_PATH") or "./data/critval.db")
    if not db_path.exists():
        print(f"台账不存在：{db_path}", file=sys.stderr)
        return EXIT_USAGE

    event_ids = tuple(e for e in (args.event or "").split(",") if e.strip())
    list_only = bool(getattr(args, "list", False))
    if not list_only and not event_ids and not getattr(args, "all", False):
        print(
            "补写是写操作：要么加 --event <事件号>，要么加 --all，"
            "要么加 --list 只看队列",
            file=sys.stderr,
        )
        return EXIT_USAGE

    probe = not bool(getattr(args, "no_probe_remote", False))
    client = RegistrationSystemClient.from_config()
    with LedgerStore(db_path) as store:
        writeback = RegistryWriteback(
            client,
            store,
            actor=str(args.actor or "").strip() or "writeback",
            probe_remote=probe,
            max_attempts=max(1, int(args.max_attempts or 1)),
        )
        queue = writeback.pending_detail(limit=int(args.limit or 100))
        if list_only:
            return _emit_writeback_queue(queue, as_json=bool(args.json))

        if event_ids:
            targets = list(event_ids)
        else:
            targets = [item["event_id"] for item in queue]
        if not targets:
            return _emit_writeback_queue(queue, as_json=bool(args.json), note="待写队列是空的")

        outcomes = []
        for event_id in targets:
            outcomes.append(writeback.replay(event_id))
        queue_after = writeback.pending_detail(limit=int(args.limit or 100))

    payload = {
        "targets": targets,
        "outcomes": [outcome.to_dict() for outcome in outcomes],
        "still_pending": [
            outcome.event_id for outcome in outcomes if not outcome.ok
        ],
        "queue_after": queue_after,
    }
    if args.json:
        code = EXIT_OK if not payload["still_pending"] else EXIT_BUSINESS
        emit_json(payload, command="writeback-retry", ok=code == EXIT_OK, exit_code=code)
        return code

    print(f"显式补写 {len(targets)} 条回执：")
    for outcome in outcomes:
        if outcome.ok:
            print(f"  · [成功] {outcome.event_id} · 台账状态 {outcome.status}")
            continue
        print(
            f"  · [失败] {outcome.event_id} · 分类 {outcome.failure_label or '（无）'} · "
            f"可重试 {'是' if outcome.retryable else '否'}"
        )
        if outcome.next_action:
            print(f"       · 下一步 {outcome.next_action}")
        if outcome.reason:
            print(f"       · {outcome.reason}")
        if outcome.issues:
            for issue in outcome.issues:
                print(f"       · {issue.field}：{issue.message}")
        if outcome.conflict:
            print(f"       · {outcome.conflict.summary()}")
    failed = [outcome for outcome in outcomes if not outcome.ok]
    if failed:
        print()
        print(
            f"！{len(failed)} 条仍失败：已保留在待写队列里并继续告警，"
            "未被丢弃（下一轮 writeback-retry --all 可再试）"
        )
        for outcome in failed:
            print(f"   · {outcome.event_id}：{outcome.next_action or '待人工处理'}")
        return EXIT_BUSINESS
    return EXIT_OK


def _emit_writeback_queue(
    queue: list[dict[str, Any]],
    *,
    as_json: bool,
    note: str = "",
) -> int:
    """打印待写队列（分类 / 次数 / 下一步）。"""
    if as_json:
        print(
            json.dumps(
                {"queue": queue, "count": len(queue), "note": note},
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return EXIT_OK
    if not queue:
        print("待写队列是空的：没有回写失败的事件")
        return EXIT_OK
    print(f"待写队列（{len(queue)} 条回写失败，等补写）：")
    for item in queue:
        print(
            f"  · {item['event_id']} · {item['item_code']} · {item['dept_code']} · "
            f"{item['failure_label']} · 已试 {item['attempts']} 次 · "
            f"可重试 {'是' if item['retryable'] else '否'} · 下一步 {item['next_action']}"
        )
        if item.get("last_error"):
            print(f"       · 末次错误：{item['last_error']}")
    return EXIT_OK


@register_command(
    "version-check",
    "检查阈值表多版本共存状态（当前生效版本 / 版本空档 / 口径冲突）",
)
def _cmd_version_check(args: argparse.Namespace) -> int:
    """给检验科组长看的改版体检。

    阈值表改版期间新旧两版同时在目录里很常见。本命令回答三件事：
    此刻生效的是哪一版、有没有项目落在**版本空档**里、有没有项目
    出现**两版口径互斥**（同一时刻两版都生效）。
    """
    from critval.kb.loader import load_threshold_table
    from critval.kb.version import build_versions, select_version

    path = Path(args.threshold)
    if not path.exists():
        print(f"阈值表不存在：{path}", file=sys.stderr)
        return EXIT_USAGE
    table = load_threshold_table(path)
    versions = build_versions(table.rows)

    at = date.fromisoformat(args.at) if args.at else date.today()
    item_codes = sorted({row.item_code for row in table.rows if row.item_code})

    gaps: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    uncovered: list[dict[str, Any]] = []
    active: dict[str, int] = {}
    for item_code in item_codes:
        selection = select_version(versions, item_code, at)
        if selection.status == "selected" and selection.version is not None:
            key = selection.version.version_no or "无版本号"
            active[key] = active.get(key, 0) + 1
        elif selection.status == "gap":
            gaps.append({"item_code": item_code, **selection.to_dict()})
        elif selection.status == "conflict":
            conflicts.append({"item_code": item_code, **selection.to_dict()})
        else:
            uncovered.append({"item_code": item_code, **selection.to_dict()})

    bindings = load_bindings(args.bindings) if args.bindings else ()
    binding_rows = [binding.to_dict() for binding in bindings]

    payload = {
        "source_name": table.source_name,
        "source_sha256": table.source_sha256,
        "at": at.isoformat(),
        "versions": [
            {
                **version.to_dict(),
                "state": version.state_at(at),
            }
            for version in versions
        ],
        "active_versions": active,
        "gaps": gaps,
        "conflicts": conflicts,
        "uncovered": uncovered,
        "bindings": binding_rows,
        "parse_report_ok": table.report.ok,
    }

    if args.json:
        code = EXIT_BUSINESS if (gaps or conflicts) else EXIT_OK
        emit_json(payload, command="version-check", ok=code == EXIT_OK, exit_code=code)
        return code

    lines = [
        f"阈值表版本体检：{table.source_name}",
        f"观察时刻     : {at.isoformat()}",
        f"文件指纹     : {table.source_sha256[:12]}",
        f"解析结论     : {'全部行已识别' if table.report.ok else '有未识别行（见解析报告）'}",
        "",
        "版本清单：",
    ]
    for version in payload["versions"]:
        lines.append(
            f"  · {version['version_no'] or '无版本号'} · 生效期 {version['period_text']}"
            f" · 状态 {version['state']} · 覆盖 {len(version['item_codes'])} 个项目"
        )
    lines.append("")
    lines.append(
        "当前生效分布："
        + ("、".join(f"{name} {count} 项" for name, count in sorted(active.items())) or "（无）")
    )
    if conflicts:
        lines.append("")
        lines.append(f"口径冲突（同一时刻多版同时生效，共 {len(conflicts)} 项）：")
        for item in conflicts[:_LIST_LIMIT]:
            lines.append(f"  · {item['item_code']}：{item['reason']}")
        if len(conflicts) > _LIST_LIMIT:
            lines.append(f"  · …其余 {len(conflicts) - _LIST_LIMIT} 项同类冲突（见 --json）")
    if gaps:
        lines.append("")
        lines.append(f"版本空档（落在空档里的结果不取就近版本，共 {len(gaps)} 项）：")
        for item in gaps[:_LIST_LIMIT]:
            lines.append(f"  · {item['item_code']}：{item['reason']}")
        if len(gaps) > _LIST_LIMIT:
            lines.append(f"  · …其余 {len(gaps) - _LIST_LIMIT} 项同类空档（见 --json）")
    if uncovered:
        lines.append("")
        lines.append(f"无生效版本（{len(uncovered)} 项）：")
        for item in uncovered[:_LIST_LIMIT]:
            lines.append(f"  · {item['item_code']}：{item['reason']}")
        if len(uncovered) > _LIST_LIMIT:
            lines.append(f"  · …其余 {len(uncovered) - _LIST_LIMIT} 项（见 --json）")
    if binding_rows:
        lines.append("")
        lines.append(f"版本绑定表：{len(binding_rows)} 条")
        for binding in binding_rows:
            lines.append(
                f"  · {binding['item_code']}：阈值表 {binding['threshold_version']}"
                f" + 制度 {binding['policy_version']}"
            )
    lines.append("")
    if conflicts or gaps:
        lines.append("结论：存在版本冲突或空档 —— 相关结果须转人工，不取就近版本。")
    elif uncovered:
        lines.append(
            f"结论：无冲突、无空档；但有 {len(uncovered)} 个项目在观察时刻没有生效版本"
            f"（多半是改版后停用的项目），这些项目的结果按「查不到依据」转人工。"
        )
    else:
        lines.append("结论：各项目在观察时刻均有唯一生效版本，版本状态自洽。")
    print("\n".join(lines))
    return EXIT_BUSINESS if (gaps or conflicts) else EXIT_OK


@register_command(
    "impact-check",
    "阈值表改版影响面与历史判定重算（只读复核，不改写历史台账）",
)
def _cmd_impact_check(args: argparse.Namespace) -> int:
    """给检验科组长看的改版影响面 + 历史判定重算。

    回答两个问题：**哪些项目的判定口径变了**（阈值 / 方向 / 单位 / 分档）、
    **历史上按旧版判过的结论在新版下还成立吗**（翻转清单）。

    **只读**：重算只产出复核清单，历史台账一行不改 —— 改写历史事实
    必须由人另开处置流程，工具不越这条线。
    """
    from critval.judge.impact import (
        build_impact_report,
        load_ruleset,
        write_flip_csv,
        write_report,
    )
    from critval.ledger.store import LedgerStore

    old_path = Path(args.old)
    new_path = Path(args.new)
    for path in (old_path, new_path):
        if not path.exists():
            print(f"阈值表不存在：{path}", file=sys.stderr)
            return EXIT_USAGE

    at = date.fromisoformat(args.at) if args.at else None
    old_ruleset = load_ruleset(old_path, label="old")
    new_ruleset = load_ruleset(new_path, label="new")

    store = None
    db_path = args.db or Config.get_env("CRITVAL_DB_PATH")
    if db_path and Path(db_path).exists():
        store = LedgerStore(db_path)

    report = build_impact_report(
        old_ruleset,
        new_ruleset,
        store,
        at=at,
        observed_from=args.window_from,
        observed_to=args.window_to,
        dept_code=args.dept,
        limit=args.limit,
    )

    md_path = write_report(report, args.out) if args.out else None
    csv_path = None
    if report.recompute is not None and args.csv:
        csv_path = write_flip_csv(report.recompute.flipped, args.csv)

    payload = report.to_dict()
    payload["report_path"] = str(md_path) if md_path else ""
    payload["flip_csv_path"] = str(csv_path) if csv_path else ""

    if args.json:
        emit_json(payload, command="impact-check")
        return EXIT_OK

    diff = report.diff
    summary = diff.summary()
    lines = [
        "阈值表改版影响面 · 历史判定重算",
        "──────────────────────────────",
        f"旧版本 : {old_ruleset.source_name} · {'、'.join(old_ruleset.versions) or '（无版本号）'}"
        f" · {len(old_ruleset.rules)} 条规则",
        f"新版本 : {new_ruleset.source_name} · {'、'.join(new_ruleset.versions) or '（无版本号）'}"
        f" · {len(new_ruleset.rules)} 条规则",
        f"基准时刻: {diff.at}",
        "",
        f"口径变化: {summary['changed_item_count']} 个项目 / {summary['total']} 处变化"
        f"（其中 {summary['flip_risk_count']} 项可能导致历史结论翻转）",
    ]
    for item in diff.items:
        lines.append(
            f"  · [{item.label}] {item.item_code} {item.item_name} —— {item.changed_fields}"
            f"：{'；'.join(f'{c.label} {c.old_value} → {c.new_value}' for c in item.changes)}"
        )
        if item.band:
            lines.append(f"      可能翻转的值域: {item.band.describe()}")
    if not diff.items:
        lines.append("  （两版口径逐项一致，无变化）")

    if report.recompute is not None:
        result = report.recompute
        lines.append("")
        lines.append(
            f"历史重算: 扫描 {result.scanned} 条 · 翻转 {len(result.flipped)} 条 · "
            f"判不出 {len(result.undecidable)} 条 · 新判出 {len(result.newly_decided)} 条 · "
            f"一致 {result.unchanged} 条"
        )
        lines.append(
            f"台账指纹: {result.ledger_before} → {result.ledger_after}"
            f"（{'一致，历史事实未被改写' if result.ledger_unchanged else '不一致，须排查'}）"
        )
        for event in result.flipped:
            lines.append(
                f"  · 翻转 {event.event_id} {event.item_code} {event.value} {event.unit}"
                f"（{event.dept_code or '未标科室'} {event.observed_at}）："
                f"{event.old_decision} → {event.new_decision} · {event.label}"
            )

    if md_path:
        lines.append("")
        lines.append(f"影响面报告已写入: {md_path}")
    if csv_path:
        lines.append(f"翻转清单已写入: {csv_path}")
    print("\n".join(lines))
    return EXIT_OK


@register_command("console-serve", "启动值班台与入站推送端点（常驻进程承载）")
def _cmd_console_serve(args: argparse.Namespace) -> int:
    """启动值班台服务承载。

    同进程提供入站推送端点（``POST /api/v1/critical-value-events``）与值班台页面。
    监听地址、凭据、阈值表路径全部取自配置对象 —— 命令行只覆盖路径类参数。
    """
    from critval.console.app import build_service, serve

    service = build_service(db_path=args.db, threshold_path=args.threshold)
    serve(args.host, args.port, service=service)
    return EXIT_OK


# ---------------------------------------------------------------------------
# 材料投递与人工确认队列（任务 id 27）
# ---------------------------------------------------------------------------


@register_command(
    "parse-queue",
    "材料解析人工确认队列（list 列出待确认项 / confirm 回填并重解析 / ignore 忽略）",
)
def _cmd_parse_queue(args: argparse.Namespace) -> int:
    """人工确认队列的运维入口。

    队列里每一条都是「材料里没读懂的地方」。确认 = 人把该补的值补上，
    然后**重解析** —— 补完真的生效才算数。
    """
    action = getattr(args, "action", None) or "list"
    queue = ParseQueue(args.queue)
    try:
        if action == "list":
            return _parse_queue_list(queue, args)
        if action == "confirm":
            return _parse_queue_confirm(queue, args)
        if action == "ignore":
            return _parse_queue_ignore(queue, args)
        print(f"未知动作：{action}", file=sys.stderr)
        return EXIT_USAGE
    finally:
        queue.close()


def _parse_queue_list(queue: ParseQueue, args: argparse.Namespace) -> int:
    """列出队列条目。"""
    items = queue.list_items(status=args.status, source_sha256=args.source or "")
    payload = {
        "queue_path": queue.path,
        "stats": queue.stats(),
        "items": [item.to_dict() for item in items],
    }
    if args.json:
        emit_json(payload, command="parse-queue")
        return EXIT_OK

    stats = payload["stats"]
    lines = [
        f"人工确认队列（{queue.path}）",
        f"条目总数 {stats['total']} · 待处理 {stats['pending']}",
    ]
    by_status = stats["by_status"]
    if by_status:
        lines.append("状态分布：" + "、".join(f"{k} {v}" for k, v in sorted(by_status.items())))
    lines.append("")
    if not items:
        lines.append("（没有符合条件的条目）")
    else:
        for item in items:
            where = item.source_name
            if item.row_no:
                where += f" 第 {item.row_no} 行"
            elif item.column_label:
                where += f" 列「{item.column_label}」"
            lines.append(f"  · [{item.status}] {item.queue_id}")
            lines.append(f"      位置：{where}")
            lines.append(f"      原因：{item.reason}")
            if item.target_field:
                lines.append(f"      可回填字段：{item.target_field}")
            if item.handler:
                lines.append(f"      处理人：{item.handler}｜确认值：{item.confirmed_value or '（无）'}")
    print("\n".join(lines))
    return EXIT_OK


def _parse_queue_confirm(queue: ParseQueue, args: argparse.Namespace) -> int:
    """回填确认 → 触发重解析 → 报告回填是否真的生效。"""
    item = queue.get(args.queue_id)
    if item is None:
        print(f"确认队列里没有这条：{args.queue_id}", file=sys.stderr)
        return EXIT_BUSINESS

    updated = queue.confirm(
        args.queue_id,
        value=args.value,
        handler=args.handler,
        note=args.note,
    )
    store = ParseReportStore(args.report_dir)
    outcome = reparse_after_confirm(queue, updated, args.value, report_store=store)

    payload = {
        "item": updated.to_dict(),
        "reparse": outcome,
        "queue_stats": queue.stats(),
    }
    if args.json:
        code = EXIT_OK if outcome.get("reparsed") else EXIT_BUSINESS
        emit_json(payload, command="parse-queue", ok=code == EXIT_OK, exit_code=code)
        return code

    lines = [
        f"已确认：{updated.queue_id}",
        f"  处理人：{updated.handler or '（未填）'}",
        f"  回填值：{updated.confirmed_value or '（无）'}",
    ]
    if not outcome.get("reparsed"):
        lines.append(f"  重解析：未执行 —— {outcome.get('reason', '原因未明')}")
    else:
        applied = outcome.get("applied_overrides", [])
        unapplied = outcome.get("unapplied_overrides", [])
        lines.append(
            f"  重解析：已执行 · 识别行数 {outcome['recognized_rows']} / 总行数 {outcome['total_rows']}"
        )
        for entry in applied:
            lines.append(
                f"    · 回填生效：第 {entry['row_no']} 行 {entry['field']} = {entry['value']}"
            )
        for entry in unapplied:
            lines.append(f"    · 回填未生效：{entry['reason']}")
        resolved = outcome.get("resolved_rows", [])
        if resolved:
            lines.append(f"    · 因本次回填被解决的行：{', '.join(str(r) for r in resolved)}")
    print("\n".join(lines))
    return EXIT_OK if outcome.get("reparsed") else EXIT_BUSINESS


def _parse_queue_ignore(queue: ParseQueue, args: argparse.Namespace) -> int:
    """忽略一条疑点（须写处理人）。"""
    item = queue.get(args.queue_id)
    if item is None:
        print(f"确认队列里没有这条：{args.queue_id}", file=sys.stderr)
        return EXIT_BUSINESS
    if not args.handler:
        print("忽略疑点必须写处理人（--handler），否则没人知道这条是谁放过的", file=sys.stderr)
        return EXIT_USAGE
    updated = queue.ignore(args.queue_id, handler=args.handler, note=args.note)
    payload = {"item": updated.to_dict(), "queue_stats": queue.stats()}
    if args.json:
        emit_json(payload, command="parse-queue")
        return EXIT_OK
    print(f"已忽略：{updated.queue_id}（处理人 {updated.handler}）")
    return EXIT_OK


@register_command("policy-summary", "解析院内危急值报告制度（docx / pdf），输出条款清单与解析报告")
def _cmd_policy_summary(args: argparse.Namespace) -> int:
    """制度解析摘要。

    输出识别条款数 / 未识别行清单 / 字段缺失清单 / 版本与生效期 ——
    制度读不全时，报告里必须写清楚哪几段没读进去。
    """
    path = Path(args.policy)
    if not path.exists():
        print(f"制度文件不存在：{path}", file=sys.stderr)
        return EXIT_USAGE

    document = load_policy_document(path)
    payload = document.to_dict()
    report = document.report
    if args.json:
        code = EXIT_BUSINESS if document.degraded else EXIT_OK
        emit_json(payload, command="policy-summary", ok=code == EXIT_OK, exit_code=code)
        return code

    print(document.summary_text())
    if document.degraded:
        print(f"解析降级：{document.degrade_reason}", file=sys.stderr)
        return EXIT_BUSINESS
    for clause in document.clauses:
        facts = [
            f"通知对象 {clause.notify_target or '（未写）'}",
            f"上报时限 {clause.deadline_text or '（未写）'}",
            f"记录去向 {clause.record_destination or '（未写）'}",
        ]
        marker = (
            f"【字段缺失：{'、'.join(FACT_LABELS.get(f, f) for f in clause.missing_fields)}】"
            if clause.missing_fields
            else ""
        )
        print(
            f"  · {clause.clause_no or '（无条号）'} · {clause.citation.locator}"
            f" · {' · '.join(facts)} {marker}".rstrip()
        )
    if report is not None:
        for row in report.unrecognized_rows:
            print(f"  ! 未识别第 {row.row_no} 段：{row.reason}")
        for row in report.incomplete_rows:
            reason = str(row.get("reason", ""))
            for field, label in FACT_LABELS.items():
                reason = reason.replace(field, label)
            print(f"  ! 第 {row['row_no']} 段 {reason}")
        if report.unrecognized_columns:
            print(f"  ! 制度全文未出现的要素：{'、'.join(report.unrecognized_columns)}")
    return EXIT_OK


@register_command("policy-diff", "比对两版院内危急值报告制度，输出受影响条款清单")
def _cmd_policy_diff(args: argparse.Namespace) -> int:
    """制度改版影响面：按稳定标识逐条对齐两版制度。"""
    old_path = Path(args.old)
    new_path = Path(args.new)
    for path in (old_path, new_path):
        if not path.exists():
            print(f"制度文件不存在：{path}", file=sys.stderr)
            return EXIT_USAGE

    old_document = load_policy_document(old_path)
    new_document = load_policy_document(new_path)
    if old_document.degraded or new_document.degraded:
        reason = old_document.degrade_reason or new_document.degrade_reason
        print(f"制度解析降级，无法比对：{reason}", file=sys.stderr)
        return EXIT_BUSINESS

    diffs = diff_policy_documents(old_document, new_document)
    csv_path = ""
    if args.csv:
        csv_path = str(write_csv(diffs, args.csv))

    payload = {
        "old": old_document.to_dict() | {"clauses": []},
        "new": new_document.to_dict() | {"clauses": []},
        "diffs": [diff.to_dict() for diff in diffs],
        "summary": summarize(diffs),
        "csv": csv_path,
    }
    if args.json:
        emit_json(payload, command="policy-diff")
        return EXIT_OK

    print(
        f"制度改版影响面：{old_document.source_name}（{old_document.version}）"
        f" → {new_document.source_name}（{new_document.version}）"
    )
    for diff in diffs:
        label = diff.clause_no or diff.stable_id
        if diff.stable_id and diff.stable_id != diff.clause_no:
            label = f"{diff.clause_no}（{diff.stable_id}）" if diff.clause_no else diff.stable_id
        print(f"  · [{diff.label}] {label} · {diff.detail}")
    summary = payload["summary"]
    print(f"合计变更 {summary['total']} 条 · 涉及条款 {summary['affected_clause_count']} 条")
    if csv_path:
        print(f"清单已落盘：{csv_path}")
    return EXIT_OK


@register_command("bind", "版本绑定表：绑定检验项目码 → 阈值表版本 + 制度版本（未绑版本不参与召回）")
def _cmd_bind(args: argparse.Namespace) -> int:
    """版本绑定表维护。

    三种用法：

    * ``--list`` —— 看当前绑定表；
    * ``--item/--threshold-version/--policy-version`` —— 手工绑定一条；
    * ``--derive --threshold --policy --at`` —— 按观察时刻从材料**推导**一份。
    """
    path = Path(args.bindings)
    table = load_binding_table(path)

    if args.list:
        payload = table.to_dict() | {"path": str(path)}
        if args.json:
            emit_json(payload, command="bind")
            return EXIT_OK
        print(table.summary_text())
        return EXIT_OK

    derived = False
    if args.derive:
        threshold_path = Path(args.threshold)
        policy_path = Path(args.policy)
        for candidate in (threshold_path, policy_path):
            if not candidate.exists():
                print(f"材料不存在：{candidate}", file=sys.stderr)
                return EXIT_USAGE
        rows = load_threshold_table(threshold_path).rows
        documents = [load_policy_document(policy_path)]
        at = parse_date(args.at) if args.at else date.today()
        if at is None:
            print(f"观察时刻写法不对：{args.at}（应为 YYYY-MM-DD）", file=sys.stderr)
            return EXIT_USAGE
        table = build_binding_table(rows, documents, at)
        derived = True

    if args.item:
        existing = table.for_item(args.item)
        bindings = [b for b in table.bindings if b.item_code != args.item]
        bindings.append(
            VersionBinding(
                item_code=args.item,
                threshold_version=args.threshold_version
                or (existing.threshold_version if existing else ""),
                policy_version=args.policy_version
                or (existing.policy_version if existing else ""),
                note=args.note or (existing.note if existing else ""),
            )
        )
        table = BindingTable(bindings=tuple(bindings))
        derived = True

    if not derived and not table.ok:
        print("绑定表为空 —— 用 --item 手工绑定，或用 --derive 从材料推导。")
        return EXIT_OK

    saved = save_binding_table(table, path)
    payload = table.to_dict() | {"path": str(saved), "derived": derived}
    if args.json:
        emit_json(payload, command="bind")
        return EXIT_OK
    print(table.summary_text())
    print(f"绑定表已落盘：{saved}")
    return EXIT_OK


@register_command("kb-watch", "扫描材料投递目录，比对指纹并重建（新增 / 变更 / 删除事件）")
def _cmd_kb_watch(args: argparse.Namespace) -> int:
    """投递目录扫描。

    只扫描时只报事件；``--apply`` 才真的重建（重解析 → 落报告 → 疑点入队 →
    规则集重编译并原子切换）。
    """
    root = Path(args.dir)
    if not root.exists():
        print(f"投递目录不存在：{root}", file=sys.stderr)
        return EXIT_USAGE

    catalog = MaterialCatalog.load(args.catalog)
    queue = ParseQueue(args.queue)
    try:
        watcher = MaterialWatcher(
            root,
            catalog=catalog,
            queue=queue,
            report_store=ParseReportStore(args.report_dir),
        )
        if not args.apply:
            events = watcher.scan()
            payload = {
                "root": str(root),
                "event_count": len(events),
                "events": [event.to_dict() for event in events],
            }
            if args.json:
                emit_json(payload, command="kb-watch")
                return EXIT_OK
            if not events:
                print(f"投递目录 {root}：没有材料变化（指纹比对结果一致）")
                return EXIT_OK
            print(f"投递目录 {root}：检出 {len(events)} 条变化")
            for event in events:
                print(f"  · [{event.action}] {event.name}（{event.kind}）")
            print("（加 --apply 执行重建）")
            return EXIT_OK

        result = watcher.refresh()
        payload = {**result, "queue_stats": queue.stats()}
        if args.json:
            emit_json(payload, command="kb-watch")
            return EXIT_OK

        print(f"投递目录 {root}：扫描完成，检出 {result['event_count']} 条变化")
        for entry in result["results"]:
            detail = entry.get("detail", {})
            print(f"  · [{entry['status']}] {entry['action']} {entry['name']}")
            if entry["status"] == "rebuilt":
                print(
                    f"      识别 {detail.get('parsed_rows')} / {detail.get('total_rows')} 行"
                    f" · 未识别 {detail.get('unrecognized_rows')} 行"
                    f" · 字段缺失 {detail.get('incomplete_rows')} 行"
                    f" · 疑点入队 {detail.get('queue_added')} 条"
                    f" · 规则集 {detail.get('ruleset_snapshot_before')} → {detail.get('ruleset_snapshot_after')}"
                )
            else:
                print(f"      {detail.get('reason', '')}")
        stats = queue.stats()
        print(f"确认队列：待处理 {stats.get('pending', 0)} 条 / 共 {stats.get('total', 0)} 条")
        return EXIT_OK
    finally:
        queue.close()


@register_command(
    "recall-merge",
    "两路召回合并：结构化精确命中 + 词法 BM25 命中按稳定主键合并，按归一化契约加权排序",
)
def _cmd_recall_merge(args: argparse.Namespace) -> int:
    """两路召回合并演练（召回层的归一化契约 + 候选合并）。

    默认按契约走：两路分数都归一化到 ``[0, 1]``，越界的分数在融合入口直接
    抛错并报出是哪一路、多少分。加 ``--raw-scores`` 可以故意绕过归一化，
    用来演练"契约真的拦得住"。
    """
    threshold_path = Path(args.threshold)
    policy_path = Path(args.policy)
    for path in (threshold_path, policy_path):
        if not path.exists():
            print(f"材料不存在：{path}", file=sys.stderr)
            return EXIT_USAGE

    index = MaterialIndex()
    index.rebuild(
        [
            MaterialSource.from_path(threshold_path, kind="threshold_table", key="threshold"),
            MaterialSource.from_path(policy_path, kind="policy_document", key="policy"),
        ]
    )
    documents = index.documents_by_id()
    at = parse_date(args.at) if args.at else None

    # 结构化路：阈值行按 item_code 精确查表，制度条款按 item_codes 多值键命中。
    structured_hits = []
    seen: set[str] = set()
    for filters in ({"item_code": args.item_code}, {"item_codes": args.item_code}):
        for hit in index.structured.query(at=at, **filters):
            if hit.doc_id in seen:
                continue
            seen.add(hit.doc_id)
            structured_hits.append(hit)
    structured = structured_candidates(structured_hits, requested_keys=1)

    lexical_hits = index.search(args.query, top_k=args.top_k)
    if args.raw_scores:
        # 演练用：跳过归一化，把 BM25 原始分直接送进融合入口。
        lexical = [
            PathCandidate(
                doc_id=hit.doc_id,
                path=LEXICAL_PATH,
                score=hit.score,
                raw_score=hit.score,
                matched_terms=hit.matched_terms,
            )
            for hit in lexical_hits
        ]
    else:
        lexical = lexical_candidates(lexical_hits, documents)

    weights = FusionWeights(structured=args.structured_weight, lexical=args.lexical_weight)
    try:
        weights.validate()
        merged = fuse(structured, lexical, weights)
    except ScoreNotNormalizedError as error:
        print(f"归一化契约拦下了这次融合：{error}", file=sys.stderr)
        print(f"  违规路径：{error.path}（{path_label(error.path)}）", file=sys.stderr)
        print(f"  违规分数：{error.score}", file=sys.stderr)
        return EXIT_BUSINESS
    except ValueError as error:
        print(f"参数不合法：{error}", file=sys.stderr)
        return EXIT_USAGE

    both = [item for item in merged if len(item.paths) == 2]
    payload = {
        "query": args.query,
        "item_code": args.item_code,
        "at": args.at,
        "weights": weights.to_dict(),
        "structured_candidates": len(structured),
        "lexical_candidates": len(lexical),
        "merged_candidates": len(merged),
        "both_paths": len(both),
        "results": [item.to_dict() for item in merged],
    }
    if args.json:
        emit_json(payload, command="recall-merge")
        return EXIT_OK

    print(
        f"两路召回合并（材料：{threshold_path.name} + {policy_path.name}）\n"
        f"查询：{args.query} ｜ 项目码：{args.item_code} ｜ "
        f"观察时刻：{args.at or '（不限）'}\n"
        f"权重：结构化精确路 {weights.structured:.2f} + 词法 BM25 路 {weights.lexical:.2f}\n"
        f"候选：结构化 {len(structured)} 条 · 词法 {len(lexical)} 条 → "
        f"合并 {len(merged)} 条（两路都命中 {len(both)} 条）"
    )
    for number, item in enumerate(merged, start=1):
        structured_part = (
            f"结构化 {item.structured_score:.2f}"
            f"（命中键 {'、'.join(item.matched_keys) or '—'}）"
            if item.structured_score is not None
            else "结构化 —"
        )
        lexical_part = (
            f"词法 {item.lexical_score:.2f}"
            f"（命中词 {'、'.join(item.matched_terms[:4]) or '—'}）"
            if item.lexical_score is not None
            else "词法 —"
        )
        both_mark = " · 两路都命中" if len(item.paths) == 2 else ""
        print(
            f"\n  {number}. 融合分 {item.fused_score:.3f} ｜ {item.locator}\n"
            f"     {structured_part} ｜ {lexical_part}{both_mark}\n"
            f"     稳定主键 {item.stable_key}"
        )
    return EXIT_OK


@register_command(
    "recall",
    "混合召回：结构化精确 + BM25 词法双路融合，经综合分硬闸门与规则重排出依据",
)
def _cmd_recall(args: argparse.Namespace) -> int:
    """混合召回引擎的运维入口。

    两路候选按权重融合 → 综合分低于硬闸门的一律截断 → 规则重排（字段命中 /
    章节位置 / 词项覆盖率）→ 取前 ``top_n`` 条。输出每条依据的出处定位与
    重排因子，便于复核"这条为什么排第一"。
    """
    threshold_path = Path(args.threshold)
    policy_path = Path(args.policy)
    for path in (threshold_path, policy_path):
        if not path.exists():
            print(f"材料不存在：{path}", file=sys.stderr)
            return EXIT_USAGE

    index = MaterialIndex()
    index.rebuild(
        [
            MaterialSource.from_path(threshold_path, kind="threshold_table", key="threshold"),
            MaterialSource.from_path(policy_path, kind="policy_document", key="policy"),
        ]
    )

    bindings = BindingTable()
    if args.bindings:
        bindings = load_binding_table(args.bindings)

    params = RetrievalParams(
        similarity_threshold=args.similarity_threshold,
        keywords_similarity_weight=args.keywords_weight,
        top_n=args.top_n,
        top_k=args.top_k,
        kb_ids=("threshold", "policy"),
        rerank_id=args.rerank_id,
    )
    try:
        params.check()
    except ParamCheckError as error:
        print(f"参数不合法（{error.field_name}）：{error}", file=sys.stderr)
        return EXIT_USAGE

    at = parse_date(args.at) if args.at else None
    retriever = HybridRetriever(index, params, bindings=bindings, at=at)
    try:
        result = retriever.retrieve(args.query, {"item_code": args.item_code})
    except ScoreNotNormalizedError as error:
        print(f"归一化契约拦下了这次融合：{error}", file=sys.stderr)
        return EXIT_BUSINESS

    payload = result.to_dict()
    payload["query"] = args.query
    payload["item_code"] = args.item_code
    payload["at"] = args.at
    if args.json:
        emit_json(payload, command="recall")
        return EXIT_OK

    print(
        f"混合召回（材料：{threshold_path.name} + {policy_path.name}）\n"
        f"查询：{args.query} ｜ 项目码：{args.item_code} ｜ "
        f"观察时刻：{args.at or '（不限）'}\n"
        f"参数：闸门 {params.similarity_threshold:.2f} ｜ "
        f"结构化权重 {params.structured_weight:.2f} + "
        f"词法权重 {params.keywords_similarity_weight:.2f} ｜ "
        f"候选池 {params.top_k} ｜ 返回 {params.top_n} ｜ 重排档 {params.rerank_id or 'default'}\n"
        f"候选：结构化 {result.structured_candidates} 条 · 词法 {result.lexical_candidates} 条 → "
        f"合并 {result.merged_candidates} 条（闸门截断 {result.gated_out} 条 · "
        f"池外截断 {result.pool_dropped} 条）"
    )
    if result.is_empty:
        print(f"\n未召回到依据：{result.empty_response}")
        return EXIT_OK
    for hit in result.hits:
        print(
            f"\n  {hit.rank}. 最终分 {hit.score:.3f}（融合分 {hit.base_score:.3f}）｜ {hit.locator}\n"
            f"     命中路 {'、'.join(hit.paths) or '—'} ｜ 命中键 "
            f"{'、'.join(hit.matched_keys) or '—'}\n"
            f"     重排因子 字段 {hit.factors['field_hit']:.2f} · "
            f"章节 {hit.factors['section_position']:.2f} · "
            f"覆盖 {hit.factors['term_coverage']:.2f} · "
            f"类型 {hit.clause_type_label or '其他'} "
            f"({hit.factors.get('clause_type_factor', 1.0):.2f})"
        )
    return EXIT_OK


@register_command(
    "recall-conflict",
    "双路召回差异归因：口径冲突 / 版本冲突 / 范围冲突清单（待人工裁决，系统不自动择一）",
)
def _cmd_recall_conflict(args: argparse.Namespace) -> int:
    """两路召回差异归因的运维入口（任务 id 33）。

    跑一次混合召回，把两路给出的依据按「项目 + 口径字段」对齐比较，产出
    **待人工裁决**的冲突清单。每条冲突保留两方取值与两方出处（文件名 +
    行号），**系统不替值班人选一个**。

    三类冲突：口径冲突（时限 / 通知对象 / 记录去向 / 阈值数值不一致）、
    版本冲突（引用了非生效版本）、范围冲突（条款适用范围与项目不匹配）。
    """
    threshold_path = Path(args.threshold)
    policy_path = Path(args.policy)
    for path in (threshold_path, policy_path):
        if not path.exists():
            print(f"材料不存在：{path}", file=sys.stderr)
            return EXIT_USAGE

    table = load_threshold_table(threshold_path)
    policy_document = load_policy_document(policy_path)
    if policy_document.degraded:
        print(f"制度解析降级：{policy_document.degrade_reason}", file=sys.stderr)
        return EXIT_BUSINESS

    index = MaterialIndex()
    index.rebuild(
        [
            MaterialSource.from_path(threshold_path, kind="threshold_table", key="threshold"),
            MaterialSource.from_path(policy_path, kind="policy_document", key="policy"),
        ]
    )

    bindings = BindingTable()
    if args.bindings:
        bindings = load_binding_table(args.bindings)

    params = RetrievalParams(
        similarity_threshold=args.similarity_threshold,
        keywords_similarity_weight=args.keywords_weight,
        top_n=args.top_n,
        top_k=args.top_k,
        kb_ids=("threshold", "policy"),
    )
    try:
        params.check()
    except ParamCheckError as error:
        print(f"参数不合法（{error.field_name}）：{error}", file=sys.stderr)
        return EXIT_USAGE

    at = parse_date(args.at) if args.at else None
    retriever = HybridRetriever(index, params, bindings=bindings, at=at)
    result = retriever.retrieve(args.query, {"item_code": args.item_code})

    # 两路各自的依据 → 归因用的「依据侧」。
    rows_by_line = {
        row.citation.line_no: row for row in table.rows if row.citation.line_no is not None
    }
    clauses_by_para = {
        clause.citation.paragraph_no: clause
        for clause in policy_document.clauses
        if clause.citation.paragraph_no is not None
    }

    structured_evidence = []
    lexical_evidence = []
    for hit in result.hits:
        citation = hit.citation
        if citation is None:
            continue
        if citation.kind == "threshold_row":
            row = rows_by_line.get(citation.line_no)
            if row is None:
                continue
            evidence = threshold_evidence(row, stable_key=hit.stable_key)
        elif citation.kind == "policy_clause":
            clause = clauses_by_para.get(citation.paragraph_no)
            if clause is None:
                continue
            evidence = clause_evidence(clause, stable_key=hit.stable_key)
        else:
            continue
        if PATH_STRUCTURED in hit.paths:
            structured_evidence.append(
                evidence if evidence.path == PATH_STRUCTURED
                else _repath(evidence, PATH_STRUCTURED)
            )
        if PATH_LEXICAL in hit.paths:
            lexical_evidence.append(
                evidence if evidence.path == PATH_LEXICAL
                else _repath(evidence, PATH_LEXICAL)
            )

    binding = bindings.for_item(args.item_code)
    conflicts = attribute_conflicts(
        args.item_code,
        structured_evidence,
        lexical_evidence,
        effective_threshold_version=binding.threshold_version if binding else "",
        effective_policy_version=binding.policy_version if binding else "",
    )

    payload = {
        "item_code": args.item_code,
        "query": args.query,
        "at": args.at,
        "classification": classify_conflicts(conflicts),
        "classification_label": CONFLICT_KIND_LABELS.get(
            classify_conflicts(conflicts), classify_conflicts(conflicts)
        ),
        "structured_evidence": len(structured_evidence),
        "lexical_evidence": len(lexical_evidence),
        "conflicts": [conflict.to_dict() for conflict in conflicts],
    }
    if args.json:
        emit_json(payload, command="recall-conflict")
        return EXIT_OK

    print(
        f"双路召回差异归因（材料：{threshold_path.name} + {policy_path.name}）\n"
        f"查询：{args.query} ｜ 项目码：{args.item_code} ｜ "
        f"观察时刻：{args.at or '（不限）'}\n"
        f"依据侧：结构化精确路 {len(structured_evidence)} 条 · "
        f"词法 BM25 路 {len(lexical_evidence)} 条\n"
        f"整体归因：{payload['classification_label']}（{payload['classification']}）"
        f" · 冲突 {len(conflicts)} 条"
    )
    if not conflicts:
        print("\n两路说法一致，未发现口径冲突。")
        return EXIT_OK
    for number, conflict in enumerate(conflicts, start=1):
        print(f"\n  {number}. {describe_conflict(conflict)}")
        for side in conflict.sides:
            who = "结构化精确路" if side.path == PATH_STRUCTURED else "词法 BM25 路"
            print(f"     · {who}：{side.value} ｜ {side.locator or side.stable_key}")
    print("\n系统不替值班人择一：以上冲突需在值班台确认页裁决后再确认回执。")
    return EXIT_OK


def _repath(evidence: Any, path: str) -> Any:
    """把依据侧换成另一条召回路径（同一份依据被两路命中时用）。"""
    from dataclasses import replace

    return replace(evidence, path=path)


@register_command(
    "recall-fallback",
    "召回分级降级：原样 → 关键词扩展 → 章节级兜底 → 转人工（逐级留痕，绝不编依据）",
)
def _cmd_recall_fallback(args: argparse.Namespace) -> int:
    """分级降级召回的运维入口（任务 id 32）。

    严格闸门下查不到制度依据时，按「关键词扩展 → 章节级兜底 → 转人工」逐级
    降级。输出每一级**试了什么、捞到几条**，以及最终依据的层级 —— 值班人
    据此判断这条依据是直接命中的还是降级捞到的。

    第 3 级（降级用尽）**一条引用都不产出**：事件转 ``transferred_to_human``
    并写审计，不编依据。
    """
    threshold_path = Path(args.threshold)
    policy_path = Path(args.policy)
    for path in (threshold_path, policy_path):
        if not path.exists():
            print(f"材料不存在：{path}", file=sys.stderr)
            return EXIT_USAGE

    index = MaterialIndex()
    index.rebuild(
        [
            MaterialSource.from_path(threshold_path, kind="threshold_table", key="threshold"),
            MaterialSource.from_path(policy_path, kind="policy_document", key="policy"),
        ]
    )

    bindings = BindingTable()
    if args.bindings:
        bindings = load_binding_table(args.bindings)

    params = RetrievalParams(
        similarity_threshold=args.similarity_threshold,
        keywords_similarity_weight=args.keywords_weight,
        top_n=args.top_n,
        top_k=args.top_k,
        kb_ids=("threshold", "policy"),
        rerank_id=args.rerank_id,
    )
    try:
        params.check()
    except ParamCheckError as error:
        print(f"参数不合法（{error.field_name}）：{error}", file=sys.stderr)
        return EXIT_USAGE

    at = parse_date(args.at) if args.at else None
    graded = GradedRetriever(HybridRetriever(index, params, bindings=bindings, at=at))
    try:
        result = graded.retrieve_graded(args.query, {"item_code": args.item_code})
    except ScoreNotNormalizedError as error:
        print(f"归一化契约拦下了这次融合：{error}", file=sys.stderr)
        return EXIT_BUSINESS

    payload = result.to_dict()
    payload["query"] = args.query
    payload["item_code"] = args.item_code
    payload["at"] = args.at
    if args.json:
        emit_json(payload, command="recall-fallback")
        return EXIT_OK

    print(
        f"分级降级召回（材料：{threshold_path.name} + {policy_path.name}）\n"
        f"查询：{args.query} ｜ 项目码：{args.item_code} ｜ "
        f"观察时刻：{args.at or '（不限）'}\n"
        f"闸门 {params.similarity_threshold:.2f} ｜ 结构化权重 "
        f"{params.structured_weight:.2f} + 词法权重 "
        f"{params.keywords_similarity_weight:.2f} ｜ 返回 {params.top_n} 条\n"
        "降级过程："
    )
    for attempt in result.fallback_attempts:
        print(
            f"  第 {attempt.level} 级（{attempt.label}）｜ "
            f"命中 {attempt.hit_count} 条 ｜ {attempt.note}"
        )
    print(f"结论层级：第 {result.level} 级（{level_label(result.level)}）")
    if result.is_empty:
        print(f"\n未召回到依据：{result.empty_response}")
        print("处置：事件转人工（transferred_to_human）并留痕，不产出任何引用。")
        return EXIT_OK
    for hit in result.hits:
        print(
            f"\n  {hit.rank}. 最终分 {hit.score:.3f} ｜ {hit.locator} ｜ "
            f"层级 {hit.level}（{level_label(hit.level)}）"
        )
    return EXIT_OK


@register_command("eval-run", "跑召回评测（命中率 / Top-N 命中率 / 出处覆盖率）")
def _cmd_eval_run(args: argparse.Namespace) -> int:
    """召回评测：把评测集跑一遍，输出三个指标并落报告。

    评测集由 ``scripts/build_eval_set.py`` 生成（期望命中条目取自生成器真值）；
    缺评测集时明确提示怎么生成，不静默跑空集。
    """
    from critval.evaluate import (
        default_material_sources,
        load_eval_set,
        render_markdown,
        run_evaluation,
    )

    eval_set_path = Path(args.eval_set)
    if not eval_set_path.exists():
        print(
            f"评测集不存在：{eval_set_path}\n"
            "先跑 `python3 scripts/build_eval_set.py` 生成标注查询集。",
            file=sys.stderr,
        )
        return EXIT_USAGE

    sources = default_material_sources(Path(__file__).resolve().parent.parent)
    missing = [source.path for source in sources if not Path(source.path).exists()]
    if missing:
        print(
            "材料缺失：{}\n先跑 `python3 scripts/fetch_data.py` 生成可再生语料。".format("、".join(missing)),
            file=sys.stderr,
        )
        return EXIT_USAGE

    weights = FusionWeights(structured=args.structured_weight, lexical=args.lexical_weight)
    try:
        weights.validate()
    except ValueError as error:
        print(f"参数不合法：{error}", file=sys.stderr)
        return EXIT_USAGE

    report = run_evaluation(
        load_eval_set(eval_set_path),
        materials=sources,
        depth=args.depth,
        top_n=args.top_n,
        weights=weights,
        candidate_depth=args.candidate_depth,
        citation_mode=args.citation_mode,
    )

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_markdown(report), encoding="utf-8")
    json_path = Path(args.json_out)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )

    payload = report.to_dict()
    if args.json:
        emit_json(payload, command="eval-run")
        return EXIT_OK

    print(
        f"召回评测（{report.query_count} 条查询：正例 {report.positive_query_count} / "
        f"负例 {report.negative_query_count}）\n"
        f"参数面：结构化路权重 {weights.structured:.2f} + 词法路权重 {weights.lexical:.2f} ｜ "
        f"检索深度 {report.depth} ｜ Top-{report.top_n} ｜ 索引条目 "
        f"{report.meta.get('index_documents', 0)}\n"
        f"  命中率        {report.hit_rate:.1%}（命中 {report.hit_total} / 期望 {report.expected_total}）\n"
        f"  Top-{report.top_n} 命中率  {report.top_n_hit_rate:.1%}"
        f"（前 {report.top_n} 名命中 {report.top_n_hit_total} / 期望 {report.expected_total}）\n"
        f"  出处覆盖率    {report.citation_coverage:.1%}"
        f"（出处齐备 {report.citation_complete} / 带出处引用 {report.cited_total}）\n"
        f"  补充：Top-{report.top_n} 全中率 {report.top_n_full_hit_rate:.1%} ｜ "
        f"出处覆盖率·严格口径 {report.citation_coverage_strict:.1%} ｜ "
        f"结果带出处比例 {report.citation_presence_rate:.1%}"
    )
    if report.meta.get("materials_match") is False:
        print("  ⚠️ 材料指纹与评测集记录不一致（材料改版或语料重建），本轮数字仅供参考。")
    print(f"报告 → {out_path} ｜ {json_path}")
    return EXIT_OK


@register_command("recall-baseline", "召回基线对比：纯 BM25 单路 vs 融合引擎（三项指标）")
def _cmd_recall_baseline(args: argparse.Namespace) -> int:
    """召回基线对比：同一份材料、同一份评测集、同一把尺子，只差召回做法。

    编排与渲染都在 ``critval.compare`` 里（脚本 ``scripts/compare_baseline.py``
    调的是同一份代码）—— 换的是入口，不是口径。
    """
    from critval.compare import (
        compare_baselines,
        render_comparison_markdown,
        write_comparison_csv,
    )
    from critval.evaluate import default_material_sources, load_eval_set

    eval_set_path = Path(args.eval_set)
    if not eval_set_path.exists():
        print(
            f"评测集不存在：{eval_set_path}\n"
            "先跑 `python3 scripts/build_eval_set.py` 生成标注查询集。",
            file=sys.stderr,
        )
        return EXIT_USAGE

    sources = default_material_sources(Path(__file__).resolve().parent.parent)
    missing = [source.path for source in sources if not Path(source.path).exists()]
    if missing:
        print(
            "材料缺失：{}\n先跑 `python3 scripts/fetch_data.py` 生成可再生语料。".format(
                "、".join(missing)
            ),
            file=sys.stderr,
        )
        return EXIT_USAGE

    total = args.structured_weight + args.lexical_weight
    if abs(total - 1.0) > 1e-6:
        print(
            "结构化路权重与词法路权重必须互补（和为 1）："
            f"结构化 {args.structured_weight:.2f} + 词法 {args.lexical_weight:.2f} = {total:.2f}",
            file=sys.stderr,
        )
        return EXIT_USAGE

    try:
        params = RetrievalParams(
            similarity_threshold=args.similarity_threshold,
            keywords_similarity_weight=args.lexical_weight,
            top_n=args.depth,
            top_k=1024,
            kb_ids=("threshold", "policy", "corpus"),
        ).check()
    except Exception as error:  # noqa: BLE001 - 参数面报错要落到人话
        print(f"参数不合法：{error}", file=sys.stderr)
        return EXIT_USAGE

    eval_set = load_eval_set(eval_set_path)
    comparison = compare_baselines(
        eval_set,
        sources,
        depth=args.depth,
        top_n=args.top_n,
        params=params,
        candidate_depth=args.candidate_depth,
    )
    markdown = render_comparison_markdown(
        comparison,
        eval_meta={
            "positive": len(eval_set.positive_queries),
            "negative": len(eval_set.negative_queries),
        },
    )
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(markdown, encoding="utf-8")
    csv_path = write_comparison_csv(comparison, Path(args.csv_out))

    payload = comparison.to_dict()
    payload["out"] = str(out_path)
    payload["csv_out"] = str(csv_path)
    if args.json:
        emit_json(payload, command="recall-baseline")
        return EXIT_OK

    rows = comparison.rows
    print(
        f"召回基线对比（{comparison.query_count} 条查询 · 索引条目 "
        f"{comparison.index_documents} · 深度 {args.depth} · Top-{args.top_n}）"
    )
    for row in rows:
        print(
            f"  {row['参赛者']:<26} 命中项数 {row['命中项数']:>3} / "
            f"{row['期望条目数']}（{row['命中率']:.1%}）｜ Top-{args.top_n} 命中率 "
            f"{row['Top-N命中率']:.1%} ｜ 出处覆盖率 {row['出处覆盖率']:.1%}"
        )
    print(
        f"  差值：命中项数 {rows[1]['命中项数'] - rows[0]['命中项数']:+d} 条 ｜ "
        f"Top-{args.top_n} 命中率 "
        f"{rows[1]['Top-N命中率'] - rows[0]['Top-N命中率']:+.1%} ｜ "
        f"出处覆盖率 {rows[1]['出处覆盖率'] - rows[0]['出处覆盖率']:+.1%}"
    )
    print(f"对比表 → {out_path} ｜ {csv_path}")
    return EXIT_OK


@register_command(
    "judge-baseline",
    "判定基线对比：只看 OBX-8 的朴素口径 vs 院内阈值表判定引擎（口径分歧检出数）",
)
def _cmd_judge_baseline(args: argparse.Namespace) -> int:
    """判定基线对比：同一批样例、同一份阈值表、同一把尺子。

    编排与渲染都在 ``critval.judge.baseline_compare`` 里（脚本
    ``scripts/compare_judge_baseline.py`` 调的是同一份代码）—— 换的是入口，
    不是口径。
    """
    from critval.judge.baseline_compare import (
        build_judge_cases,
        compare_judge_baselines,
        render_comparison_markdown,
        write_comparison_csv,
    )
    from critval.judge.ruleset import RuleSet
    from critval.kb.loader import load_threshold_table

    threshold_path = Path(args.threshold)
    if not threshold_path.exists():
        print(f"阈值表不存在：{threshold_path}", file=sys.stderr)
        return EXIT_USAGE

    table = load_threshold_table(threshold_path)
    # 快照 ID 由**材料指纹**推导 —— 注册表的版本计数器随进程内重载次数递增，
    # 同一份材料在不同进程里会拿到不同快照 ID，「可复算」就守不住了。
    ruleset = RuleSet.compile(
        table.rows,
        snapshot_id=f"rs-cmp-{table.source_sha256[:12]}",
        source_name=table.source_name,
        source_sha256=table.source_sha256,
    )
    cases = build_judge_cases(table.rows)
    comparison = compare_judge_baselines(cases, table.rows, ruleset=ruleset)

    markdown = render_comparison_markdown(comparison)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(markdown, encoding="utf-8")
    csv_path = write_comparison_csv(comparison, Path(args.csv_out))

    payload = comparison.to_dict()
    payload["out"] = str(out_path)
    payload["csv_out"] = str(csv_path)
    if args.json:
        emit_json(payload, command="judge-baseline")
        return EXIT_OK

    base_row, engine_row = comparison.rows
    print(
        f"判定基线对比（{comparison.case_count} 条样例 · 阈值表 "
        f"{comparison.threshold_source or threshold_path.name}）"
    )
    for row in comparison.rows:
        print(
            f"  {row['参赛者']:<26} 判危急 {row['判危急数']:>3} ｜ 判不危急 "
            f"{row['判不危急数']:>3} ｜ 不判定 {row['不判定数']:>3} ｜ 口径分歧检出 "
            f"{row['口径分歧检出数']:>3} ｜ 误报 {row['误报数']:>3} ｜ 漏报 {row['漏报数']:>3}"
        )
    print(
        f"  差值：误报 {base_row['误报数'] - engine_row['误报数']:+d} 条 ｜ 漏报 "
        f"{base_row['漏报数'] - engine_row['漏报数']:+d} 条 ｜ 口径分歧检出 "
        f"{engine_row['口径分歧检出数'] - base_row['口径分歧检出数']:+d} 条"
    )
    print(f"对比表 → {out_path} ｜ {csv_path}")
    return EXIT_OK


def _fmt_kv(payload: dict[str, Any]) -> str:
    """把一段输入 / 输出压成一行人读文本（回放终端输出用）。"""
    parts = []
    for key, value in payload.items():
        if isinstance(value, bool):
            text = "是" if value else "否"
        elif value is None:
            text = "（空）"
        else:
            text = str(value)
        parts.append(f"{key} {text}")
    return " · ".join(parts) if parts else "（无）"


@register_command(
    "judge-replay",
    "回放某条事件的判定链路（原始值 → 换算 → 分档 → 阈值比较 → 交叉校验 → 结论），并与落库判定记录逐字段核对",
)
def _cmd_judge_replay(args: argparse.Namespace) -> int:
    """判定回放：按事件 ID 重跑判定，把结果与落库判定记录逐字段比对。

    回放是**只读**的 —— 不写台账、不改状态、不重推处置卡。它回答的是
    「当初那条结论，用同一份材料能不能复算出来」。
    """
    from dataclasses import replace

    from critval.judge.ambiguity import check_ambiguity
    from critval.judge.critical import DECISION_AMBIGUOUS_OBSERVATION, judge_event
    from critval.judge.explain import (
        REPLAY_COMPARE_FIELDS,
        STAGE_ORDER,
        JudgementRecord,
        build_judgement_record,
        diff_records,
        event_from_row,
        replay_supersede_reason,
    )
    from critval.judge.ruleset import RuleSet
    from critval.kb.loader import load_threshold_table
    from critval.ledger.store import LedgerStore

    db_path = Path(args.db or Config.get_env("CRITVAL_DB_PATH") or "./data/critval.db")
    if not db_path.exists():
        print(f"台账不存在：{db_path}", file=sys.stderr)
        return EXIT_USAGE
    threshold_path = Path(args.threshold)
    if not threshold_path.exists():
        print(f"阈值表不存在：{threshold_path}", file=sys.stderr)
        return EXIT_USAGE

    table = load_threshold_table(threshold_path)

    # 采集一份配置快照：回放结论要标出「用的是哪一版配置」（任务 id 20）。
    Config.init_env()

    with LedgerStore(db_path) as store:
        row = store.get_event(args.event)
        if row is None:
            print(f"台账里没有事件 {args.event}", file=sys.stderr)
            return EXIT_BUSINESS
        stored_payload = store.get_judgement_record(args.event)
        if stored_payload is None:
            print(
                f"事件 {args.event} 没有判定记录 —— 该事件可能在本功能上线前入库，无法回放",
                file=sys.stderr,
            )
            return EXIT_BUSINESS

        stored = JudgementRecord.from_dict(stored_payload)
        event = event_from_row(row)

        # 复算走**和当初一样的口径**：落库记录里有规则集快照 ID，说明当初
        # 走的是规则引擎路径，回放也编译一份规则集再判。
        ruleset = None
        if stored.ruleset_snapshot_id:
            ruleset = RuleSet.compile(
                table.rows,
                snapshot_id=f"rs-replay-{table.source_sha256[:12]}",
                source_name=table.source_name,
                source_sha256=table.source_sha256,
            )
        judgement = judge_event(event, table.rows, ruleset=ruleset)

        # 与生产链路对齐的两处上下文（同标本同刻互斥值 / 修正结果冲突）：
        # 互斥检测是只读的；冲突说明只作展示，比对时按「理由归一化」处理。
        ambiguity = check_ambiguity(store, event, table.rows)
        if ambiguity is not None:
            judgement = replace(
                judgement,
                decision=DECISION_AMBIGUOUS_OBSERVATION,
                reason=f"{ambiguity.reason}；{judgement.reason}",
            )
        supersede_note = replay_supersede_reason(store, event)

        replayed = build_judgement_record(
            event, judgement, threshold_source_sha256=table.source_sha256
        )
        differences = diff_records(stored, replayed)
        compared = len(REPLAY_COMPARE_FIELDS) + len(STAGE_ORDER)
        consistent = len(differences) == 0

        payload = {
            "event_id": event.event_id,
            "specimen_no": event.specimen_no,
            "item_code": event.item_code,
            "item_name": event.item_name,
            "decision": replayed.decision,
            "consistent": consistent,
            "compared_fields": compared,
            "differences": differences,
            "stored": stored.to_dict(),
            "replayed": replayed.to_dict(),
            "version_check": {
                "stored_ruleset_snapshot_id": stored.ruleset_snapshot_id,
                "replayed_ruleset_snapshot_id": replayed.ruleset_snapshot_id,
                "stored_config_version": stored.config_version,
                "replayed_config_version": replayed.config_version,
                "threshold_source_sha256": table.source_sha256,
            },
            "supersede_note": supersede_note,
        }
        if args.json:
            code = EXIT_OK if consistent else EXIT_BUSINESS
            emit_json(payload, command="judge-replay", ok=code == EXIT_OK, exit_code=code)
            return code

        print(
            f"[判定回放] 事件 {event.event_id} · 标本 {event.specimen_no or '（无）'}"
            f" · 项目 {event.item_code} {event.item_name} · 科室 {event.dept_code or '（无）'}"
        )
        print(f"  原始值      {replayed.original_value}")
        conversion_output = replayed.steps[2].output if len(replayed.steps) > 2 else {}
        print(
            f"  换算值      {replayed.converted_text or '（未换算）'}"
            f"（精度 {conversion_output.get('精度位数')} 位"
            f" · {conversion_output.get('换算方式')}）"
        )
        print(f"  命中规则    {replayed.rule_id or '（未命中规则）'}")
        print(f"  阈值行      {replayed.threshold_locator or '（无阈值行）'}")
        print(f"  结论        {replayed.decision} —— {replayed.reason}")
        if supersede_note:
            print(f"  取代关系    {supersede_note}")
        print("")
        print("[判定链] 逐段复算")
        for step in replayed.steps:
            print(f"  {step.summary()}")
            print(f"     输入  {_fmt_kv(step.input)}")
            print(f"     输出  {_fmt_kv(step.output)}")
            if step.basis:
                print(f"     依据  {step.basis}")
            if step.note:
                print(f"     备注  {step.note}")
        print("")
        if consistent:
            print(
                f"[复算核对] 与落库判定记录逐字段一致"
                f"（{compared} 项全部相同 · 结论 {replayed.decision} 可复算）"
            )
        else:
            print(f"[复算核对] 不一致：{len(differences)} 处差异")
            for item in differences:
                print(
                    f"  · {item['field']}：落库 {item['stored']!r} → 回放 {item['replayed']!r}"
                )
        print(
            f"[版本对照] 规则集快照 落库 {stored.ruleset_snapshot_id or '（无）'}"
            f" / 回放 {replayed.ruleset_snapshot_id or '（无）'}"
            f" · 配置版本 落库 {stored.config_version} / 回放 {replayed.config_version}"
            f" · 阈值表指纹 {table.source_sha256[:16]}…"
        )
        return EXIT_OK if consistent else EXIT_BUSINESS


@register_command(
    "ledger-trace",
    "回答四问：按哪条依据、通知了谁、几分钟内、谁确认的（台账 + 审计流水）",
)
def _cmd_ledger_trace(args: argparse.Namespace) -> int:
    """把一条事件的处置链路摊开给人看。

    答案全部取自**已落库的事实**（事件行 + 引用表 + 通知记录 + 审计流水），
    不是现场拼的；取不到就明说「没记录」，不拿空值糊弄。
    """
    from critval.ledger.audit import AuditLog
    from critval.ledger.store import LedgerStore

    db_path = Path(args.db or Config.get_env("CRITVAL_DB_PATH") or "./data/critval.db")
    if not db_path.exists():
        print(f"台账不存在：{db_path}", file=sys.stderr)
        return EXIT_USAGE

    with LedgerStore(db_path) as store:
        if store.get_event(args.event) is None:
            print(f"台账里没有事件 {args.event}", file=sys.stderr)
            return EXIT_BUSINESS
        answers = AuditLog(store).trace(args.event)
        notifications = store.notifications_of(args.event)
        payload = answers.to_dict()
        payload["notifications"] = notifications

    lines = [
        f"事件 {args.event} · 当前状态 {answers.status or '（无）'}",
        "──────────────────────────────",
        f"按哪条依据   : {answers.basis or '（台账里没有命中阈值行记录）'}",
    ]
    for locator in answers.basis_citations:
        if locator and locator != answers.basis:
            lines.append(f"               · {locator}")
    lines.append(f"通知了谁     : {answers.notified_who or '（没有通知记录）'}")
    lines.append(
        f"几分钟内     : {answers.due_minutes if answers.due_minutes is not None else '（制度未写时限）'}"
    )
    lines.append(
        f"谁确认的     : {answers.acknowledged_by or '（尚未确认）'}"
        + (f"（{answers.acknowledged_at}）" if answers.acknowledged_at else "")
    )
    if answers.registered_at:
        lines.append(f"回写登记系统 : {answers.registered_at}")
    lines.append("──────────────────────────────")
    lines.append(f"推送记录     : {len(notifications)} 次尝试")
    for record in notifications:
        lines.append(
            f"               · 第 {record.get('attempt')} 次"
            f"（{record.get('kind')}）{record.get('notified_at')} 渠道 {record.get('channel') or '未指定'}"
        )
    if answers.missing:
        lines.append("──────────────────────────────")
        lines.append("没记录到     : " + "、".join(answers.missing))
    return _emit(payload, args.json, "\n".join(lines))


@register_command(
    "trace",
    "回放一条事件的处置链：事件在哪一步停下、为什么（节点级轨迹）",
)
def _cmd_trace(args: argparse.Namespace) -> int:
    """把一条事件**逐节点**回放一遍，回答「停在哪一步、为什么」。

    与 ``ledger-trace`` 的分工：``ledger-trace`` 回答**四问**（按哪条依据、
    通知了谁、几分钟内、谁确认的），从**已生效事实**里取答案；
    ``trace`` 回答**过程**（走过哪些环节、在哪一步停的、为什么停、
    这一环重跑有没有意义），从**节点级轨迹**里取答案。

    轨迹是处置链跑的时候**当场记的**，随事件落库；回放读的是落库的轨迹，
    不是现场重跑 —— 事后复核看到的与当时发生的完全一致。
    """
    from critval.flow.trace import render_trace_text
    from critval.ledger.store import LedgerStore

    db_path = Path(args.db or Config.get_env("CRITVAL_DB_PATH") or "./data/critval.db")
    if not db_path.exists():
        print(f"台账不存在：{db_path}", file=sys.stderr)
        return EXIT_USAGE

    with LedgerStore(db_path) as store:
        if store.get_event(args.event) is None:
            print(f"台账里没有事件 {args.event}", file=sys.stderr)
            return EXIT_BUSINESS
        traces = store.flow_traces_of(args.event)
        status = store.get_status(args.event)

    if not traces:
        print(
            f"事件 {args.event} 没有节点级轨迹（这条事件不是经处置链跑进来的）",
            file=sys.stderr,
        )
        return EXIT_BUSINESS

    text = render_trace_text(traces, event_id=args.event, status=status)
    payload = {
        "event_id": args.event,
        "status": status,
        "trace_count": len(traces),
        "traces": traces,
        "stopped_at": next(
            (
                row.get("node")
                for row in traces
                if row.get("status") in ("failed", "stopped")
            ),
            "",
        ),
        "fallback_used": any(row.get("status") == "fallback" for row in traces),
    }
    return _emit(payload, args.json, text)


@register_command(
    "doctor",
    "台账体检：库完整性 / 并发加固 / 状态分布 / 待办计数 / 配置与规则版本",
)
def _cmd_doctor(args: argparse.Namespace) -> int:
    """值班台开机第一问：昨晚这台机器上的台账还好吗。

    异常项（``error`` 级）会让退出码非零 —— 运维脚本据此报警；
    提醒项（``warn``）不改变退出码，因为「有人待回执」不是故障。
    """
    from critval.ledger.health import run_doctor
    from critval.ledger.store import LedgerStore

    db_path = Path(args.db or Config.get_env("CRITVAL_DB_PATH") or "./data/critval.db")
    if not db_path.exists():
        print(f"台账不存在：{db_path}", file=sys.stderr)
        return EXIT_USAGE

    Config.init_env()
    ruleset_id = RuleRegistry.snapshot_id()
    if args.threshold:
        try:
            RuleRegistry.init_config(args.threshold)
            ruleset_id = RuleRegistry.snapshot_id()
        except Exception as exc:  # 阈值表装载失败不该让体检整体失败
            print(f"阈值表装载失败（不影响体检其余项）：{exc}", file=sys.stderr)

    with LedgerStore(db_path) as store:
        report = run_doctor(
            store,
            db_path=str(db_path),
            config_version=Config.config_version(),
            ruleset_snapshot_id=ruleset_id,
        )

    code = EXIT_OK if report.ok else EXIT_BUSINESS
    if args.json:
        emit_json(report.to_dict(), command="doctor", ok=code == EXIT_OK, exit_code=code)
    else:
        print(report.to_text())
    return code


@register_command("audit-stats", "审计流水统计（按动作 / 科室 / 日期）与通知统计")
def _cmd_audit_stats(args: argparse.Namespace) -> int:
    """审计流水报表 —— 复核与飞检材料用。"""
    from critval.ledger.audit import AuditLog
    from critval.ledger.store import LedgerStore

    db_path = Path(args.db or Config.get_env("CRITVAL_DB_PATH") or "./data/critval.db")
    if not db_path.exists():
        print(f"台账不存在：{db_path}", file=sys.stderr)
        return EXIT_USAGE

    with LedgerStore(db_path) as store:
        log = AuditLog(store)
        payload = log.stats(since=args.since, until=args.until, dept_code=args.dept)
        payload["notify"] = store.notify_stats()
        payload["idempotency"] = store.idempotency_stats()
        if args.export:
            payload["export_path"] = str(log.export_csv(args.export))

    lines = [
        f"审计流水条数 : {payload['total']}",
        f"涉及事件数   : {payload['events']}",
        "──────────────────────────────",
        "按动作统计",
    ]
    for action, count in payload["by_action"].items():
        lines.append(f"               · {action:<28} {count}")
    if payload["by_dept"]:
        lines.append("──────────────────────────────")
        lines.append("按科室统计")
        for dept, count in payload["by_dept"].items():
            lines.append(f"               · {dept:<28} {count}")
    lines.append("──────────────────────────────")
    notify = payload["notify"]
    lines.append(
        f"通知         : 去重事件 {notify['notified_events']} 条 / "
        f"真实尝试 {notify['attempts']} 次（其中重推 {notify['retries']} 次）"
    )
    if "export_path" in payload:
        lines.append(f"导出         : {payload['export_path']}")
    return _emit(payload, args.json, "\n".join(lines))


@register_command(
    "notify-queue",
    "投递可靠性复核：本机通知队列 / 死信 / 逐次投递记录 / 投递轮次",
)
def _cmd_notify_queue(args: argparse.Namespace) -> int:
    """投递可靠性复核 —— 「推了几次、为什么重推、哪条卡住了」。

    四份材料一次看完：

    * **本机通知队列** —— 企微未配置或重试耗尽时处置卡的落点；
    * **死信** —— 重试耗尽的原卡内容与最后一次错误；
    * **逐次投递记录** —— 每一次出站尝试（状态码 / 耗时 / 重试序号 / 错误）；
    * **投递轮次** —— 抑制键的占用情况（同一轮次只推一次）。
    """
    from critval.ledger.store import LedgerStore
    from critval.notify.local import LocalNotifier

    db_path = Path(args.db or Config.get_env("CRITVAL_DB_PATH") or "./data/critval.db")
    queue_path = Path(
        args.queue or Config.get_env("CRITVAL_LOCAL_NOTIFICATION_PATH") or "./data/local_notifications.jsonl"
    )
    if not db_path.exists():
        print(f"台账不存在：{db_path}", file=sys.stderr)
        return EXIT_USAGE

    local = LocalNotifier(queue_path)
    with LedgerStore(db_path) as store:
        if args.event:
            letters = store.dead_letters_of(args.event)
            attempts = store.delivery_attempts_of(args.event)
            rounds = store.delivery_rounds(args.event)
            queue = [r for r in local.records() if r.event_id == args.event]
        else:
            letters = store.dead_letters(limit=_LIST_LIMIT)
            attempts = store.delivery_attempts(limit=_LIST_LIMIT)
            rounds = store.delivery_rounds()
            queue = list(local.records())

    payload = {
        "queue_path": str(queue_path),
        "local_queue": [r.to_dict() for r in queue],
        "dead_letters": letters,
        "delivery_attempts": attempts,
        "delivery_rounds": rounds,
    }
    pending = [r for r in queue if not r.handled]
    lines = [
        f"本机队列文件 : {queue_path}",
        f"队列条数     : {len(queue)}（待处理 {len(pending)}）",
        f"死信条数     : {len(letters)}",
        f"投递尝试记录 : {len(attempts)}",
        f"投递轮次     : {len(rounds)}",
    ]
    if pending:
        lines.append("──────────────────────────────")
        lines.append("待处理的队列条目")
        for record in pending[:_LIST_LIMIT]:
            lines.append(
                f"               · {record.event_id} · 轮次 {record.round_key or '-'} · "
                f"{record.reason or '未记录原因'}"
            )
    if letters:
        lines.append("──────────────────────────────")
        lines.append("死信（重试耗尽 · 事件保留在台账里）")
        for letter in letters[:_LIST_LIMIT]:
            lines.append(
                f"               · {letter['event_id']} · 轮次 {letter['round_key']} · "
                f"试了 {letter['attempts']} 次 · {letter['last_error']}"
            )
    if attempts:
        lines.append("──────────────────────────────")
        lines.append("逐次投递记录")
        for row in attempts[:_LIST_LIMIT]:
            mark = "成功" if row["ok"] else "失败"
            lines.append(
                f"               · {row['event_id']} · 第 {row['attempt']} 次 · {mark} · "
                f"状态码 {row['status_code']} · 耗时 {row['elapsed_ms']} ms · "
                f"退避 {row['waited_ms']} ms"
            )
    return _emit(payload, args.json, "\n".join(lines))


@register_command(
    "audit-verify",
    "审计链完整性自检：逐条重算链式哈希，断链时报出首个断链位置",
)
def _cmd_audit_verify(args: argparse.Namespace) -> int:
    """审计链自检 —— 「append-only 不等于不可篡改」的兑现处。

    库里那条 ``audit_no_update`` 触发器只拦得住**还握着库连接的人**；
    拿到库文件的人先 ``DROP TRIGGER`` 再改一条，触发器拦不住。链式
    哈希把那种改动照出来：内容改过、链哈希改过、中间少了一条，
    都报得出来。

    正常库 exit 0；发现断链 exit 非 0，并报出**首个**断链位置。
    """
    from critval.ledger.audit import AuditLog
    from critval.ledger.store import LedgerStore

    db_path = Path(args.db or Config.get_env("CRITVAL_DB_PATH") or "./data/critval.db")
    if not db_path.exists():
        print(f"台账不存在：{db_path}", file=sys.stderr)
        return EXIT_USAGE

    with LedgerStore(db_path) as store:
        log = AuditLog(store)
        report = log.verify_chain()
        exported = ""
        if args.export:
            exported = str(log.export_csv(args.export))

    payload = report.to_dict()
    if exported:
        payload["export_path"] = exported

    lines = [
        f"审计链校验 · 结论：{'通过' if report.ok else '发现断链'}",
        "──────────────────────────────",
        f"流水条数   : {report.total}",
        f"已校验     : {report.verified}",
        f"链头哈希   : {report.head_hash or '（空链）'}",
    ]
    if report.ok:
        lines.append("──────────────────────────────")
        lines.append("结论       : 逐条重算的内容哈希与链哈希全部对上，链未被改动")
    else:
        first = report.first_break
        lines.append("──────────────────────────────")
        lines.append(f"首个断链   : 流水号 {first.seq}")
        lines.append(f"问题类型   : {first.label}")
        lines.append(f"说明       : {first.detail}")
        if first.expected:
            lines.append(f"期望哈希   : {first.expected}")
        if first.found:
            lines.append(f"实际哈希   : {first.found}")
        lines.append("──────────────────────────────")
        lines.append(f"断链处数   : {len(report.breaks)}")
        for item in report.breaks[1:6]:
            lines.append(f"               · 流水号 {item.seq}：{item.label}")
        if len(report.breaks) > 6:
            lines.append(f"               · …… 另有 {len(report.breaks) - 6} 处")
    if exported:
        lines.append("──────────────────────────────")
        lines.append(f"导出       : {exported}")
    return _emit(payload, args.json, "\n".join(lines), EXIT_OK if report.ok else EXIT_BUSINESS)


@register_command(
    "roster-resolve",
    "把当班名单解析成唯一通知对象（班次窗口判定 + 角色优先级 + 值班长兜底）",
)
def _cmd_roster_resolve(args: argparse.Namespace) -> int:
    """离线复核「此刻该通知谁」。

    读一份排班系统响应（JSON），按给定时刻做班次窗口判定与角色唯一化，
    输出**唯一**通知对象、择人方式与来源标注。拿不到通知对象时退出码为
    ``1``（业务失败 = 转人工），不硬凑一个人。
    """
    from critval.roster.cache import CachedRoster
    from critval.roster.resolve import resolve_notify_target, target_provenance
    from critval.roster.types import parse_roster_payload

    path = Path(args.roster)
    if not path.exists():
        print(f"排班名单文件不存在：{path}", file=sys.stderr)
        return EXIT_USAGE
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"排班名单文件不是合法 JSON：{exc.msg}", file=sys.stderr)
        return EXIT_USAGE

    at = datetime.now()
    if args.at:
        at = _parse_moment(args.at)
        if at is None:
            print(f"判定时刻格式不对：{args.at}（应为 YYYY-MM-DD HH:MM）", file=sys.stderr)
            return EXIT_USAGE

    people = parse_roster_payload(payload)
    cache_entry = None
    if args.origin == "cache":
        if not args.as_of:
            print("--origin cache 时必须给 --as-of（名单取数时刻），否则无法复核有效期", file=sys.stderr)
            return EXIT_USAGE
        fetched = _parse_moment(args.as_of)
        if fetched is None:
            print(f"取数时刻格式不对：{args.as_of}（应为 YYYY-MM-DD HH:MM）", file=sys.stderr)
            return EXIT_USAGE
        cache_entry = CachedRoster(
            dept_code=args.dept,
            people=people,
            fetched_at=fetched.astimezone(),
        )

    target = resolve_notify_target(
        people,
        dept_code=args.dept,
        at=at,
        origin=args.origin,
        as_of=cache_entry.as_of_text() if cache_entry else "",
        cache_entry=cache_entry,
        cache_ttl_seconds=float(args.cache_ttl),
    )
    provenance = target_provenance(target)
    payload_out = target.to_dict()
    payload_out["source_text"] = describe_provenance(provenance)
    payload_out["mode"] = "roster-resolve"

    if args.json:
        code = EXIT_OK if target.resolved else EXIT_BUSINESS
        emit_json(payload_out, command="roster-resolve", ok=code == EXIT_OK, exit_code=code)
        return code

    print("────────── 当班名单 → 通知对象 ──────────")
    print(f"科室          : {target.dept_code or '（未指定）'}")
    print(f"判定时刻      : {target.at}")
    print(f"名单人数      : {len(people)} 人（在班 {len(target.candidates)} 人）")
    print(f"通知对象      : {target.label() or '（无 —— 转人工）'}")
    print(f"择人方式      : {target.selection}")
    print(f"来源          : {payload_out['source_text']}")
    print(f"机器原因码    : {target.reason}")
    print(f"说明          : {target.note}")
    return EXIT_OK if target.resolved else EXIT_BUSINESS


# ---------------------------------------------------------------------------
# 运维通道子命令（任务 id 13）：ingest / bind / probe / replay / export / bench
#
# 这一组是**运维与批处理通道**，不承担主路径 —— 主路径是常驻守护进程
# （``python -m critval.daemon``）。这里提供的是：材料入库出解析报告、
# 试检索回放、批量重放、导出复核、吞吐基准。
# ---------------------------------------------------------------------------


@register_command(
    "ingest",
    "材料与报文入库：解析材料出解析报告 / 批量把 ER7 帧灌进台账（幂等）",
)
def _cmd_ingest(args: argparse.Namespace) -> int:
    """材料入库（出解析报告）或报文批量入库。

    * ``--threshold``：解析阈值表 → 解析报告（识别行数 / 未识别列 /
      未识别行清单）。**未识别的行不会被静默跳过** —— 逐行列出来等人回填。
    * ``--policy``：解析院内制度 → 条款清单与未识别段落清单。
    * ``--frames <目录>``：把目录里的 ER7 帧批量灌进台账 —— 走的是与
      守护进程**同一条**入站路径（``HL7LedgerSink`` → ``LedgerInboundProcessor``），
      幂等键挡重复。
    * ``--dir <目录>``：**批处理模式** —— 与 ``--frames`` 同一条入库路径，
      区别是默认启用断点续跑游标（``data/ingest_cursor.json``）：批量回灌
      中途被打断时，重跑同一条命令会从游标继续，不重复灌已处理过的帧。
    """
    from critval.console.inbound import LedgerInboundProcessor
    from critval.hl7.replay import BatchReplayer, HL7LedgerSink, ReplayCursor, load_sources
    from critval.judge.registry import RuleRegistry
    from critval.kb.loader import load_threshold_table
    from critval.kb.policy import load_policy_document
    from critval.ledger.store import LedgerStore

    frames_dir = args.frames or args.dir
    if not (args.threshold or args.policy or frames_dir):
        print(
            "请至少给一个来源：--threshold / --policy / --frames / --dir",
            file=sys.stderr,
        )
        return EXIT_USAGE

    payload: dict[str, Any] = {}
    lines: list[str] = []

    rows: list[Any] = []
    if args.threshold:
        table = load_threshold_table(args.threshold)
        rows = list(table.rows)
        report = table.report
        payload["threshold"] = report.to_dict()
        lines.append(f"阈值表：{report.summary()}")
        for item in report.unrecognized_rows[:_LIST_LIMIT]:
            lines.append(f"  · 未识别 第 {item.row_no} 行：{item.reason}")
        for item in report.incomplete_rows[:_LIST_LIMIT]:
            lines.append(f"  · 字段缺失 第 {item.get('row_no', '?')} 行：{item.get('reason', '')}")
        if report.unrecognized_columns:
            lines.append(f"  · 未识别列：{'、'.join(report.unrecognized_columns)}")
        if len(report.unrecognized_rows) > _LIST_LIMIT:
            lines.append(f"  · …… 另有 {len(report.unrecognized_rows) - _LIST_LIMIT} 行未识别")

    if args.policy:
        document = load_policy_document(args.policy)
        payload["policy"] = {
            "source_name": document.source_name,
            "version": document.version,
            "clause_count": len(document.clauses),
            "degraded": document.degraded,
            "degrade_reason": document.degrade_reason,
            "report": document.report.to_dict() if document.report else None,
        }
        lines.append(f"制度：{document.summary_text()}")
        if document.report:
            for item in document.report.unrecognized_rows[:_LIST_LIMIT]:
                lines.append(f"  · 未识别 第 {item.row_no} 段：{item.reason}")
            for item in document.report.incomplete_rows[:_LIST_LIMIT]:
                lines.append(
                    f"  · 字段缺失 第 {item.get('row_no', '?')} 段：{item.get('reason', '')}"
                )

    if frames_dir:
        sources = load_sources(frames_dir)
        if not sources:
            print(f"目录里没有可重放的 ER7 帧：{frames_dir}", file=sys.stderr)
            return EXIT_USAGE
        # 批处理模式（--dir）默认启用断点续跑游标；--frames 维持原口径
        # （给了 --cursor 才启用），避免既有批处理被静默改变行为。
        cursor_path = args.cursor
        if not cursor_path and args.dir:
            cursor_path = _DEFAULT_INGEST_CURSOR
        store = LedgerStore(args.db)
        try:
            ruleset = None
            if args.threshold:
                RuleRegistry.init_config(args.threshold)
                ruleset = RuleRegistry.get_ruleset()
            processor = LedgerInboundProcessor(
                store,
                ruleset_provider=lambda: ruleset,
                rows_provider=lambda: rows,
            )
            sink = HL7LedgerSink(processor, store=store)
            # 必须 .load()：游标文件是**上一次**跑批留下的，不读回来就等于
            # 每次都从头跑 —— 续跑功能形同虚设。
            cursor = ReplayCursor(cursor_path).load() if cursor_path else None
            replayer = BatchReplayer(sink, cursor=cursor, rate_limit=args.rate_limit)
            report = replayer.run(sources)
            payload["frames"] = {
                "sources": len(sources),
                "frames_seen": report.frames_seen,
                "frames_processed": report.frames_processed,
                "frames_skipped": report.frames_skipped,
                "events_new": report.events_new,
                "events_duplicate": report.events_duplicate,
                "unparsed": report.unparsed,
                "failed": report.failed,
                "elapsed_seconds": round(report.elapsed_seconds, 3),
                "throughput": round(report.throughput, 2),
                "ledger_events": store.count_events(),
            }
            if cursor is not None:
                payload["frames"]["cursor"] = {
                    "path": str(cursor.path),
                    "enabled": True,
                    "sources_done": len(cursor.entries),
                    "resumed": report.frames_skipped > 0,
                }
                lines.append(
                    f"断点续跑游标：{cursor.path}（已完成来源 {len(cursor.entries)} 个 · "
                    f"本次续跑跳过 {report.frames_skipped} 帧）"
                )
            else:
                payload["frames"]["cursor"] = {"enabled": False}
            lines.append(
                f"报文入库：{report.frames_seen} 帧（处理 {report.frames_processed} · "
                f"续跑跳过 {report.frames_skipped}）→ 新增事件 {report.events_new} · "
                f"幂等跳过 {report.events_duplicate} · 未识别 {report.unparsed} · "
                f"失败 {report.failed} · 台账现有 {store.count_events()} 条"
            )
        finally:
            store.close()

    if args.json:
        emit_json(payload, command="ingest")
        return EXIT_OK
    print("\n".join(lines))
    return EXIT_OK


@register_command(
    "probe",
    "试检索回放：列出参与召回的材料版本，按项目码试检索并给出候选、得分与出处",
)
def _cmd_probe(args: argparse.Namespace) -> int:
    """试检索回放（不落库、不改状态）。

    「参与召回的是哪些材料版本」与「按这个项目码能召回什么依据」两件事
    一起给出来 —— 与源产品「勾选文件试检索」的语义同构：**先把范围摆
    出来，再看检索结果**。

    给了 ``--value`` 还会多给一段**试判**：按同一份院内阈值表，这条观察值
    会不会被判成危急、依据是哪一行。值班技师改阈值表时先在这里试一遍，
    不用等真实危急值来了才知道改坏了。
    """
    from critval.index.builder import MaterialIndex, MaterialSource
    from critval.retrieve.engine import HybridRetriever
    from critval.retrieve.normalize import ScoreNotNormalizedError
    from critval.retrieve.params import RetrievalParams

    threshold_path = Path(args.threshold)
    policy_path = Path(args.policy)
    for path in (threshold_path, policy_path):
        if not path.exists():
            print(f"材料不存在：{path}", file=sys.stderr)
            return EXIT_USAGE

    # --item 与 --item-code 同义；给了 --item 以 --item 为准。
    item_code = args.item or args.item_code

    index = MaterialIndex()
    index.rebuild(
        [
            MaterialSource.from_path(threshold_path, kind="threshold_table", key="threshold"),
            MaterialSource.from_path(policy_path, kind="policy_document", key="policy"),
        ]
    )
    params = RetrievalParams(
        similarity_threshold=args.similarity_threshold,
        keywords_similarity_weight=args.keywords_weight,
        top_n=args.top_n,
        top_k=args.top_k,
        kb_ids=("threshold", "policy"),
    )
    try:
        params.check()
    except ParamCheckError as error:
        print(f"参数不合法（{error.field_name}）：{error}", file=sys.stderr)
        return EXIT_USAGE

    retriever = HybridRetriever(index, params)
    try:
        result = retriever.retrieve(args.query, {"item_code": item_code})
    except ScoreNotNormalizedError as error:
        print(f"归一化契约拦下了这次融合：{error}", file=sys.stderr)
        return EXIT_BUSINESS

    payload = result.to_dict()
    payload["query"] = args.query
    payload["item_code"] = item_code
    visible = retriever.visible_documents(item_code)
    materials: dict[str, dict[str, Any]] = {}
    for document in visible.values():
        entry = materials.setdefault(
            document.source_name,
            {"source_name": document.source_name, "sha256": document.source_sha256, "entries": 0},
        )
        entry["entries"] += 1
    payload["materials"] = [materials[key] for key in sorted(materials)]

    judgement = _probe_trial_judgement(args, item_code)
    if judgement is not None:
        payload["judgement"] = judgement

    # 判了危急却一条**制度条款**都没召回 —— 这是**危险状态**：处置卡的
    # 通知对象 / 上报时限 / 记录去向都出自制度条款，缺了就只是一张没有
    # 依据的卡。退出码报业务异常，让人看见。
    policy_hits = [
        hit for hit in result.hits if getattr(hit.citation, "kind", "") == "policy_clause"
    ]
    dangerous = bool(judgement and judgement.get("critical") and not policy_hits)
    code = EXIT_BUSINESS if dangerous else EXIT_OK
    if args.json:
        emit_json(payload, command="probe", ok=code == EXIT_OK, exit_code=code)
        return code

    print(f"试检索回放（材料：{threshold_path.name} + {policy_path.name}）")
    print(f"查询：{args.query} ｜ 项目码：{item_code}")
    print(
        "参与召回的材料版本："
        + (
            "；".join(
                f"{item['source_name']}（{item['entries']} 条 · 指纹 "
                f"{item['sha256'][:12]}）"
                for item in payload["materials"]
            )
            or "（无可见条目）"
        )
    )
    print(
        f"参数：闸门 {params.similarity_threshold:.2f} ｜ "
        f"结构化 {params.structured_weight:.2f} + 词法 {params.keywords_similarity_weight:.2f} ｜ "
        f"候选池 {params.top_k} ｜ 返回 {params.top_n}"
    )
    if judgement is not None:
        print(
            f"\n试判：{judgement['decision_text']}"
            f"（观察值 {judgement['value']} {judgement['unit'] or '（无单位）'}）"
        )
        print(f"  理由    : {judgement['reason']}")
        print(f"  阈值依据: {judgement['threshold_row'] or '（未命中阈值行）'}")
        if judgement.get("requires_human_review"):
            print("  ⚠ 该结论须转人工复核（不自动当成不危急放过）")
    if result.is_empty:
        print(f"\n未召回到依据：{result.empty_response}")
        if dangerous:
            print("  ⚠ 判为危急却一条制度依据都没召回 —— 处置卡会缺依据，先补制度条款或改检索式")
        return code
    if dangerous:
        print("  ⚠ 判为危急但没召回到任何制度条款 —— 处置卡的时限与记录去向会缺依据")
    print(
        f"候选：结构化 {result.structured_candidates} 条 · 词法 {result.lexical_candidates} 条 → "
        f"合并 {result.merged_candidates} 条（闸门截断 {result.gated_out} 条）"
    )
    for hit in result.hits:
        print(
            f"\n  {hit.rank}. 最终分 {hit.score:.3f} ｜ {hit.locator}\n"
            f"     命中路 {'、'.join(hit.paths) or '—'} ｜ 命中键 {'、'.join(hit.matched_keys) or '—'}"
        )
    return code


def _probe_trial_judgement(
    args: argparse.Namespace, item_code: str
) -> Optional[dict[str, Any]]:
    """按院内阈值表试判一个观察值（只读，不落库）。

    返回 ``None`` 表示没给 ``--value``（不试判）。给了但数值写错、阈值表
    没收录该项目、单位换不了 —— 一律**不判定**并说明原因，绝不猜。
    """
    from decimal import Decimal, InvalidOperation

    from critval.judge.critical import (
        DECISION_CRITICAL,
        judge_value,
        requires_human_review,
        select_threshold_row,
    )
    from critval.kb.loader import load_threshold_table

    raw = str(args.value or "").strip()
    if not raw:
        return None

    # 观察值可以写成「6.2 mmol/L」—— 单位跟在值后面，运维从报告里直接复制。
    unit = str(args.unit or "").strip()
    text = raw
    if not unit:
        parts = raw.split(None, 1)
        text = parts[0]
        if len(parts) > 1:
            unit = parts[1].strip()

    try:
        value = Decimal(text)
    except (InvalidOperation, ValueError):
        return {
            "decision": "not_a_number",
            "decision_text": "不判定（观察值写不成数字）",
            "reason": f"观察值 {raw!r} 解析不成数字，不做判定（转人工）",
            "critical": False,
            "judged": False,
            "requires_human_review": True,
            "value": text,
            "unit": unit,
            "threshold_row": None,
        }

    table = load_threshold_table(args.threshold)
    observed_at = _parse_moment(str(args.at or "")) if getattr(args, "at", "") else None
    row, matched = select_threshold_row(
        list(table.rows),
        item_code,
        observed_at.date() if observed_at else None,
    )
    if row is None:
        reason = (
            f"阈值表 {table.source_name} 未收录项目码 {item_code}"
            if not matched
            else f"项目码 {item_code} 在 {table.source_name} 命中 {len(matched)} 行但无生效版本，不做判定（转人工）"
        )
        return {
            "decision": "no_threshold",
            "decision_text": "不判定（阈值表未收录该项目）",
            "reason": reason,
            "critical": False,
            "judged": False,
            "requires_human_review": True,
            "value": str(value),
            "unit": unit,
            "threshold_row": None,
        }

    judgement = judge_value(row, value, unit)
    citation = row.citation
    return {
        "decision": judgement.decision,
        "decision_text": _PROBE_DECISION_TEXT.get(
            judgement.decision, f"不判定（{judgement.decision}）"
        ),
        "reason": judgement.reason,
        "critical": judgement.decision == DECISION_CRITICAL,
        "judged": judgement.judged,
        "requires_human_review": requires_human_review(judgement.decision),
        "value": str(value),
        "unit": unit,
        "converted_value": (
            str(judgement.converted_value) if judgement.converted_value is not None else ""
        ),
        "converted_unit": judgement.converted_unit or "",
        "conversion_note": judgement.conversion_note,
        "near_boundary": judgement.near_boundary,
        "boundary_note": judgement.boundary_note,
        "threshold_row": citation.locator,
        "threshold_row_detail": citation.to_dict(),
        "rule_id": judgement.rule_id,
    }


@register_command(
    "replay",
    "批量重放 ER7 帧目录（断点续跑 · 幂等去重 · 与守护进程同一条入站路径）",
)
def _cmd_replay(args: argparse.Namespace) -> int:
    """批量重放 —— 回灌演练用。

    走的是产品**同一条**入站路径（``HL7LedgerSink`` → ``LedgerInboundProcessor``），
    不另写一套判定逻辑：批量重放判得对，实时收下来也得判得对。
    """
    from critval.hl7.replay import BatchReplayer, HL7LedgerSink, ReplayCursor, load_sources
    from critval.console.inbound import LedgerInboundProcessor
    from critval.judge.registry import RuleRegistry
    from critval.kb.loader import load_threshold_table
    from critval.ledger.store import LedgerStore

    sources = load_sources(args.frames)
    if not sources:
        print(f"目录里没有可重放的 ER7 帧：{args.frames}", file=sys.stderr)
        return EXIT_USAGE

    rows: list[Any] = []
    ruleset = None
    if args.threshold:
        table = load_threshold_table(args.threshold)
        rows = list(table.rows)
        RuleRegistry.init_config(args.threshold)
        ruleset = RuleRegistry.get_ruleset()

    store = LedgerStore(args.db)
    try:
        processor = LedgerInboundProcessor(
            store,
            ruleset_provider=lambda: ruleset,
            rows_provider=lambda: rows,
        )
        sink = HL7LedgerSink(processor, store=store)
        # 同上：读回上一次落盘的游标，中断后重跑才真的从断点继续。
        cursor = ReplayCursor(args.cursor).load() if args.cursor else None
        replayer = BatchReplayer(
            sink,
            cursor=cursor,
            rate_limit=args.rate_limit,
            progress=None if args.json else _replay_progress(),
        )
        report = replayer.run(sources)
        payload = {
            "sources": len(sources),
            "frames_seen": report.frames_seen,
            "frames_processed": report.frames_processed,
            "frames_skipped": report.frames_skipped,
            "events_new": report.events_new,
            "events_duplicate": report.events_duplicate,
            "unparsed": report.unparsed,
            "failed": report.failed,
            "elapsed_seconds": round(report.elapsed_seconds, 3),
            "throughput": round(report.throughput, 2),
            "ledger_events": store.count_events(),
        }
        if args.json:
            emit_json(payload, command="replay")
            return EXIT_OK
        print(
            f"批量重放：{report.frames_seen} 帧（处理 {report.frames_processed} · "
            f"续跑跳过 {report.frames_skipped}）\n"
            f"新增事件 {report.events_new} · 幂等跳过 {report.events_duplicate} · "
            f"未识别 {report.unparsed} · 失败 {report.failed}\n"
            f"吞吐 {report.throughput:.1f} 帧/秒 · 台账现有 {store.count_events()} 条"
        )
        return EXIT_OK
    finally:
        store.close()


def _replay_progress() -> Callable[[int, int, Any], None]:
    """重放进度回调（每 200 帧打一行，别刷屏）。"""

    def _emit(done: int, total: int, report: Any) -> None:
        if done % 200 == 0 or done == total:
            print(f"  进度 {done}/{total} 帧 · 新增 {report.events_new} · 幂等 {report.events_duplicate}")

    return _emit


@register_command(
    "bench",
    "入站吞吐基准：帧/秒、P50/P95 单帧耗时、峰值内存、幂等重放",
)
def _cmd_bench(args: argparse.Namespace) -> int:
    """入站吞吐基准 —— 回答「院内真实报文量下扛不扛得住」。

    三个阶段：``parse``（纯计算）/ ``ingest``（端到端落台账）/
    ``idempotent``（幂等重放）。峰值内存双测：``tracemalloc`` 堆峰值
    （分阶段）+ ``resource`` 进程 RSS 高水位（进程级）。
    """
    import sys as _sys

    scripts_dir = Path(__file__).resolve().parent.parent / "scripts"
    if str(scripts_dir) not in _sys.path:
        _sys.path.insert(0, str(scripts_dir))
    try:
        import bench_ingest  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover —— 基准脚本缺失时报清楚
        print(f"找不到基准脚本：{exc}", file=sys.stderr)
        return EXIT_BUSINESS

    argv = ["--frames", args.frames, "--mode", args.mode, "--db", args.db]
    if args.limit:
        argv += ["--limit", str(args.limit)]
    if args.json:
        argv.append("--json")
    return int(bench_ingest.main(argv))


def build_parser() -> argparse.ArgumentParser:
    """按子命令注册表装配解析器。

    每个子命令的名字、帮助文本与参数都登记在注册表里（参数表见
    ``critval/cli_specs.py``），这里不再逐个 ``if name == ...`` 拼参数 ——
    新增子命令不用改解析器。
    """
    return _REGISTRY.build_parser()

def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI 入口 —— 派发与退出码收敛都在注册表里（``critval/cli_registry.py``）。"""
    return _REGISTRY.run(argv)


if __name__ == "__main__":
    sys.exit(main())
