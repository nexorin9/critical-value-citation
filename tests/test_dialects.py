"""HL7 厂商方言兼容矩阵单测。

覆盖 **8 个差异点**，每个至少一条用例：

1. 厂商私有 ``Z`` 段承载项目码；
2. ``OBX-3`` 只给中文项目名（走 ``OBR-4`` 兜底，标低置信）；
3. 标本号在标本段 ``SPM-2``；
4. 厂商自有日期格式；
5. 复合字段用冒号分隔组件；
6. 空 ``OBX`` 分组（跳过留痕 / 保留落 unparsed）；
7. ``OBX-8`` 用法不统一（整体忽略 / 词表归一 / 原样保留）；
8. 同名段多实例（空 ``OBR`` 不遮蔽后续 ``OBR``）。

外加几条通用规则：方言配置可从 JSON 加载、加载失败回落默认方言、
``OBX-8`` 永不作判定权威、时刻归一到墙钟时间、项目码形态可配置、
兜底链的适用范围、官方样例协议对拍差异为 0。
"""

from __future__ import annotations

import csv
import json
import subprocess
import sys
from pathlib import Path

import pytest

from critval.hl7.dialects import (
    BUILTIN_DIALECTS,
    DEFAULT_DIALECT,
    Dialect,
    DialectError,
    load_dialect,
    parse_oru_r01,
    resolve_dialect,
)
from critval.hl7.normalize import normalize_events, parse_oru_r01 as normalize_parse

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DIALECT_DIR = PROJECT_ROOT / "samples" / "dialects"
EXPECTED_FILE = DIALECT_DIR / "expected.json"
DIALECT_FRAMES = PROJECT_ROOT / "samples" / "hl7" / "方言样例"
OFFICIAL_FRAME = PROJECT_ROOT / "samples" / "hl7" / "官方样例" / "官方样例_ORU_R01_协议对拍.hl7"


def frame(name: str) -> str:
    return (DIALECT_FRAMES / name).read_text(encoding="utf-8")


def parse(name: str, dialect: str = "default"):
    return parse_oru_r01(frame(name), BUILTIN_DIALECTS[dialect])


# ---------------------------------------------------------------------------
# 配置加载
# ---------------------------------------------------------------------------


def test_方言配置可从JSON文件加载():
    dialect = load_dialect(DIALECT_DIR / "vendor_zseg.json")
    assert dialect.name == "vendor_zseg"
    assert dialect.z_item_code is not None
    assert dialect.z_item_code.segment == "ZLR"
    assert dialect.z_item_code.item_code_field == 2


def test_文件方言与内置方言逐字段一致():
    for path in sorted(DIALECT_DIR.glob("*.json")):
        if path.name == "expected.json":
            continue
        dialect = load_dialect(path)
        assert dialect.to_dict() == BUILTIN_DIALECTS[dialect.name].to_dict(), path.name


def test_按名字取内置方言按路径取文件方言():
    assert resolve_dialect("vendor_spm").name == "vendor_spm"
    assert resolve_dialect(str(DIALECT_DIR / "vendor_spm.json")).name == "vendor_spm"
    assert resolve_dialect(None).name == "default"


def test_方言配置非法时回落默认方言并告警(caplog):
    broken = DIALECT_DIR / "_临时_坏配置.json"
    broken.write_text("{ 这不是 JSON", encoding="utf-8")
    try:
        with caplog.at_level("WARNING"):
            dialect = resolve_dialect(str(broken))
        assert dialect.name == "default"
        assert any("回落" in record.message for record in caplog.records)
    finally:
        broken.unlink()


def test_字段引用写法不认识时报错():
    with pytest.raises(DialectError):
        Dialect.from_dict({"name": "x", "item_code": ["OBX_3"]})


