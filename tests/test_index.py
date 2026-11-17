"""索引层单测：中文分词 / BM25 倒排 / 结构化与版本索引 / 材料增量重建。

覆盖任务 id 6 的四组硬要求：

* **空索引** —— 没有文档时检索不报错，返回空；
* **重复条目** —— 同一个 ``doc_id`` 重复投递按覆盖处理，不重复计分；
* **失效版本不参与召回** —— 给定观察时刻，已失效 / 未生效的版本不出现在候选里；
* **增量重建** —— 材料内容没变不重建；变了只重建那一份；撤回的条目被摘除。
"""

from __future__ import annotations

import math
import time
from datetime import date
from pathlib import Path

import pytest

from critval.index.builder import MaterialIndex, MaterialSource
from critval.index.inverted import BM25Params, InvertedIndex
from critval.index.structured import IndexDocument, StructuredIndex, documents_from_fields
from critval.index.tokenizer import (
    SINGLE_CHAR_STOPWORDS,
    STOPWORDS,
    bigrams,
    is_stopword,
    normalize,
    tokenize,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_THRESHOLD = PROJECT_ROOT / "samples" / "threshold" / "示例危急值阈值表.xlsx"
SAMPLE_POLICY = PROJECT_ROOT / "samples" / "policy" / "示例危急值报告制度.docx"


# —— 分词 ——


def test_中文按双字切分():
    assert bigrams("检验科值班技师") == ["检验", "验科", "科值", "值班", "班技", "技师"]


def test_孤立单字汉字不丢():
    assert bigrams("钾") == ["钾"]
    assert "钾" in tokenize("钾 血")


def test_停用词被滤掉():
    tokens = tokenize("检验科须在 15 分钟内 发出危急值报告")
    assert "科须" not in tokens
    assert "须在" not in tokens
    assert "危急" in tokens
    assert "报告" in tokens


def test_项目码与单位整体保留():
    tokens = tokenize("血红蛋白（718-7）检验结果 30 g/L")
    assert "718-7" in tokens
    assert "g/l" in tokens
    assert "30" in tokens


def test_全角数字与括号归一():
    assert normalize("１５分钟（30）") == "15分钟(30)"
    assert "15" in tokenize("１５分钟")


def test_空文本返回空列表():
    assert tokenize("") == []
    assert bigrams("") == []


def test_停用词表不收录会影响判定的词():
    for word in ("危急", "通知", "登记", "记录", "值班", "阈值", "上限", "时限", "科室"):
        assert word not in STOPWORDS
        assert not is_stopword(word)


def test_单字停用词只作用于孤立单字():
    assert "的" in SINGLE_CHAR_STOPWORDS
    assert "的" not in STOPWORDS  # 双字停用词表里没有它
    assert is_stopword("的")
    # "的" 出现在双字串里时不被单独剔除
    assert "危急" in tokenize("危急值")


# —— BM25 倒排 ——


def test_空索引检索不报错():
    index = InvertedIndex()
    assert index.search(tokenize("血红蛋白危急值")) == []
    assert index.doc_count == 0
    assert index.average_length == 0.0
    assert index.stats()["term_count"] == 0


def test_bm25打分可手算核对():
    index = InvertedIndex(params=BM25Params(k1=1.5, b=0.75))
    index.add("d1", ["a", "b", "a"])
    index.add("d2", ["a", "c"])

    n = 2
    avgdl = 5 / 2
    idf_a = math.log(1.0 + (n - 2 + 0.5) / (2 + 0.5))
    tf = 2  # d1 里 "a" 出现两次
    dl = 3
    denom = tf + 1.5 * (1 - 0.75 + 0.75 * dl / avgdl)
    expected = idf_a * (tf * (1.5 + 1)) / denom

    assert index.score_document(["a"], "d1") == pytest.approx(expected)


def test_词频饱和项按公式生效():
    index = InvertedIndex()
    index.add("d1", ["a", "a", "a"])
    index.add("d2", ["a"])
    # 同一词在 d1 里出现 3 次：得分更高，但增益随 tf 饱和
    assert index.score_document(["a"], "d1") > index.score_document(["a"], "d2")


def test_重复条目按覆盖处理():
    index = InvertedIndex()
    index.add("d1", ["危急值", "登记"])
    first = index.score_document(["危急值"], "d1")
    replaced = index.add("d1", ["危急值", "登记"])
    second = index.score_document(["危急值"], "d1")

    assert replaced is True
    assert index.doc_count == 1
    assert index.total_length == 2  # 不是 4
    assert index.replacements == 1
    assert first == pytest.approx(second)


def test_移除后不再命中且统计量同步():
    index = InvertedIndex()
    index.add("d1", ["危急值", "登记"])
    index.add("d2", ["危急值"])
    assert index.remove("d1") is True
    assert index.remove("d1") is False
    assert index.doc_count == 1
    assert index.total_length == 1
    assert [hit.doc_id for hit in index.search(["危急值"])] == ["d2"]


def test_同分按doc_id稳定排序():
    index = InvertedIndex()
    index.add("doc-b", ["危急值"])
    index.add("doc-a", ["危急值"])
    index.add("doc-c", ["危急值"])
    hits = index.search(["危急值"])
    assert [hit.doc_id for hit in hits] == ["doc-a", "doc-b", "doc-c"]
    assert index.search(["危急值"]) == hits  # 两次运行逐位一致


def test_查询词去重不重复加权():
    index = InvertedIndex()
    index.add("d1", ["危急值"])
    once = index.score_document(["危急值"], "d1")
    twice = index.score_document(["危急值", "危急值"], "d1")
    assert once == pytest.approx(twice)


def test_k1与b可配且非法参数报错():
    index = InvertedIndex(params=BM25Params(k1=1.2, b=0.3))
    index.add("d1", ["a", "a", "b"])
    index.add("d2", ["a"])
    assert index.params.k1 == 1.2
    assert index.params.b == 0.3
    assert index.score_document(["a"], "d1") > 0

    with pytest.raises(ValueError):
        BM25Params(k1=-1).validate()
    with pytest.raises(ValueError):
        BM25Params(b=1.5).validate()


def test_top_k截断生效():
    index = InvertedIndex()
    for number in range(10):
        index.add(f"d{number}", ["危急值"])
    assert len(index.search(["危急值"], top_k=3)) == 3
    assert index.search(["危急值"], top_k=0) == []


def test_命中词随结果一起返回():
    index = InvertedIndex()
    index.add("d1", ["危急值", "登记"])
    hits = index.search(["危急值", "不存在"])
    assert len(hits) == 1
    assert hits[0].matched_terms == ("危急值",)


# —— 结构化与版本索引 ——


def _doc(
    doc_id: str,
    *,
    item_code: str = "",
    direction: str = "",
    unit: str = "",
    version: str = "",
    effective_from: date | None = None,
    effective_to: date | None = None,
    item_codes: tuple[str, ...] = (),
    section_title: str = "",
) -> IndexDocument:
    return documents_from_fields(
        doc_id=doc_id,
        source_name="测试材料",
        source_sha256="sha-test",
        kind="threshold_row",
        text=f"{item_code} {item_name_of(item_code)} {unit}",
        fields={
            "item_code": item_code,
            "direction": direction,
            "unit": unit,
            "version": version,
            "section_title": section_title,
        },
        multi_fields={"item_codes": item_codes},
        effective_from=effective_from,
        effective_to=effective_to,
        version=version,
    )


def item_name_of(item_code: str) -> str:
    return {"718-7": "血红蛋白", "2601-3": "钾"}.get(item_code, "")


def test_结构化索引按项目码精确命中():
    index = StructuredIndex()
    index.add(_doc("t1", item_code="718-7", direction="both", unit="g/L"))
    index.add(_doc("t2", item_code="2601-3", direction="high", unit="mmol/L"))
    hits = index.query(item_code="718-7")
    assert [hit.doc_id for hit in hits] == ["t1"]
    assert hits[0].matched_keys == ("item_code",)


def test_多键组合查询是且语义():
    index = StructuredIndex()
    index.add(_doc("t1", item_code="718-7", direction="both", unit="g/L", version="示例 v1.0"))
    index.add(_doc("t2", item_code="718-7", direction="low", unit="g/dL", version="示例 v1.0"))
    hits = index.query(item_code="718-7", unit="g/L")
    assert [hit.doc_id for hit in hits] == ["t1"]
    assert hits[0].matched_keys == ("item_code", "unit")
    assert index.query(item_code="718-7", unit="mg/dL") == []


def test_多值键逐值命中():
    index = StructuredIndex()
    index.add(_doc("p1", item_codes=("718-7", "2601-3")))
    assert [hit.doc_id for hit in index.query(item_codes="718-7")] == ["p1"]
    assert [hit.doc_id for hit in index.query(item_codes="2601-3")] == ["p1"]
    assert index.query(item_codes="777-7") == []


def test_失效版本不参与召回():
    index = StructuredIndex()
    index.add(
        _doc(
            "old",
            item_code="718-7",
            version="示例 v1.0",
            effective_from=date(2026, 1, 1),
            effective_to=date(2026, 1, 31),
        )
    )
    index.add(
        _doc(
            "new",
            item_code="718-7",
            version="示例 v2.0",
            effective_from=date(2026, 2, 5),
        )
    )
    at = date(2026, 3, 1)
    assert index.visible_at(at) == ("new",)
    assert index.expired_at(at) == ("old",)
    assert index.pending_at(date(2026, 1, 20)) == ("new",)
    assert [hit.doc_id for hit in index.query(at=at, item_code="718-7")] == ["new"]
    assert index.visible_versions(at) == {"示例 v2.0": ("new",)}


def test_版本号可列出():
    index = StructuredIndex()
    index.add(_doc("t1", version="示例 v1.0"))
    index.add(_doc("t2", version="示例 v1.0"))
    index.add(_doc("t3", version="示例 v2.0"))
    assert index.versions() == {"示例 v1.0": ("t1", "t2"), "示例 v2.0": ("t3",)}


def test_结构化索引重复条目按覆盖():
    index = StructuredIndex()
    index.add(_doc("t1", item_code="718-7"))
    replaced = index.add(_doc("t1", item_code="2601-3"))
    assert replaced is True
    assert index.doc_count == 1
    assert index.query(item_code="718-7") == []
    assert [hit.doc_id for hit in index.query(item_code="2601-3")] == ["t1"]
    assert index.replacements == 1


def test_结构化索引移除条目():
    index = StructuredIndex()
    index.add(_doc("t1", item_code="718-7"))
    assert index.remove("t1") is True
    assert index.remove("t1") is False
    assert index.doc_count == 0
    assert index.values("item_code") == ()


# —— 材料增量重建 ——


def _write_corpus(path: Path, lines: list[str]) -> Path:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _corpus_lines(count: int, marker: str = "") -> list[str]:
    return [
        f"CLAUSE-{index:06d}\t第三章 报告流程与通知时限\t血红蛋白（718-7）危急值"
        f"{marker}检验科值班技师应当在收到报告后 10 分钟内到岗处置，"
        f"通知对象：检验科值班技师；上报时限：10 分钟内；记录去向：危急值登记系统。"
        for index in range(count)
    ]


def test_首次重建把材料全部入库(tmp_path):
    corpus = _write_corpus(tmp_path / "corpus.tsv", _corpus_lines(5))
    index = MaterialIndex()
    source = MaterialSource.from_path(corpus, kind="corpus", key="corpus")
    result = index.rebuild([source])

    assert result.added == 1
    assert result.unchanged == 0
    assert index.doc_count() == 5
    assert index.manifest == {"corpus": source.sha256}


def test_材料内容没变则不重复入库(tmp_path):
    corpus = _write_corpus(tmp_path / "corpus.tsv", _corpus_lines(5))
    index = MaterialIndex()
    source = MaterialSource.from_path(corpus, kind="corpus", key="corpus")
    index.rebuild([source])
    before = index.inverted.stats()

    result = index.rebuild([MaterialSource.from_path(corpus, kind="corpus", key="corpus")])
    assert result.unchanged == 1
    assert result.added == 0
    assert result.updated == 0
    assert index.inverted.stats() == before


def test_材料变了只重建这一份(tmp_path):
    first = _write_corpus(tmp_path / "a.tsv", _corpus_lines(4))
    second = _write_corpus(tmp_path / "b.tsv", _corpus_lines(6))
    index = MaterialIndex()
    index.rebuild(
        [
            MaterialSource.from_path(first, kind="corpus", key="a"),
            MaterialSource.from_path(second, kind="corpus", key="b"),
        ]
    )
    assert index.doc_count() == 10

    _write_corpus(first, _corpus_lines(4, marker="（改版）"))
    result = index.rebuild(
        [
            MaterialSource.from_path(first, kind="corpus", key="a"),
            MaterialSource.from_path(second, kind="corpus", key="b"),
        ]
    )
    assert result.updated == 1
    assert result.unchanged == 1
    assert result.added == 0
    assert result.changed_sources == ("a",)
    assert index.doc_count() == 10


def test_材料撤回后条目被摘除(tmp_path):
    first = _write_corpus(tmp_path / "a.tsv", _corpus_lines(4))
    second = _write_corpus(tmp_path / "b.tsv", _corpus_lines(6))
    index = MaterialIndex()
    index.rebuild(
        [
            MaterialSource.from_path(first, kind="corpus", key="a"),
            MaterialSource.from_path(second, kind="corpus", key="b"),
        ]
    )
    result = index.rebuild([MaterialSource.from_path(second, kind="corpus", key="b")])
    assert result.removed == 1
    assert index.doc_count() == 6
    assert "a" not in index.manifest


def test_重建后能搜到新内容且旧内容不再命中(tmp_path):
    corpus = _write_corpus(tmp_path / "corpus.tsv", _corpus_lines(3))
    index = MaterialIndex()
    index.rebuild([MaterialSource.from_path(corpus, kind="corpus", key="corpus")])
    assert index.search("交接班记录")

    _write_corpus(corpus, _corpus_lines(3, marker=""))
    corpus.write_text(
        "\n".join(
            f"CLAUSE-{number:06d}\t第一章 总则\t急诊会诊制度修订说明 {number}"
            for number in range(3)
        )
        + "\n",
        encoding="utf-8",
    )
    index.rebuild([MaterialSource.from_path(corpus, kind="corpus", key="corpus")])
    assert index.search("急诊会诊")
    assert index.search("交接班记录") == []


def test_空材料集合不炸(tmp_path):
    index = MaterialIndex()
    result = index.rebuild([])
    assert result.added == 0
    assert index.doc_count() == 0
    assert index.search("血红蛋白") == []
    assert index.lookup(item_code="718-7") == []


def test_不认识的材料类型报错(tmp_path):
    path = tmp_path / "x.txt"
    path.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError):
        MaterialIndex().rebuild([MaterialSource.from_path(path, kind="unknown", key="x")])


