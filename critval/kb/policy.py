"""院内危急值报告制度解析（docx / pdf）→ 条款级条目。

制度是「通知对象 / 上报时限 / 记录去向」的权威依据。处置卡上这三项
每一条都要能回答「按哪份制度的哪一条」—— 所以每条条款都带**出处五件套**：

    source_sha256 + 段落序号 + 章节路径 + 可读行号 + 文件版本

解析口径与阈值表解析同一套原则：

* **抽不到就说缺项**，不编造 —— 三项事实只写了一半的条款标「字段缺失」
  并进解析报告；完全没写三项事实的（总则类条款）不算缺项；
* **读不出来的段落不静默跳过** —— 空正文条款、含要素却无条款号的段落
  都进解析报告待人工确认；
* **docx 与 pdf 共用一份实现** —— 两种载体只是「怎么拿到段落」不同，
  条款识别与事实抽取只有一份代码（与 ``critval/hl7/normalize.py``
  同样的「一份规范化实现」纪律）。

版本与生效期：制度文件头部的「版本：… 生效日期：…」读成版本号与生效期，
按观察时刻选生效版本；历史版本**保留**（回溯当时依据时要用），
落在版本空档不取就近版本 —— 与阈值表口径一致。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Any, Optional, Sequence

from critval.kb.schema import POLICY_LABELS, parse_date
from critval.kb.version import ParseReport, sha256_file
from critval.models import Citation, PolicyClause

__all__ = [
    "FACT_LABELS",
    "PolicyDependencyMissing",
    "PolicyDocument",
    "PolicyEntry",
    "PolicyVersionSelection",
    "load_policy_document",
    "parse_policy_entries",
    "select_policy_version",
]

#: 三项事实字段（处置卡四要素里的三项来自制度）。
FACT_FIELDS: tuple[str, ...] = ("notify_target", "deadline", "record_destination")

#: 事实字段 → 人读标签（CLI 与变更说明里给中文，不给英文字段名）。
FACT_LABELS: dict[str, str] = {
    "notify_target": "通知对象",
    "deadline": "上报时限",
    "record_destination": "记录去向",
}

#: 条款编号：``第X条`` / ``（一）`` / ``1、`` / ``1.`` —— 制度里常见的几种写法。
_CLAUSE_MARKER_RE = re.compile(
    r"^\s*(第[一二三四五六七八九十百千零〇两0-9]+条"
    r"|[（(][一二三四五六七八九十0-9]+[)）]"
    r"|\d+[、.．)）])"
)

#: 项目码（LOINC 形态 ``718-7``）—— 条款正文里点名的检验项目。
#: 前后不许再跟数字：``2026-01`` 这类日期不会被当成项目码。
_ITEM_CODE_RE = re.compile(r"(?<!\d)(\d{3,5}-\d)(?!\d)")

#: 条款正文中「项目名（项目码）」的写法。
_ITEM_NAME_RE = re.compile(r"([一-鿿A-Za-z]{2,12})\s*[（(]\s*(\d{3,5}-\d)\s*[）)]")

#: 文件头部的版本 / 生效日期 / 失效日期。
_VERSION_RE = re.compile(
    r"版本[：:]\s*(.+?)(?=[　；;，,]|\s+(?:生效日期|编制部门|说明|发布日期)|$)"
)
_EFFECTIVE_FROM_RE = re.compile(
    r"生效日期[：:]\s*([0-9]{4}[-/年.][0-9]{1,2}[-/月.][0-9]{1,2}日?)"
)
_EFFECTIVE_TO_RE = re.compile(
    r"失效日期[：:]\s*([0-9]{4}[-/年.][0-9]{1,2}[-/月.][0-9]{1,2}日?)"
)

#: 「10 分钟内」里的分钟数。
_MINUTES_RE = re.compile(r"(\d+)\s*分钟")

#: 归一化正文（算稳定标识用）：去掉空白与标点，只留实义字符。
_NORMALIZE_RE = re.compile(r"[\s　，。；;、:：()（）【】\[\]“”\"'‘’]+")


def _label_pattern(aliases: Sequence[str]) -> re.Pattern[str]:
    """按字段别名拼出「标签：取值」的匹配式。

    取值到 ``；`` / ``。`` / 换行为止 —— 制度里三项事实用分号隔开。
    """
    parts = "|".join(re.escape(alias) for alias in aliases)
    return re.compile(rf"(?:{parts})\s*[：:]\s*([^；;。\n]+)")


#: 字段 → 匹配式（别名表来自 ``critval.kb.schema.POLICY_LABELS``）。
_LABEL_PATTERNS: dict[str, re.Pattern[str]] = {
    key: _label_pattern(aliases) for key, aliases in POLICY_LABELS.items()
}


class PolicyDependencyMissing(RuntimeError):
    """制度解析依赖缺失（python-docx / pypdf 未安装）。

    **显式降级**：解析器不会静默返回「空制度」，而是把缺失原因透出来，
    由调用方决定是降级还是中断。
    """


@dataclass(frozen=True)
class PolicyEntry:
    """制度里的一段（docx 段落 / pdf 行）。"""

    index: int  # 1 起的可读序号（段落序号 / PDF 行号）
    text: str
    level: int = 0  # 0 = 正文段；1 = 章标题；2 = 节标题


@dataclass
class PolicyDocument:
    """一份制度文件的解析结果：条款 + 解析报告 + 版本与生效期。"""

    source_name: str
    source_sha256: str = ""
    clauses: tuple[PolicyClause, ...] = ()
    report: Optional[ParseReport] = None
    source_path: str = ""
    version: str = ""
    effective_from: Optional[date] = None
    effective_to: Optional[date] = None
    #: 解析降级标记（依赖缺失等）。降级时条款数为 0，但原因一定写明。
    degraded: bool = False
    degrade_reason: str = ""

    @property
    def ok(self) -> bool:
        """这份制度是否读全了（没有未识别行、没有降级）。"""
        if self.degraded:
            return False
        return bool(self.report) and self.report.ok

    def versions(self) -> tuple[str, ...]:
        return (self.version,) if self.version else ()

    def clauses_of_version(self, version: str) -> tuple[PolicyClause, ...]:
        return tuple(
            clause
            for clause in self.clauses
            if (clause.citation.file_version or "") == version
        )

    def clause_by_id(self) -> dict[str, PolicyClause]:
        """按稳定标识索引条款（改版 diff 用）。"""
        return {clause.stable_id: clause for clause in self.clauses}

    def clauses_for_item(self, item_code: str) -> tuple[PolicyClause, ...]:
        """点名了某个检验项目码的条款。"""
        return tuple(clause for clause in self.clauses if item_code in clause.item_codes)

    def summary_text(self) -> str:
        """人读摘要（CLI 与解析报告用同一套口径）。"""
        report = self.report
        head = f"{self.source_name}"
        if self.version:
            head += f" · 版本 {self.version}"
        if self.effective_from:
            head += f"（生效 {self.effective_from.isoformat()}）"
        if self.degraded:
            head += f" · 解析降级：{self.degrade_reason}"
            return head
        if report is None:
            return head
        return (
            f"{head} · 共 {report.total_rows} 段 · 识别 {report.recognized_rows} 条条款"
            f" · 未识别 {len(report.unrecognized_rows)} 段"
            f" · 字段缺失 {len(report.incomplete_rows)} 条"
            f" · 未覆盖事实字段 {len(report.unrecognized_columns)} 个"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_name": self.source_name,
            "source_sha256": self.source_sha256,
            "source_path": self.source_path,
            "version": self.version,
            "effective_from": self.effective_from.isoformat() if self.effective_from else None,
            "effective_to": self.effective_to.isoformat() if self.effective_to else None,
            "degraded": self.degraded,
            "degrade_reason": self.degrade_reason,
            "clause_count": len(self.clauses),
            "clauses": [clause.to_dict() for clause in self.clauses],
            "report": self.report.to_dict() if self.report else None,
        }


def _normalize_body(text: str) -> str:
    """归一化正文（算稳定标识用）。"""
    return _NORMALIZE_RE.sub("", text)


def _body_digest(text: str) -> str:
    """无条款号条款的稳定标识（正文摘要）。"""
    return "body:" + hashlib.sha1(_normalize_body(text).encode("utf-8")).hexdigest()[:12]


def _extract_facts(text: str) -> dict[str, str]:
    """抽三项事实字段；抽不到就不给（**不编造**）。"""
    facts: dict[str, str] = {}
    for key, pattern in _LABEL_PATTERNS.items():
        match = pattern.search(text)
        if match:
            value = match.group(1).strip()
            if value:
                facts[key] = value
    return facts


def _extract_item_codes(text: str) -> tuple[str, ...]:
    """条款正文里点名的检验项目码（去重、保序）。"""
    seen: list[str] = []
    for code in _ITEM_CODE_RE.findall(text):
        if code not in seen:
            seen.append(code)
    return tuple(seen)


def _extract_item_names(text: str) -> tuple[str, ...]:
    """条款正文里「项目名（项目码）」的项目名（去重、保序）。"""
    seen: list[str] = []
    for name, _code in _ITEM_NAME_RE.findall(text):
        if name not in seen:
            seen.append(name)
    return tuple(seen)


def _scan_metadata(entries: Sequence[PolicyEntry]) -> tuple[str, Optional[date], Optional[date]]:
    """先扫一遍头部元信息：版本号 + 生效日期 + 失效日期。

    先扫后用，是因为元信息行未必排在条款前面 —— 制度模板把版本写在
    落款处是很常见的。条款的出处要带文件版本号，所以必须先扫出来。
    """
    version = ""
    effective_from: Optional[date] = None
    effective_to: Optional[date] = None
    for entry in entries:
        text = entry.text
        if not version:
            match = _VERSION_RE.search(text)
            if match:
                version = match.group(1).strip()
        if effective_from is None:
            match = _EFFECTIVE_FROM_RE.search(text)
            if match:
                effective_from = parse_date(match.group(1))
        if effective_to is None:
            match = _EFFECTIVE_TO_RE.search(text)
            if match:
                effective_to = parse_date(match.group(1))
    return version, effective_from, effective_to


def _assign_stable_ids(clauses: Sequence[PolicyClause]) -> tuple[PolicyClause, ...]:
    """给每条条款算**稳定标识**（改版 diff 的配对键）。

    * 条款号在全文唯一 → 直接用条款号；
    * 条款号重复（有的制度按项目分条写，条号会重号）→ 用
      ``条款号#项目码`` 区分；条款里没点名项目时退回 ``条款号#出现序号``；
    * 没有条款号 → 用正文摘要（内容改了标识就变，属于「删一条加一条」）。
    """
    counts: dict[str, int] = {}
    for clause in clauses:
        if clause.clause_no:
            counts[clause.clause_no] = counts.get(clause.clause_no, 0) + 1

    seen: dict[str, int] = {}
    result: list[PolicyClause] = []
    for clause in clauses:
        stable_id = ""
        if clause.clause_no:
            seen[clause.clause_no] = seen.get(clause.clause_no, 0) + 1
            occurrence = seen[clause.clause_no]
            if counts[clause.clause_no] == 1:
                stable_id = clause.clause_no
            elif clause.item_codes:
                stable_id = f"{clause.clause_no}#{clause.item_codes[0]}"
            else:
                stable_id = f"{clause.clause_no}#{occurrence}"
        else:
            stable_id = _body_digest(clause.body)
        result.append(replace(clause, stable_id=stable_id))
    return tuple(result)


def parse_policy_entries(
    entries: Sequence[PolicyEntry],
    source_name: str,
    source_sha256: str = "",
    source_path: str = "",
) -> PolicyDocument:
    """把「段落序列」解析成条款 + 解析报告（不碰文件，便于单测）。

    docx 与 pdf 都先转成 ``PolicyEntry`` 序列再走这里 —— 条款识别、
    事实抽取、稳定标识只有这一份实现。
    """
    report = ParseReport(
        source_name=source_name,
        source_sha256=source_sha256,
        source_path=source_path,
    )
    version, effective_from, effective_to = _scan_metadata(entries)
    report.file_version = version

    clauses: list[PolicyClause] = []
    section_stack: list[tuple[int, str]] = []
    seen_facts: set[str] = set()

    for entry in entries:
        text = entry.text.strip()
        if not text:
            continue
        report.total_rows += 1

        if entry.level > 0:
            # 标题段：维护章节路径（层级高的来了，低的出栈）。
            while section_stack and section_stack[-1][0] >= entry.level:
                section_stack.pop()
            section_stack.append((entry.level, text))
            continue

        marker = _CLAUSE_MARKER_RE.match(text)
        facts = _extract_facts(text)
        has_fact = bool(facts)

        if not marker:
            if has_fact:
                # 有制度要素却没有条款编号 —— 不能当作条款引用，但也不能丢。
                report.add_unrecognized(
                    row_no=entry.index,
                    reason="段落含制度要素但无条款编号，无法作为条款引用（待人工确认）",
                    values=(text,),
                )
            continue

        clause_no = marker.group(1).strip()
        body = text[marker.end():].strip()
        if not body:
            report.add_unrecognized(
                row_no=entry.index,
                reason=f"条款 {clause_no} 正文为空", values=(text,)
            )
            continue

        notify_target = facts.get("notify_target", "")
        deadline_text = facts.get("deadline", "")
        record_destination = facts.get("record_destination", "")
        missing: tuple[str, ...] = ()
        if facts and len(facts) < len(FACT_FIELDS):
            missing = tuple(field for field in FACT_FIELDS if field not in facts)
            report.add_incomplete(
                row_no=entry.index,
                reason="字段缺失：" + "、".join(missing),
                values=(text,),
            )
        seen_facts.update(facts)

        minutes: Optional[int] = None
        minute_match = _MINUTES_RE.search(deadline_text)
        if minute_match:
            minutes = int(minute_match.group(1))

        citation = Citation(
            source_name=source_name,
            source_sha256=source_sha256,
            kind="policy_clause",
            paragraph_no=entry.index,
            section_path=tuple(title for _level, title in section_stack),
            line_no=entry.index,
            file_version=version or None,
        )
        clauses.append(
            PolicyClause(
                section_title=section_stack[-1][1] if section_stack else "",
                body=text,
                notify_target=notify_target,
                deadline_text=deadline_text,
                deadline_minutes=minutes,
                record_destination=record_destination,
                citation=citation,
                clause_no=clause_no,
                item_codes=_extract_item_codes(text),
                item_names=_extract_item_names(text),
                missing_fields=missing,
            )
        )
        report.recognized_rows += 1

    # 全文都没出现的事实字段（等价于阈值表的「未识别列」）：制度没写，
    # 处置卡上的这一项就只能标缺项。
    report.unrecognized_columns = [field for field in FACT_FIELDS if field not in seen_facts]

    return PolicyDocument(
        source_name=source_name,
        source_sha256=source_sha256,
        clauses=_assign_stable_ids(clauses),
        report=report,
        source_path=source_path,
        version=version,
        effective_from=effective_from,
        effective_to=effective_to,
    )


def _read_docx_entries(source: Path) -> list[PolicyEntry]:
    """docx → 段落序列（标题层级取自 Word 的 Heading 样式）。"""
    try:
        from docx import Document
    except ImportError as exc:  # pragma: no cover - 环境问题
        raise PolicyDependencyMissing(
            "解析 docx 制度需要 python-docx，请先安装 requirements.txt 中的依赖"
        ) from exc

    document = Document(source)
    entries: list[PolicyEntry] = []
    for index, paragraph in enumerate(document.paragraphs, start=1):
        text = (paragraph.text or "").strip()
        if not text:
            continue
        style_name = ""
        try:
            if paragraph.style is not None:
                style_name = paragraph.style.name or ""
        except (AttributeError, KeyError):  # pragma: no cover - 样式缺失的怪文件
            style_name = ""
        level = 0
        if style_name.startswith("Heading"):
            tail = style_name.split()[-1]
            level = int(tail) if tail.isdigit() else 1
            level = min(level, 2)
        entries.append(PolicyEntry(index=index, text=text, level=level))
    return entries


def _read_pdf_entries(source: Path) -> list[PolicyEntry]:
    """pdf → 行序列（用 pypdf 抽文本，章节按「第X章 / 第X节」识别）。"""
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - 环境问题
        raise PolicyDependencyMissing(
            "解析 pdf 制度需要 pypdf，请先安装 requirements.txt 中的依赖"
        ) from exc

    reader = PdfReader(str(source))
    lines: list[str] = []
    for page in reader.pages:
        text = page.extract_text() or ""
        lines.extend(text.splitlines())

    entries: list[PolicyEntry] = []
    index = 0
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        index += 1
        level = 0
        if re.match(r"^\s*第[一二三四五六七八九十百千零〇两0-9]+章", line):
            level = 1
        elif re.match(r"^\s*第[一二三四五六七八九十百千零〇两0-9]+节", line):
            level = 2
        entries.append(PolicyEntry(index=index, text=line, level=level))
    return entries


def load_policy_document(path: str | Path) -> PolicyDocument:
    """读取一份制度文件（docx / pdf）→ 条款 + 解析报告。

    依赖缺失时**显式降级**：返回 ``degraded=True`` 的结果并写明原因，
    而不是静默返回「这份制度里没有条款」。
    """
    source = Path(path)
    if not source.exists():
        raise FileNotFoundError(f"制度文件不存在：{source}")

    digest = sha256_file(source)
    suffix = source.suffix.lower()
    if suffix == ".docx":
        reader = _read_docx_entries
    elif suffix == ".pdf":
        reader = _read_pdf_entries
    else:
        raise ValueError(f"不支持的制度文件类型：{source.name}（仅支持 .docx / .pdf）")

    try:
        entries = reader(source)
    except PolicyDependencyMissing as exc:
        return _degraded_document(source, digest, str(exc))

    document = parse_policy_entries(
        entries,
        source_name=source.name,
        source_sha256=digest,
        source_path=str(source),
    )
    return document


def load_policy_documents(paths: Sequence[str | Path]) -> tuple[PolicyDocument, ...]:
    """读取多份制度文件（新旧版本并存时用）。"""
    return tuple(load_policy_document(path) for path in paths)


def _degraded_document(
    source: Path,
    digest: str,
    reason: str,
) -> PolicyDocument:
    """构造一份「降级」结果：条款为空，但原因写在明处。"""
    report = ParseReport(
        source_name=source.name,
        source_sha256=digest,
        source_path=str(source),
    )
    report.add_unrecognized(row_no=0, reason=f"制度解析降级：{reason}")
    return PolicyDocument(
        source_name=source.name,
        source_sha256=digest,
        clauses=(),
        report=report,
        source_path=str(source),
        degraded=True,
        degrade_reason=reason,
    )


@dataclass(frozen=True)
class PolicyVersionSelection:
    """按观察时刻选生效制度版本的结论。

    ``status`` 与阈值表版本选取同口径：``selected`` / ``conflict``
    （口径互斥）/ ``gap``（版本空档）/ ``no_match``（没有可用版本）。
    后三种都必须转人工。
    """

    status: str
    version: str
    reason: str
    document: Optional[PolicyDocument] = None
    candidates: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.status == "selected" and self.document is not None

    @property
    def needs_human(self) -> bool:
        return not self.ok

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "version": self.version,
            "reason": self.reason,
            "candidates": list(self.candidates),
            "ok": self.ok,
            "needs_human": self.needs_human,
        }


def select_policy_version(
    documents: Sequence[PolicyDocument],
    at: date,
) -> PolicyVersionSelection:
    """按观察时刻选生效的制度版本。

    口径与阈值表版本选取一致：

    * 唯一一版覆盖 ``at`` → 选中；
    * 多版同时覆盖 → **口径互斥**，拒绝自动引用（转人工）；
    * 上一版已失效、下一版还没生效 → **版本空档**，不取就近版本；
    * 制度文件没标版本号 → 无法确认生效版本（转人工）。
    """
    versioned = [doc for doc in documents if doc.version]
    candidates = tuple(doc.version for doc in versioned)
    if not versioned:
        return PolicyVersionSelection(
            status="no_match",
            version="",
            reason="制度文件未标注版本号，无法确认生效版本（转人工）",
            candidates=(),
        )

    covering = tuple(
        doc
        for doc in versioned
        if (doc.effective_from is None or doc.effective_from <= at)
        and (doc.effective_to is None or doc.effective_to >= at)
    )
    if len(covering) == 1:
        document = covering[0]
        period = (
            f"{document.effective_from.isoformat()} ~ "
            f"{document.effective_to.isoformat() if document.effective_to else '长期有效'}"
        )
        return PolicyVersionSelection(
            status="selected",
            version=document.version,
            reason=f"命中制度版本 {document.version}（生效期 {period}）",
            document=document,
            candidates=candidates,
        )
    if len(covering) > 1:
        detail = "、".join(f"{doc.version}（{doc.source_name}）" for doc in covering)
        return PolicyVersionSelection(
            status="conflict",
            version="",
            reason=(
                f"观察时刻 {at.isoformat()} 同时落在 {len(covering)} 个制度版本的生效期内，"
                f"口径互斥，拒绝自动引用（转人工）：{detail}"
            ),
            candidates=candidates,
        )

    ordered = sorted(
        versioned,
        key=lambda doc: (
            doc.effective_from or date.min,
            doc.effective_to or date.max,
            doc.version,
        ),
    )
    previous: Optional[PolicyDocument] = None
    for document in ordered:
        if document.effective_from is not None and document.effective_from <= at:
            previous = document
    if previous is not None and previous.effective_to is not None and at > previous.effective_to:
        following = next(
            (
                doc
                for doc in ordered
                if doc.effective_from is not None and doc.effective_from > previous.effective_to
            ),
            None,
        )
        if following is None:
            return PolicyVersionSelection(
                status="no_match",
                version="",
                reason=(
                    f"制度版本 {previous.version} 已于 {previous.effective_to.isoformat()} 失效，"
                    f"新版本未再发布（转人工）"
                ),
                candidates=candidates,
            )
        return PolicyVersionSelection(
            status="gap",
            version="",
            reason=(
                f"观察时刻 {at.isoformat()} 落在制度版本空档"
                f"（{previous.version} 于 {previous.effective_to.isoformat()} 失效，"
                f"{following.version} 于 {following.effective_from.isoformat()} 才生效），"
                f"不取就近版本，转人工"
            ),
            candidates=candidates,
        )

    drafts = "、".join(doc.version for doc in versioned)
    return PolicyVersionSelection(
        status="no_match",
        version="",
        reason=(
            f"观察时刻 {at.isoformat()} 早于所有制度版本的生效期"
            f"（现有版本：{drafts}），转人工"
        ),
        candidates=candidates,
    )