def test_置信度与字段性质取值不认识时报错():
    with pytest.raises(DialectError):
        Dialect.from_dict({"name": "x", "item_code": [{"ref": "OBX-3.1", "confidence": "很高"}]})
    with pytest.raises(DialectError):
        Dialect.from_dict({"name": "x", "item_code": [{"ref": "OBX-3.1", "kind": "乱写"}]})


# ---------------------------------------------------------------------------
# 差异点 1：厂商私有 Z 段承载项目码
# ---------------------------------------------------------------------------


def test_厂商私有Z段承载项目码():
    message = parse("方言样例_厂商Z段.hl7", "vendor_zseg")
    observation = message.observations[0]
    assert observation.item_code == "718-7"
    assert observation.item_name == "血红蛋白"
    assert observation.item_code_source == "ZLR-2"
    assert observation.item_code_confidence == "medium"


def test_默认方言读不出私有Z段的项目码():
    message = parse("方言样例_厂商Z段.hl7", "default")
    assert message.observations[0].item_code == ""
    assert message.observations[0].item_code_source == ""


def test_私有Z段项目码缺失时该条结果落unparsed():
    message = parse("方言样例_厂商Z段.hl7", "vendor_zseg")
    events, unparsed = normalize_events(message)
    assert len(events) == 1
    assert unparsed == []
    assert events[0].item_code == "718-7"
    assert events[0].item_code_confidence == "medium"


# ---------------------------------------------------------------------------
# 差异点 2：OBX-3 只给中文项目名 → OBR-4 兜底 + 低置信
# ---------------------------------------------------------------------------


def test_项目码缺失时走OBR4兜底并标低置信():
    message = parse("方言样例_文本项目码.hl7", "default")
    observation = message.observations[0]
    assert observation.item_code == "6690-2"
    assert observation.item_code_source == "OBR-4.1"
    assert observation.item_code_confidence == "low"
    assert observation.item_name == "白细胞计数"
    assert any("低置信" in note for note in observation.notes)


def test_兜底来的项目码在事件上留痕():
    message = parse("方言样例_文本项目码.hl7", "default")
    events, unparsed = normalize_events(message)
    assert len(events) == 1
    assert unparsed == []
    assert events[0].item_code_source == "OBR-4.1"
    assert events[0].item_code_confidence == "low"


def test_中文项目名不当项目码用():
    """「白细胞计数」是名字，不是码 —— 当码用会把阈值表匹配到错的行。"""
    message = parse("方言样例_文本项目码.hl7", "default")
    assert "白细胞计数" != message.observations[0].item_code


def test_项目码形态可配置():
    """院内用中文当项目码时，把形态正则调宽即可（改配置不改代码）。"""
    dialect = Dialect.from_dict(
        {
            "name": "zh_code",
            "item_code": [{"ref": "OBX-3.1", "confidence": "high", "kind": "code"}],
            "item_code_pattern": "^[\\u4e00-\\u9fa5A-Za-z0-9._-]+$",
        }
    )
    message = parse_oru_r01(frame("方言样例_文本项目码.hl7"), dialect)
    assert message.observations[0].item_code == "白细胞计数"
    assert message.observations[0].item_code_confidence == "high"


# ---------------------------------------------------------------------------
# 差异点 3：标本号在标本段 SPM-2
# ---------------------------------------------------------------------------


def test_标本号在标本段时按SPM2取值():
    message = parse("方言样例_标本号SPM.hl7", "vendor_spm")
    assert message.specimen_no == "SPM-ORD-00000003"
    assert message.specimen_no_source == "SPM-2.1"
    assert message.specimen_no_confidence == "high"


def test_默认方言读不到标本号时回落SPM2并标低置信():
    message = parse("方言样例_标本号SPM.hl7", "default")
    assert message.specimen_no == "SPM-ORD-00000003"
    assert message.specimen_no_source == "SPM-2.1"
    assert message.specimen_no_confidence == "low"