def test_阈值表与制度材料能进索引(tmp_path):
    index = MaterialIndex()
    result = index.rebuild(
        [
            MaterialSource.from_path(SAMPLE_THRESHOLD, kind="threshold_table", key="threshold"),
            MaterialSource.from_path(SAMPLE_POLICY, kind="policy_document", key="policy"),
        ]
    )
    assert result.added == 2
    assert index.doc_count() > 20
    assert index.lookup(item_code="718-7")
    hits = index.search("血红蛋白 危急值 上报时限")
    assert hits
    assert hits[0].score > 0


# —— 规模验收 ——


def test_索引构建与增量重建耗时可测且增量更快(tmp_path):
    """≥2000 行语料：全量重建 vs 增量重建，增量必须更快。"""
    materials = 8
    per_file = 250
    paths = [
        _write_corpus(
            tmp_path / f"chunk-{number}.tsv",
            _corpus_lines(per_file, marker=f"第{number}批"),
        )
        for number in range(materials)
    ]
    sources = [
        MaterialSource.from_path(path, kind="corpus", key=path.name) for path in paths
    ]

    index = MaterialIndex()
    index.rebuild(sources)  # 预热：让首次导入成本不计入对照

    started = time.perf_counter()
    full = index.full_rebuild(sources)
    full_seconds = time.perf_counter() - started
    assert full.docs_after == materials * per_file

    changed = paths[3]
    _write_corpus(changed, _corpus_lines(per_file, marker="第3批（改版）"))
    started = time.perf_counter()
    incremental = index.rebuild(
        [
            MaterialSource.from_path(path, kind="corpus", key=path.name)
            for path in paths
        ]
    )
    incremental_seconds = time.perf_counter() - started

    assert incremental.updated == 1
    assert incremental.unchanged == materials - 1
    assert incremental_seconds < full_seconds, (
        f"增量重建（{incremental_seconds:.3f}s）应当快于全量重建（{full_seconds:.3f}s）"
    )
    assert index.doc_count() == materials * per_file
