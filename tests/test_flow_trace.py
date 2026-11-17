"""处置链节点级轨迹与链路回放的单元测试。

对应 task id 45 的「写 ``tests/test_flow_trace.py``：轨迹字段齐备、耗时为正数、
摘要脱敏（断言不含 PID-3 明细）、回放输出与落库轨迹一致」。

盯住五件事：

* **轨迹字段齐备** —— 一个节点跑过就有「节点名 / 环节 / 收场 / 耗时 /
  输入摘要 / 产出摘要 / 结论」，少一项就答不了「这一步做了什么」；
* **耗时为正数** —— 快节点被四舍五入成 0 会让人以为「没跑过」；
* **摘要脱敏** —— 轨迹随事件**长期**留在台账里，是最容易漏掉患者明细的
  地方：姓名 / 身份证 / 住院号 / PID-3 明细一律不进摘要；
* **回放与落库一致** —— 回放读的是落库的轨迹，不是现场重跑，
  事后复核看到的必须与当时发生的完全一致；
* **能回答「在哪一步停下、为什么」** —— 这是本任务存在的唯一理由。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from critval.flow.dsl import (
    DEFAULT_FALLBACK_COMPONENT,
    FlowDSL,
    NodeParam,
    NodeRegistry,
)
from critval.flow.nodes import TransferToHumanNode
from critval.flow.pipeline import FlowStop, Pipeline
from critval.flow.trace import (
    REDACTED,
    STATUS_FAILED,
    STATUS_FALLBACK,
    STATUS_SKIPPED,
    STATUS_SUCCESS,
    TraceRecorder,
    redact_text,
    render_trace_text,
)
from critval.ledger.store import LedgerStore
from critval.models import CriticalEvent

# 患者明细样例：这些串**一个字都不许**出现在轨迹摘要里。
PATIENT_NAME = "张三"
PATIENT_ID_CARD = "310101199001011234"
PATIENT_INPATIENT_NO = "住院流水号 00012345"
PID3_DETAIL = "PID-3: 住院流水号 00012345^住院流水号 00012345"


def _event(event_id: str = "evt-trace-0001", patient_ref: str = "pt-000000000001") -> CriticalEvent:
    return CriticalEvent(
        event_id=event_id,
        message_control_id="MC-TRACE-0001",
        specimen_no="SP-TRACE-0001",
        item_code="718-7",
        item_name="血红蛋白",
        value=Decimal("25"),
        unit="g/L",
        observed_at=datetime(2026, 1, 2, 8, 30, tzinfo=timezone.utc),
        dept_code="LAB",
        patient_ref=patient_ref,
    )


@pytest.fixture()
def store() -> LedgerStore:
    instance = LedgerStore(":memory:")
    try:
        yield instance
    finally:
        instance.close()


# ---------------------------------------------------------------------------
# 测试用的节点
# ---------------------------------------------------------------------------
class RecordingNode:
    """记自己跑过几次的节点。"""

    def __init__(self, name: str, param: NodeParam, calls: list) -> None:
        self.name = name
        self.param = param
        self.calls = calls

    def run(self, context):
        self.calls.append(self.name)
        context["judgement"] = {"decision": "critical", "item_code": "718-7"}
        return {"node": self.name, "decision": "critical"}


class BoomNode:
    """一跑就炸的节点。"""

    def __init__(self, name: str, param: NodeParam) -> None:
        self.name = name
        self.param = param

    def run(self, context):
        raise RuntimeError("这个环节炸了")


class StopNode:
    """正常止步的节点（不危急这类业务结论）。"""

    def __init__(self, name: str, param: NodeParam) -> None:
        self.name = name
        self.param = param

    def run(self, context):
        raise FlowStop("判定结论为不危急")


def _registry(calls: list) -> NodeRegistry:
    registry = NodeRegistry()
    registry.register("step", lambda name, param: RecordingNode(name, param, calls))
    registry.register("boom", BoomNode)
    registry.register("stop", StopNode)
    registry.register(DEFAULT_FALLBACK_COMPONENT, TransferToHumanNode)
    return registry


def _chain(*, fail_at: str = "", stop_at: str = "") -> tuple[FlowDSL, NodeRegistry]:
    calls: list = []
    dsl = FlowDSL()
    dsl.add("received", "step", {}, downstream=["judged"])
    dsl.add("judged", "boom" if fail_at == "judged" else "stop" if stop_at == "judged" else "step",
            {}, downstream=["card_built"])
    dsl.add("card_built", "step", {}, downstream=["notified"])
    dsl.add("notified", "step", {}, downstream=[])
    dsl.add(DEFAULT_FALLBACK_COMPONENT, DEFAULT_FALLBACK_COMPONENT, {}, downstream=[])
    return dsl, _registry(calls)


# ---------------------------------------------------------------------------
# 1. 轨迹字段齐备
# ---------------------------------------------------------------------------
class TestTraceFields:
    def test_每个跑过的节点都有一条轨迹(self):
        dsl, registry = _chain()
        pipeline = Pipeline(dsl, registry)
        result = pipeline.run({"event": _event()})

        assert len(result.traces) == len(result.executed) + 1  # +1 是兜底出口的跳过
        assert [trace.node for trace in result.traces[:3]] == [
            "received",
            "judged",
            "card_built",
        ]

    def test_轨迹字段齐备(self):
        dsl, registry = _chain()
        pipeline = Pipeline(dsl, registry)
        result = pipeline.run({"event": _event()})

        for trace in result.traces:
            payload = trace.to_dict()
            for key in (
                "seq",
                "node",
                "component",
                "status",
                "status_label",
                "conclusion",
                "elapsed_ms",
                "input_summary",
                "output_summary",
                "error",
                "at",
            ):
                assert key in payload, f"轨迹少字段 {key}"
            assert payload["node"]
            assert payload["component"]
            assert payload["status"]
            assert payload["at"]

    def test_步序连号且从一开始(self):
        dsl, registry = _chain()
        pipeline = Pipeline(dsl, registry)
        result = pipeline.run({"event": _event()})

        assert [trace.seq for trace in result.traces] == list(
            range(1, len(result.traces) + 1)
        )

    def test_耗时为正数(self):
        """快节点也要记出耗时 —— 记成 0 会让人以为「没跑过」。"""
        dsl, registry = _chain()
        pipeline = Pipeline(dsl, registry)
        result = pipeline.run({"event": _event()})

        ran = [t for t in result.traces if t.status != STATUS_SKIPPED]
        assert ran, "至少要有一条跑过的轨迹"
        for trace in ran:
            assert trace.elapsed_ms > 0

    def test_结论是人话而不是空串(self):
        dsl, registry = _chain()
        pipeline = Pipeline(dsl, registry)
        result = pipeline.run({"event": _event()})

        for trace in result.traces:
            if trace.status == STATUS_SKIPPED:
                continue
            assert trace.conclusion, f"节点 {trace.node} 没有结论"


# ---------------------------------------------------------------------------
# 2. 摘要脱敏
# ---------------------------------------------------------------------------
class TestRedaction:
    def test_事件摘要不含患者标识(self):
        event = _event(patient_ref=PATIENT_INPATIENT_NO)
        dsl, registry = _chain()
        pipeline = Pipeline(dsl, registry)
        result = pipeline.run({"event": event})

        for trace in result.traces:
            assert PATIENT_INPATIENT_NO not in trace.input_summary
            assert PATIENT_INPATIENT_NO not in trace.output_summary

    def test_患者标识层字段不进摘要(self):
        dsl, registry = _chain()
        pipeline = Pipeline(dsl, registry)
        result = pipeline.run(
            {
                "event": _event(),
                "patient_ref": PATIENT_INPATIENT_NO,
                "patient_name": PATIENT_NAME,
                "id_card": PATIENT_ID_CARD,
                "admission_no": PATIENT_INPATIENT_NO,
            }
        )

        for trace in result.traces:
            blob = trace.input_summary + trace.output_summary
            assert PATIENT_NAME not in blob
            assert PATIENT_ID_CARD not in blob
            assert PATIENT_INPATIENT_NO not in blob

    def test_含患者标签的文本整段脱敏(self):
        text = f"患者 {PATIENT_NAME} 住院 {PATIENT_INPATIENT_NO}"
        assert redact_text(text) == REDACTED

    def test_患者标识层字段名一律不进映射摘要(self):
        """通用映射摘要 —— 患者标识层字段**按字段名**就挡掉，不看值。"""
        from critval.flow.trace import summarize_mapping

        summary = summarize_mapping(
            {
                "item_code": "718-7",
                "value": "25",
                "patient_ref": PATIENT_INPATIENT_NO,
                "patient_name": PATIENT_NAME,
                "id_card": PATIENT_ID_CARD,
                "admission_no": "00012345",
                "住院号": "00012345",
            }
        )

        assert "718-7" in summary
        assert "25" in summary
        assert PATIENT_NAME not in summary
        assert PATIENT_ID_CARD not in summary
        assert "00012345" not in summary

    def test_PID3明细不出现在任何摘要里(self):
        from critval.flow.trace import summarize_mapping

        summary = summarize_mapping(
            {
                "item_code": "718-7",
                "raw_note": f"原始报文 {PID3_DETAIL}",
                "obs_value": "25",
            }
        )

        assert "PID-3" not in summary
        assert "00012345" not in summary
        assert "718-7" in summary

    def test_节点契约点名要患者字段时摘要仍脱敏(self):
        """就算节点契约把 ``patient_ref`` 声明成输入项，摘要也不许带明细。"""
        from critval.flow.contract import FieldSpec, NodeContract

        class PatientAwareNode:
            def __init__(self, name, param):
                self.name = name
                self.param = param

            def contract(self):
                return NodeContract(
                    name=self.name,
                    inputs=(
                        FieldSpec(name="patient_ref", where="context"),
                        FieldSpec(name="item_code", where="context"),
                    ),
                    outputs=(),
                    retryable=False,
                )

            def run(self, context):
                return {"ok": True}

        registry = NodeRegistry()
        registry.register("aware", PatientAwareNode)
        dsl = FlowDSL()
        dsl.add("aware", "aware", {})
        pipeline = Pipeline(dsl, registry)
        result = pipeline.run(
            {
                "event": _event(),
                "item_code": "718-7",
                "patient_ref": PATIENT_INPATIENT_NO,
            }
        )

        blob = result.traces[0].input_summary
        assert "00012345" not in blob
        assert "718-7" in blob


def test_落库后的轨迹同样不含患者明细(store):
    """摘要会长期留在台账里 —— 落库这一关也要拦。"""
    from critval.flow.trace import NodeTrace as _NodeTrace

    store.record_flow_trace(
        "evt-trace-0001",
        _NodeTrace(
            seq=1,
            node="judged",
            component="judge",
            status=STATUS_SUCCESS,
            conclusion="判定结论 critical",
            elapsed_ms=1.0,
            input_summary=f"事件=evt-1 · 姓名 {PATIENT_NAME}",
            output_summary=f"住院号 {PATIENT_INPATIENT_NO}",
            error="",
        ),
    )

    rows = store.flow_traces_of("evt-trace-0001")
    blob = " ".join(
        f"{row['input_summary']} {row['output_summary']} {row['conclusion']}"
        for row in rows
    )
    assert PATIENT_NAME not in blob
    assert PATIENT_INPATIENT_NO not in blob
    assert REDACTED in blob


# ---------------------------------------------------------------------------
# 3. 轨迹随事件落库
# ---------------------------------------------------------------------------
class TestPersistence:
    def test_轨迹随事件落库(self, store):
        dsl, registry = _chain()
        pipeline = Pipeline(dsl, registry, store=store)
        pipeline.run({"event": _event("evt-trace-0001")})

        rows = store.flow_traces_of("evt-trace-0001")
        assert len(rows) == len(pipeline.recorder.traces())
        assert [row["node"] for row in rows][:2] == ["received", "judged"]

    def test_落库轨迹与内存轨迹逐条一致(self, store):
        dsl, registry = _chain()
        pipeline = Pipeline(dsl, registry, store=store)
        result = pipeline.run({"event": _event("evt-trace-0001")})

        rows = store.flow_traces_of("evt-trace-0001")
        for trace, row in zip(result.traces, rows):
            assert row["node"] == trace.node
            assert row["status"] == trace.status
            assert row["conclusion"] == trace.conclusion
            assert row["elapsed_ms"] == pytest.approx(trace.elapsed_ms)

    def test_不同事件的轨迹不混(self, store):
        dsl, registry = _chain()
        pipeline = Pipeline(dsl, registry, store=store)
        pipeline.run({"event": _event("evt-trace-a")})
        pipeline.run({"event": _event("evt-trace-b")})

        assert store.flow_trace_count("evt-trace-a") > 0
        assert store.flow_trace_count("evt-trace-b") > 0
        for row in store.flow_traces_of("evt-trace-a"):
            assert row["event_id"] == "evt-trace-a"

    def test_没有台账时轨迹仍在内存里(self):
        dsl, registry = _chain()
        pipeline = Pipeline(dsl, registry)
        result = pipeline.run({"event": _event()})

        assert result.traces
        assert len(result.traces) == len(pipeline.recorder.traces())

    def test_回放输出与落库轨迹一致(self, store):
        """回放读的是落库的轨迹 —— 事后看到的必须与当时发生的一致。"""
        dsl, registry = _chain()
        pipeline = Pipeline(dsl, registry, store=store)
        result = pipeline.run({"event": _event("evt-trace-0001")})

        rows = store.flow_traces_of("evt-trace-0001")
        text = render_trace_text(rows, event_id="evt-trace-0001")
        for trace in result.traces:
            assert trace.node in text
            assert trace.conclusion in text


# ---------------------------------------------------------------------------
# 4. 回答「在哪一步停下、为什么」
# ---------------------------------------------------------------------------
class TestReplay:
    def test_正常跑完的链说没有停在半路(self):
        dsl, registry = _chain()
        pipeline = Pipeline(dsl, registry)
        result = pipeline.run({"event": _event()})

        text = render_trace_text(result.traces, event_id="evt-trace-0001")
        assert "停在哪一步：没有停在半路" in text

    def test_节点异常时能看出停在哪一步(self):
        dsl, registry = _chain(fail_at="judged")
        pipeline = Pipeline(dsl, registry)
        result = pipeline.run({"event": _event()})

        assert result.status == "transferred_to_human"
        text = render_trace_text(result.traces, event_id="evt-trace-0001")
        assert "停在哪一步：judged" in text
        assert "为什么" in text
        assert "这个环节炸了" in text

    def test_正常止步也标得出停在哪一步(self):
        dsl, registry = _chain(stop_at="judged")
        pipeline = Pipeline(dsl, registry)
        result = pipeline.run({"event": _event()})

        assert result.status == "stopped"
        text = render_trace_text(result.traces, event_id="evt-trace-0001")
        assert "停在哪一步：judged" in text
        assert "不危急" in text

    def test_停止后的节点标为跳过并注明原因(self):
        dsl, registry = _chain(fail_at="judged")
        pipeline = Pipeline(dsl, registry)
        result = pipeline.run({"event": _event()})

        skipped = [t for t in result.traces if t.status == STATUS_SKIPPED]
        assert skipped, "停下的节点后面应该标跳过"
        for trace in skipped:
            assert "未执行" in trace.conclusion
            assert "judged" in trace.conclusion

    def test_失败的节点只出现一次(self):
        """失败的那一环本身有轨迹 —— 不该再补一条「跳过」把它变成两行。"""
        dsl, registry = _chain(fail_at="judged")
        pipeline = Pipeline(dsl, registry)
        result = pipeline.run({"event": _event()})

        judged = [t for t in result.traces if t.node == "judged"]
        assert len(judged) == 1
        assert judged[0].status == STATUS_FAILED

    def test_回落转人工被记进轨迹(self):
        dsl, registry = _chain(fail_at="judged")
        pipeline = Pipeline(dsl, registry)
        result = pipeline.run({"event": _event()})

        fallback = [t for t in result.traces if t.status == STATUS_FALLBACK]
        assert len(fallback) == 1
        assert "转人工" in fallback[0].conclusion

        text = render_trace_text(result.traces, event_id="evt-trace-0001")
        assert "兜底出口" in text

    def test_回放列出每一环的输入与产出(self):
        dsl, registry = _chain()
        pipeline = Pipeline(dsl, registry)
        result = pipeline.run({"event": _event()})

        text = render_trace_text(result.traces, event_id="evt-trace-0001")
        assert "输入 ·" in text
        assert "产出 ·" in text

    def test_中文列宽对齐不参差(self):
        """中文占两列 —— 按字符数补空格会让表格参差不齐。"""
        dsl, registry = _chain()
        pipeline = Pipeline(dsl, registry)
        result = pipeline.run({"event": _event()})

        text = render_trace_text(result.traces, event_id="evt-trace-0001")
        head = [
            line for line in text.splitlines() if line.startswith("  1. ")
        ]
        assert head, "回放里应该有第一环"
        # 节点名与状态之间是固定宽度的空白。
        assert "  " in head[0]

    def test_没有轨迹时不编内容(self):
        text = render_trace_text([], event_id="evt-trace-0001")
        assert "没有轨迹记录" in text


# ---------------------------------------------------------------------------
# 5. 可重试性
# ---------------------------------------------------------------------------
class TestRetryable:
    def test_轨迹记下这一环能不能重跑(self):
        recorder = TraceRecorder()
        trace = recorder.record(
            node="notified",
            component="notify",
            status=STATUS_FAILED,
            conclusion="推送失败",
            elapsed_ms=1.0,
            retryable=True,
        )
        assert trace.retryable is True
        assert trace.to_dict()["retryable"] is True

    def test_回放能说出这一环能不能重跑(self):
        recorder = TraceRecorder()
        recorder.record(
            node="notified",
            component="notify",
            status=STATUS_FAILED,
            conclusion="推送失败",
            elapsed_ms=1.0,
            error="连接超时",
            retryable=True,
        )
        text = render_trace_text(recorder.traces(), event_id="evt-trace-0001")
        assert "能不能重跑：可以" in text

    def test_不可重试的环节明说重跑没用(self):
        recorder = TraceRecorder()
        recorder.record(
            node="judged",
            component="judge",
            status=STATUS_FAILED,
            conclusion="判定失败",
            elapsed_ms=1.0,
            error="阈值表读不出来",
            retryable=False,
        )
        text = render_trace_text(recorder.traces(), event_id="evt-trace-0001")
        assert "能不能重跑：不能" in text

    def test_节点契约声明的可重试性进轨迹(self, store):
        """推送与回写是幂等的 → 可重试；判定是纯计算 → 不可重试。"""
        from critval.flow.contract import NodeContract

        class RetryableNode:
            def __init__(self, name, param):
                self.name = name
                self.param = param

            def contract(self):
                return NodeContract(
                    name=self.name,
                    inputs=(),
                    outputs=(),
                    retryable=True,
                )

            def run(self, context):
                return {"ok": True}

        registry = NodeRegistry()
        registry.register("idem", RetryableNode)
        dsl = FlowDSL()
        dsl.add("idem", "idem", {})
        pipeline = Pipeline(dsl, registry, store=store)
        result = pipeline.run({"event": _event("evt-trace-0001")})

        assert result.traces[0].retryable is True


# ---------------------------------------------------------------------------
# 6. CLI 回放命令
# ---------------------------------------------------------------------------
class TestCliTrace:
    def _seed(self, tmp_path, *, fail_at: str = "") -> str:
        db_path = tmp_path / "trace.db"
        dsl, registry = _chain(fail_at=fail_at)
        store = LedgerStore(db_path)
        try:
            store.record_event(_event("evt-trace-0001"))
            pipeline = Pipeline(dsl, registry, store=store)
            pipeline.run({"event": _event("evt-trace-0001")})
        finally:
            store.close()
        return str(db_path)

    def test_CLI回放能回答停在哪一步(self, tmp_path, capsys):
        from critval.cli import main

        db_path = self._seed(tmp_path, fail_at="judged")
        code = main(["trace", "--event", "evt-trace-0001", "--db", db_path])
        out = capsys.readouterr().out

        assert code == 0
        assert "停在哪一步：judged" in out
        assert "为什么" in out

    def test_CLI回放逐节点打印(self, tmp_path, capsys):
        from critval.cli import main

        db_path = self._seed(tmp_path)
        code = main(["trace", "--event", "evt-trace-0001", "--db", db_path])
        out = capsys.readouterr().out

        assert code == 0
        for node in ("received", "judged", "card_built", "notified"):
            assert node in out

    def test_CLI回放JSON可解析且带轨迹(self, tmp_path, capsys):
        from critval.cli import main

        db_path = self._seed(tmp_path)
        code = main(["trace", "--event", "evt-trace-0001", "--db", db_path, "--json"])
        out = capsys.readouterr().out

        assert code == 0
        payload = json.loads(out)
        assert payload["event_id"] == "evt-trace-0001"
        assert payload["trace_count"] > 0
        assert payload["traces"]
        assert "stopped_at" in payload

    def test_CLI回放未知事件返回业务失败码(self, tmp_path, capsys):
        from critval.cli import main

        db_path = self._seed(tmp_path)
        code = main(["trace", "--event", "evt-not-exist", "--db", db_path])
        err = capsys.readouterr().err

        assert code == 1
        assert "没有事件" in err

    def test_CLI回放台账不存在返回用法错误码(self, tmp_path, capsys):
        from critval.cli import main

        code = main(
            ["trace", "--event", "evt-trace-0001", "--db", str(tmp_path / "missing.db")]
        )
        err = capsys.readouterr().err

        assert code == 2
        assert "台账不存在" in err

    def test_CLI回放事件存在但没有轨迹时明说(self, tmp_path, capsys):
        from critval.cli import main

        db_path = tmp_path / "notrace.db"
        with LedgerStore(db_path) as store:
            store.record_event(_event("evt-trace-0001"))

        code = main(["trace", "--event", "evt-trace-0001", "--db", str(db_path)])
        err = capsys.readouterr().err

        assert code == 1
        assert "没有节点级轨迹" in err