def test_缺OBR3不回落OBR2():
    """OBR-2 是开单方申请号，与标本号未必同号 —— 不悄悄换一个号去绑标本。"""
    text = frame("方言样例_标本号SPM.hl7").replace(
        "VENDOR-REQ-00000003|", ""
    )
    message = parse_oru_r01(text, BUILTIN_DIALECTS["vendor_spm"])
    assert message.specimen_no == "SPM-ORD-00000003"


# ---------------------------------------------------------------------------
# 差异点 4：厂商自有日期格式
# ---------------------------------------------------------------------------


def test_厂商日期格式按配置集合解析():
    message = parse("方言样例_厂商日期格式.hl7", "vendor_legacy_date")
    observation = message.observations[0]
    assert observation.observed_at is not None
    assert observation.observed_at.strftime("%Y-%m-%d %H:%M:%S") == "2026-02-01 10:25:00"


def test_默认方言读不懂厂商日期时回落消息时间并留痕():
    message = parse("方言样例_厂商日期格式.hl7", "default")
    observation = message.observations[0]
    assert observation.observed_at is not None
    assert observation.observed_at.strftime("%Y-%m-%d %H:%M:%S") == "2026-02-01 10:12:00"
    assert any("回落消息时间" in note for note in observation.notes)


def test_不在日期格式集合内的值不解析成时间():
    """方言没配日期格式、链里也没有回落字段 → 观察时刻不可用，转人工。"""
    dialect = Dialect.from_dict(
        {
            "name": "no_dates",
            "message_control_id": [{"ref": "MSH-10"}],
            "specimen_no": [{"ref": "OBR-3.1"}],
            "item_code": [{"ref": "OBX-3.1", "kind": "code"}],
            "observed_at": [{"ref": "OBX-14"}],
        }
    )
    message = parse_oru_r01(frame("方言样例_厂商日期格式.hl7"), dialect)
    observation = message.observations[0]
    assert observation.observed_at is None
    assert any("不在方言配置的日期格式集合内" in note for note in observation.notes)
    events, unparsed = normalize_events(message)
    assert events == []
    assert unparsed[0].reason == "missing_observed_at"


# ---------------------------------------------------------------------------
# 差异点 5：复合字段用冒号分隔
# ---------------------------------------------------------------------------


def test_冒号分隔的复合字段按方言分隔符拆分():
    message = parse("方言样例_冒号组件.hl7", "vendor_alt_component")
    observation = message.observations[0]
    assert observation.item_code == "718-7"
    assert observation.item_name == "血红蛋白"
    assert observation.item_code_confidence == "high"


def test_默认方言不把冒号串当项目码():
    message = parse("方言样例_冒号组件.hl7", "default")
    observation = message.observations[0]
    assert observation.item_code == ""
    assert observation.item_name == "718-7:血红蛋白"


# ---------------------------------------------------------------------------
# 差异点 6：空 OBX 分组
# ---------------------------------------------------------------------------


def test_空OBX分组按默认策略跳过并留痕():
    message = parse("方言样例_空OBX分组.hl7", "default")
    assert len(message.observations) == 1
    assert any("空 OBX 分组" in note for note in message.notes)


def test_空OBX分组按keep策略保留并落unparsed():
    message = parse("方言样例_空OBX分组.hl7", "vendor_empty_obx")
    assert len(message.observations) == 2
    events, unparsed = normalize_events(message)
    assert len(events) == 1
    assert len(unparsed) == 1
    assert unparsed[0].reason == "missing_item_code"


# ---------------------------------------------------------------------------
# 差异点 7：OBX-8 用法不统一
# ---------------------------------------------------------------------------


def test_异常标志整体忽略时字段为空():
    message = parse("方言样例_异常标志词表.hl7", "vendor_obx8_ignore")
    assert message.observations[0].abnormal_flag == ""


def test_异常标志按词表归一():
    message = parse("方言样例_异常标志词表.hl7", "vendor_obx8_vocab")
    assert message.observations[0].abnormal_flag == "H"


def test_默认方言原样保留异常标志():
    message = parse("方言样例_异常标志词表.hl7", "default")
    assert message.observations[0].abnormal_flag == "HH"


