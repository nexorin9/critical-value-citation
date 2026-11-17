"""公开检验参考区间与危急值阈值样例集（示例数据层）。

数据性质：常见检验项目的**公开文献整理**参考区间与危急值阈值，用于演示
阈值表解析与危急值判定；**院内上线必须以本院危急值标准文件为准**。

每条记录带来源标注：
- ``public-literature``：公开文献整理（用于阈值表主体）
- ``hl7-fhir-example``：HL7 官方发布的 FHIR R4 Observation 示例中给出的 referenceRange

本模块只提供"种子表"；fetch_data.py 会把它与抓取到的公开样例合并后写入
``data/raw/reference_intervals.csv``，供 build_materials.py / gen_synth.py 消费。
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# 科室（用于 PV1-7 所在科室与制度语料的场景化）
# ---------------------------------------------------------------------------
DEPARTMENTS = [
    {"code": "CARD", "name": "心脏内科"},
    {"code": "NEUR", "name": "神经内科"},
    {"code": "RESP", "name": "呼吸内科"},
    {"code": "SURG", "name": "普通外科"},
    {"code": "ORTH", "name": "骨科"},
    {"code": "ICU", "name": "重症医学科"},
    {"code": "EMER", "name": "急诊科"},
    {"code": "HEMA", "name": "血液内科病区"},
]

# ---------------------------------------------------------------------------
# 检验项目目录
#   code        LOINC 项目码（阈值表主键）
#   unit        UCUM 风格单位；单位换算在 judge.units 层处理
#   ref_low/high 参考区间上下界；crit_low/high 危急值阈值（None 表示该方向不设）
# ---------------------------------------------------------------------------
ANALYTES = [
    {"code": "718-7", "name_zh": "血红蛋白", "name_en": "Hemoglobin", "unit": "g/L",
     "ref_low": 115.0, "ref_high": 150.0, "crit_low": 30.0, "crit_high": 200.0,
     "source": "public-literature", "source_note": "成人全血常见参考区间与危急值"},
    {"code": "789-8", "name_zh": "红细胞压积", "name_en": "Hematocrit", "unit": "%",
     "ref_low": 40.0, "ref_high": 50.0, "crit_low": 20.0, "crit_high": 60.0,
     "source": "public-literature", "source_note": "成人全血常见参考区间与危急值"},
    {"code": "6690-2", "name_zh": "白细胞计数", "name_en": "Leukocytes", "unit": "10*9/L",
     "ref_low": 3.5, "ref_high": 9.5, "crit_low": 1.5, "crit_high": 30.0,
     "source": "public-literature", "source_note": "成人全血常见参考区间与危急值"},
    {"code": "777-3", "name_zh": "血小板计数", "name_en": "Platelets", "unit": "10*9/L",
     "ref_low": 125.0, "ref_high": 350.0, "crit_low": 20.0, "crit_high": 1000.0,
     "source": "public-literature", "source_note": "成人全血常见参考区间与危急值"},
    {"code": "2823-3", "name_zh": "钾", "name_en": "Potassium", "unit": "mmol/L",
     "ref_low": 3.5, "ref_high": 5.3, "crit_low": 2.8, "crit_high": 6.5,
     "source": "public-literature", "source_note": "成人血清常见参考区间与危急值"},
    {"code": "2951-2", "name_zh": "钠", "name_en": "Sodium", "unit": "mmol/L",
     "ref_low": 137.0, "ref_high": 147.0, "crit_low": 120.0, "crit_high": 160.0,
     "source": "public-literature", "source_note": "成人血清常见参考区间与危急值"},
    {"code": "2075-0", "name_zh": "氯", "name_en": "Chloride", "unit": "mmol/L",
     "ref_low": 99.0, "ref_high": 110.0, "crit_low": 80.0, "crit_high": 125.0,
     "source": "public-literature", "source_note": "成人血清常见参考区间与危急值"},
    {"code": "17861-6", "name_zh": "钙", "name_en": "Calcium", "unit": "mmol/L",
     "ref_low": 2.11, "ref_high": 2.52, "crit_low": 1.5, "crit_high": 3.5,
     "source": "public-literature", "source_note": "成人血清常见参考区间与危急值"},
    {"code": "2601-3", "name_zh": "镁", "name_en": "Magnesium", "unit": "mmol/L",
     "ref_low": 0.75, "ref_high": 1.0, "crit_low": 0.4, "crit_high": 2.5,
     "source": "public-literature", "source_note": "成人血清常见参考区间与危急值"},
    {"code": "2345-7", "name_zh": "葡萄糖", "name_en": "Glucose", "unit": "mmol/L",
     "ref_low": 3.9, "ref_high": 6.1, "crit_low": 2.8, "crit_high": 22.2,
     "source": "public-literature", "source_note": "成人血清常见参考区间与危急值"},
    {"code": "2160-0", "name_zh": "肌酐", "name_en": "Creatinine", "unit": "umol/L",
     "ref_low": 44.0, "ref_high": 133.0, "crit_low": None, "crit_high": 707.0,
     "source": "public-literature", "source_note": "成人血清常见参考区间与危急值"},
    {"code": "3094-0", "name_zh": "尿素氮", "name_en": "Urea nitrogen", "unit": "mmol/L",
     "ref_low": 2.9, "ref_high": 8.2, "crit_low": None, "crit_high": 35.7,
     "source": "public-literature", "source_note": "成人血清常见参考区间与危急值"},
    {"code": "6768-6", "name_zh": "淀粉酶", "name_en": "Amylase", "unit": "U/L",
     "ref_low": 30.0, "ref_high": 110.0, "crit_low": None, "crit_high": 1000.0,
     "source": "public-literature", "source_note": "成人血清常见参考区间与危急值"},
    {"code": "1742-6", "name_zh": "丙氨酸氨基转移酶", "name_en": "Alanine aminotransferase", "unit": "U/L",
     "ref_low": 7.0, "ref_high": 40.0, "crit_low": None, "crit_high": 1000.0,
     "source": "public-literature", "source_note": "成人血清常见参考区间与危急值"},
    {"code": "1920-8", "name_zh": "天门冬氨酸氨基转移酶", "name_en": "Aspartate aminotransferase", "unit": "U/L",
     "ref_low": 13.0, "ref_high": 35.0, "crit_low": None, "crit_high": 1000.0,
     "source": "public-literature", "source_note": "成人血清常见参考区间与危急值"},
    {"code": "1975-2", "name_zh": "总胆红素", "name_en": "Bilirubin.total", "unit": "umol/L",
     "ref_low": 3.4, "ref_high": 20.5, "crit_low": None, "crit_high": 342.0,
     "source": "public-literature", "source_note": "成人血清常见参考区间与危急值"},
    {"code": "2157-6", "name_zh": "肌酸激酶", "name_en": "Creatine kinase", "unit": "U/L",
     "ref_low": 25.0, "ref_high": 200.0, "crit_low": None, "crit_high": 1000.0,
     "source": "public-literature", "source_note": "成人血清常见参考区间与危急值"},
    {"code": "10839-9", "name_zh": "肌红蛋白", "name_en": "Myoglobin", "unit": "ng/mL",
     "ref_low": 25.0, "ref_high": 72.0, "crit_low": None, "crit_high": 500.0,
     "source": "public-literature", "source_note": "成人血清常见参考区间与危急值"},
    {"code": "10841-5", "name_zh": "肌钙蛋白I", "name_en": "Troponin I", "unit": "ng/mL",
     "ref_low": 0.0, "ref_high": 0.04, "crit_low": None, "crit_high": 0.5,
     "source": "public-literature", "source_note": "成人血清常见参考区间与危急值"},
    {"code": "5902-2", "name_zh": "凝血酶原时间", "name_en": "Prothrombin time", "unit": "s",
     "ref_low": 11.0, "ref_high": 14.0, "crit_low": 6.0, "crit_high": 40.0,
     "source": "public-literature", "source_note": "成人血浆常见参考区间与危急值"},
    {"code": "14982-3", "name_zh": "活化部分凝血活酶时间", "name_en": "Activated partial thromboplastin time", "unit": "s",
     "ref_low": 28.0, "ref_high": 42.0, "crit_low": 15.0, "crit_high": 100.0,
     "source": "public-literature", "source_note": "成人血浆常见参考区间与危急值"},
    {"code": "3255-7", "name_zh": "纤维蛋白原", "name_en": "Fibrinogen", "unit": "g/L",
     "ref_low": 2.0, "ref_high": 4.0, "crit_low": 1.0, "crit_high": 8.0,
     "source": "public-literature", "source_note": "成人血浆常见参考区间与危急值"},
    {"code": "8480-6", "name_zh": "收缩压", "name_en": "Systolic blood pressure", "unit": "mm[Hg]",
     "ref_low": 90.0, "ref_high": 140.0, "crit_low": 70.0, "crit_high": 200.0,
     "source": "public-literature", "source_note": "成人静息血压常见参考区间"},
    {"code": "8867-4", "name_zh": "心率", "name_en": "Heart rate", "unit": "/min",
     "ref_low": 60.0, "ref_high": 100.0, "crit_low": 40.0, "crit_high": 150.0,
     "source": "public-literature", "source_note": "成人静息心率常见参考区间"},
    {"code": "2708-6", "name_zh": "血氧饱和度", "name_en": "Oxygen saturation", "unit": "%",
     "ref_low": 95.0, "ref_high": 100.0, "crit_low": 90.0, "crit_high": None,
     "source": "public-literature", "source_note": "成人动脉血氧饱和度常见参考区间"},
    {"code": "8331-1", "name_zh": "体温", "name_en": "Body temperature", "unit": "Cel",
     "ref_low": 36.0, "ref_high": 37.2, "crit_low": 35.0, "crit_high": 40.0,
     "source": "public-literature", "source_note": "成人腋温常见参考区间"},
    {"code": "26499-4", "name_zh": "C反应蛋白", "name_en": "C-reactive protein", "unit": "mg/L",
     "ref_low": 0.0, "ref_high": 8.0, "crit_low": None, "crit_high": 100.0,
     "source": "public-literature", "source_note": "成人血清常见参考区间与危急值"},
]

# ---------------------------------------------------------------------------
# 制度条款模板（用于生成样例制度 docx 与大语料 policy_corpus.txt）
#   每条条款含「通知对象 / 上报时限 / 记录去向」三个可抽取要素，
#   格式固定为 "通知对象：…；上报时限：…；记录去向：…"，供制度解析任务抽取。
# ---------------------------------------------------------------------------
NOTICE_TARGETS = [
    "检验科值班技师与检验科组长",
    "检验科值班技师、临床科室值班护士长",
    "检验科值班技师、临床科室值班医师",
    "检验科值班技师、临床科室护士长与科室主任",
    "检验科值班技师、临床科室值班护士长、医务科值班人员",
]

REPORT_DEADLINES = [
    "10 分钟内",
    "15 分钟内",
    "20 分钟内",
    "30 分钟内",
]

RECORD_DESTINATIONS = [
    "危急值登记系统，并同步记入患者病程记录",
    "危急值登记系统，并同步记入护理交接班记录",
    "危急值登记系统，并同步记入科室交班本",
    "危急值登记系统，并同步记入检验科危急值台账",
]

POLICY_CHAPTERS = [
    "第一章 总则",
    "第二章 危急值项目与阈值",
    "第三章 报告流程与通知时限",
    "第四章 记录与登记",
    "第五章 责任与考核",
]

# 条款主题句式（与项目、科室、时限组合构成语料）
CLAUSE_TEMPLATES = [
    "{name}（{code}）检验结果低于危急值下限 {crit_low} {unit} 或高于危急值上限 {crit_high} {unit} 时，"
    "检验科须在 {deadline} 发出危急值报告并通知 {dept}。",
    "{dept}患者 {name}（{code}）结果触发危急值判定时，检验科值班技师应当班复核并在 {deadline} 完成报告确认，"
    "确认后通知 {target}。",
    "对 {dept}送检标本中 {name}（{code}）的危急值，检验科应在 {deadline} 内通知 {target}，"
    "并同步完成登记。",
    "{name}（{code}）危急值确认后，{target}应当在收到报告后 {deadline} 内到岗处置，"
    "处置情况由检验科登记。",
    "若 {dept}对 {name}（{code}）危急值处置未在 {deadline} 内响应，检验科组长有权电话追催并记入交班记录。",
]


def analyte_index() -> dict:
    """项目码 → 项目字典。"""
    return {item["code"]: item for item in ANALYTES}


def reference_range_text(item: dict) -> str:
    """参考区间文本形态：115-150 / <0.04 / >100。"""
    low = item.get("ref_low")
    high = item.get("ref_high")
    if low is not None and high is not None:
        return "{:g}-{:g}".format(float(low), float(high))
    if low is not None:
        return "<{:g}".format(float(low))
    if high is not None:
        return ">{:g}".format(float(high))
    return ""


def direction(item: dict) -> str:
    """危急方向：both / low / high。"""
    has_low = item.get("crit_low") is not None
    has_high = item.get("crit_high") is not None
    if has_low and has_high:
        return "both"
    if has_low:
        return "low"
    if has_high:
        return "high"
    return "none"


def _as_float(text):
    if text is None:
        return None
    text = str(text).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def load_specs(csv_path=None) -> list:
    """读取合并后的参考区间样例集（data/raw/reference_intervals.csv）。

    文件缺失时回退到本模块内置的公开文献整理种子表，保证下游永远拿到同一套
    项目码 / 单位 / 危急阈值口径。返回的字典同时带 ref_text（参考区间文本形态）。
    """
    import csv as _csv
    from pathlib import Path as _Path

    rows = []
    if csv_path is not None:
        path = _Path(csv_path)
        if path.exists():
            with open(path, encoding="utf-8", newline="") as handle:
                rows = list(_csv.DictReader(handle))

    if not rows:
        specs = []
        for item in ANALYTES:
            spec = dict(item)
            spec["ref_text"] = reference_range_text(spec)
            spec["direction"] = direction(spec)
            specs.append(spec)
        return specs

    specs = []
    for row in rows:
        code = (row.get("项目码") or row.get("code") or "").strip()
        if not code:
            continue
        spec = {
            "code": code,
            "name_zh": (row.get("项目名") or row.get("name_zh") or "").strip(),
            "name_en": (row.get("英文名") or row.get("name_en") or "").strip(),
            "unit": (row.get("单位") or row.get("unit") or "").strip(),
            "ref_low": _as_float(row.get("参考区间下界")),
            "ref_high": _as_float(row.get("参考区间上界")),
            "ref_text": (row.get("参考区间") or row.get("ref_text") or "").strip(),
            "crit_low": _as_float(row.get("低危急值")),
            "crit_high": _as_float(row.get("高危急值")),
            "source": (row.get("来源") or row.get("source") or "").strip(),
            "source_note": (row.get("来源备注") or row.get("source_note") or "").strip(),
        }
        if not spec["ref_text"]:
            spec["ref_text"] = reference_range_text(spec)
        spec["direction"] = direction(spec)
        specs.append(spec)
    return specs