def test_异常标志永不作判定权威():
    """OBX-8 只作筛选与交叉校验 —— 方言层不产出任何危急结论。"""
    message = parse("方言样例_厂商Z段.hl7", "vendor_zseg")
    observation = message.observations[0]
    assert observation.abnormal_flag == "H"
    payload = observation.to_dict()
    assert set(payload) == {
        "item_code",
        "item_name",
        "item_code_source",
        "item_code_confidence",
        "item_code_text",
        "item_code_text_source",
        "value_raw",
        "unit",
        "reference_range",
        "abnormal_flag",
        "result_status",
        "observed_at_raw",
        "set_id",
        "notes",
    }
    assert not any("危急" in str(value) for value in payload.values())


def test_异常标志取第一个组件而不是整段():
    """官方样例报文里 OBX-8 是编码型字段：A^Abnormal^HL70078 → A。"""
    message = parse_oru_r01(OFFICIAL_FRAME.read_text(encoding="utf-8"), DEFAULT_DIALECT)
    assert message.observations[0].abnormal_flag == "A"


# ---------------------------------------------------------------------------
# 差异点 8：同名段多实例
# ---------------------------------------------------------------------------


def test_空OBR段不遮蔽后续带数据的OBR():
    """官方样例报文里前一个 OBR 是空壳，标本号在第二个 OBR 里。"""
    message = parse_oru_r01(OFFICIAL_FRAME.read_text(encoding="utf-8"), DEFAULT_DIALECT)
    assert message.specimen_no == "LAB4432"
    assert message.specimen_no_source == "OBR-3.1"


# ---------------------------------------------------------------------------
# 通用规则
# ---------------------------------------------------------------------------


def test_科室按标准语义取PV1位置字段():
    """标准 HL7 里 PV1-3 是病人所在位置，PV1-7 是收治医生 —— 别把医生号当科室。"""
    message = parse_oru_r01(OFFICIAL_FRAME.read_text(encoding="utf-8"), DEFAULT_DIALECT)
    assert message.dept_code == "EMERG"


def test_带时区偏移的时刻归一到墙钟时间():
    """一条报文里 OBX-14（无偏移）与 MSH-7（带偏移）不能一个带时区一个不带。"""
    message = parse_oru_r01(OFFICIAL_FRAME.read_text(encoding="utf-8"), DEFAULT_DIALECT)
    for observation in message.observations:
        assert observation.observed_at is not None
        assert observation.observed_at.tzinfo is None


def test_兜底链在多结果报文里不启用():
    """订单级标识归属不到具体某一条结果 —— 宁可落 unparsed 也不猜。"""
    text = (
        "MSH|^~\\&|LIS|HOSP|CRITVAL|HOSP|20260201101200||ORU^R01|DUALMSG1|P|2.5.1\r"
        "OBR|1|REQ1|ORD1|6690-2^白细胞计数^6690-2||||||||||||||||||||||||||F\r"
        "OBX|1|NM||1|2.76|10*9/L|3.5-9.5|L|||F|||20260201101200\r"
        "OBX|2|NM||1|3.10|10*9/L|3.5-9.5|L|||F|||20260201101200\r"
    )
    message = parse_oru_r01(text, DEFAULT_DIALECT)
    assert all(observation.item_code == "" for observation in message.observations)
    events, unparsed = normalize_events(message)
    assert events == []
    assert len(unparsed) == 2


def test_空值字面量按方言配置归空():
    dialect = Dialect.from_dict(
        {
            "name": "nullish",
            "item_code": [{"ref": "OBX-3.1", "confidence": "high", "kind": "code"}],
            "empty_values": ["", "NULL", "空"],
        }
    )
    text = (
        "MSH|^~\\&|LIS|HOSP|CRITVAL|HOSP|20260201101200||ORU^R01|NULLMSG1|P|2.5.1\r"
        "OBR|1|REQ1|ORD1||||||||||||||||||||||||||F\r"
        "OBX|1|NM|NULL|1|2.76|10*9/L|3.5-9.5|L|||F|||20260201101200\r"
    )
    message = parse_oru_r01(text, dialect)
    assert message.observations[0].item_code == ""


def test_规范化入口与方言层结果一致():
    """``normalize.parse_oru_r01`` 只是方言层结果的转换，不自己取字段。"""
    raw = frame("方言样例_厂商Z段.hl7")
    direct = parse_oru_r01(raw, BUILTIN_DIALECTS["vendor_zseg"])
    through = normalize_parse(raw, BUILTIN_DIALECTS["vendor_zseg"])
    assert direct.to_dict()["observations"] == through.to_dict()["observations"]


# ---------------------------------------------------------------------------
# 官方样例协议对拍
# ---------------------------------------------------------------------------


def test_官方样例在默认方言下逐字段与期望一致():
    baseline = json.loads(EXPECTED_FILE.read_text(encoding="utf-8"))
    entry = baseline["官方样例_ORU_R01_协议对拍.hl7"]
    message = parse_oru_r01(OFFICIAL_FRAME.read_text(encoding="utf-8"), DEFAULT_DIALECT)
    first = message.observations[0]
    actual = {
        "message_control_id": message.message_control_id,
        "specimen_no": message.specimen_no,
        "specimen_no_source": message.specimen_no_source,
        "dept_code": message.dept_code,
        "result_status": message.result_status,
        "observation_count": len(message.observations),
        "item_codes": "|".join(obs.item_code for obs in message.observations),
        "first_item_code": first.item_code,
        "first_item_name": first.item_name,
        "first_value": first.value_raw,
        "first_unit": first.unit,
        "first_observed_at": first.observed_at.strftime("%Y-%m-%d %H:%M:%S"),
        "first_obx_flag": first.abnormal_flag,
        "first_result_status": first.result_status,
        "first_item_code_source": first.item_code_source,
        "first_item_code_confidence": first.item_code_confidence,
    }
    assert actual == {k: v for k, v in entry.items() if k in actual}


def test_对拍脚本输出对拍表且参照方言差异为零(tmp_path):
    out = tmp_path / "dialect_diff.csv"
    result = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "parse_matrix.py"),
            "--out",
            str(out),
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "参照方言逐字段比对] 差异 0 处" in result.stdout
    assert out.exists()
    with out.open(encoding="utf-8-sig") as handle:
        rows = list(csv.reader(handle))
    assert rows[0][:5] == ["帧", "参照方言", "对拍方言", "比对口径", "字段"]
    # 官方样例的默认方言必须与期望基线零差异
    official_rows = [
        row
        for row in rows[1:]
        if row[0] == "官方样例_ORU_R01_协议对拍.hl7" and row[1] == "default" and row[2] == "default"
    ]
    assert official_rows == []


# ---------------------------------------------------------------------------
# 低置信项目码要出现在处置卡上
# ---------------------------------------------------------------------------


def test_兜底匹配的项目码在处置卡上标为降级():
    """值班人必须看到「这条依据的项目码是兜底来的」—— 卡片不能装作一样可靠。"""
    from critval.card.builder import build_card
    from critval.judge.critical import judge_event
    from critval.kb.loader import load_threshold_table

    table = load_threshold_table(PROJECT_ROOT / "samples" / "threshold" / "示例危急值阈值表.xlsx")
    message = parse("方言样例_文本项目码.hl7", "default")
    events, _ = normalize_events(message)
    event = events[0]
    assert event.item_code_confidence == "low"

    judgement = judge_event(event, table.rows)
    card = build_card(event, judgement)

    basis = next(element for element in card.elements if element.key == "threshold_basis")
    assert basis.status == "degraded"
    assert basis.reason == "item_code_fallback"
    assert "OBR-4.1" in basis.note
    assert "低置信" in basis.note
