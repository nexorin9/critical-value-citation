"""中文分词（双字切分）+ 停用词 —— 词法召回的第一道工序。

## 为什么用「双字切分」而不是词典分词

院内制度文本是**条款体**：一句话里塞着项目名、阈值、时限、通知对象、记录去向，
几乎不含分词词典能覆盖的词边界（"危急值登记系统" "护理交接班记录" 都不在通用
词典里）。装一套词典分词器，要为每家医院的行文习惯各维护一份词表 —— 院内换一次
文件名，词表就得跟着改。

**双字切分**（bigram）不依赖词表：把连续汉字串切成所有相邻两字的组合
（"检验科值班技师" → 检验 / 验科 / 科值 / 值班 / 班技 / 技师）。召回时查询与文档
用同一套切法，"值班技师" 这类固定搭配天然对得上；对不上也不需要退回"匹配不到"，
只是得分低一点。代价是索引里会多存一些无意义组合（如 "科须"），但那由 BM25 的
idf 自然压下去 —— 到处都出现的组合 idf 趋近 0。

数值、英文与项目码**整体保留**：`718-7`、`g/L`、`30` 这类 token 一字不改。
项目码是阈值表主键，被拆开就再也对不上了。

## 停用词口径

停用词只收**制度文本里高频出现、且不携带任何检索信号**的双字功能词
（"应当" "进行" "相关"）。凡是可能影响命中判定的词一律不收 —— 停用词表收得
越宽，漏召回的风险越大，而漏召回在这个产品里意味着"值班人拿不到依据"。

单字停用词只作用于**孤立单字**（连续汉字串长度为 1 的情形），不影响双字切分。
"""

from __future__ import annotations

import re
import unicodedata
from typing import Iterable, Iterator

__all__ = [
    "STOPWORDS",
    "SINGLE_CHAR_STOPWORDS",
    "bigrams",
    "is_stopword",
    "normalize",
    "tokenize",
]


#: 连续汉字串（基本区 + 扩展 A）。
_CJK = "\u3400-\u4dbf\u4e00-\u9fff"

#: 一次扫描同时认出「汉字串」与「字母数字串」，保证 token 顺序与原文一致。
_TOKEN_RE = re.compile(
    rf"(?P<cjk>[{_CJK}]+)|(?P<ascii>[A-Za-z0-9]+(?:[/.\-][A-Za-z0-9]+)*)"
)

#: 双字停用词：制度文本里的高频功能词，不携带检索信号。
STOPWORDS: frozenset[str] = frozenset(
    {
        "进行",
        "予以",
        "根据",
        "按照",
        "对于",
        "关于",
        "上述",
        "以下",
        "以及",
        "并且",
        "同时",
        "应当",
        "必须",
        "不得",
        "可以",
        "相关",
        "有关",
        "情况",
        "时候",
        "其中",
        "及其",
        "之后",
        "之前",
        "本院",
        "院内",
        "须在",
        "科须",
        "应在",
        "并将",
        "对此",
        "此外",
        "所有",
        "各类",
        "各个",
        "一项",
        "如果",
        "由于",
        "因为",
        "以便",
        "为准",
        "为限",
    }
)

#: 单字停用词：只作用于孤立单字 token（连续汉字串长度为 1 时）。
SINGLE_CHAR_STOPWORDS: frozenset[str] = frozenset(
    {
        "的",
        "了",
        "和",
        "与",
        "及",
        "或",
        "在",
        "是",
        "为",
        "对",
        "由",
        "从",
        "到",
        "等",
        "该",
        "其",
        "之",
        "并",
        "也",
        "都",
        "就",
        "还",
        "把",
        "被",
        "让",
        "使",
        "有",
        "无",
        "不",
        "个",
        "次",
    }
)


def normalize(text: str) -> str:
    """归一化：全角转半角（NFKC）+ 小写。

    NFKC 让 ``（718-7）`` 变成 ``(718-7)``、``１５`` 变成 ``15`` —— 院内材料里
    全角数字与半角数字混用是常态，不归一就会切出不同的 token。
    """
    if not text:
        return ""
    return unicodedata.normalize("NFKC", text).lower()


def bigrams(text: str) -> list[str]:
    """把汉字串切成双字组合。

    长度为 1 的汉字串保留原字（否则那个字就彻底丢了）。
    """
    out: list[str] = []
    for run in re.findall(rf"[{_CJK}]+", normalize(text)):
        if len(run) == 1:
            out.append(run)
            continue
        out.extend(run[index : index + 2] for index in range(len(run) - 1))
    return out


def is_stopword(token: str) -> bool:
    """这个 token 是不是停用词。"""
    if len(token) == 1:
        return token in SINGLE_CHAR_STOPWORDS
    return token in STOPWORDS


def _iter_raw_tokens(text: str) -> Iterator[str]:
    """按原文顺序吐出未经停用词过滤的 token。"""
    normalized = normalize(text)
    for match in _TOKEN_RE.finditer(normalized):
        cjk = match.group("cjk")
        if cjk:
            if len(cjk) == 1:
                yield cjk
            else:
                for index in range(len(cjk) - 1):
                    yield cjk[index : index + 2]
            continue
        ascii_token = match.group("ascii")
        # 纯分隔符尾巴（"718-7-" 之类）不产生 token。
        yield ascii_token


def tokenize(
    text: str,
    *,
    remove_stopwords: bool = True,
    keep_single_char: bool = True,
) -> list[str]:
    """把一段中文文本切成检索用的 token 序列。

    * 连续汉字 → 双字组合；
    * 字母 / 数字 / 项目码 → 整体保留并转小写（``718-7``、``g/L``、``30``）；
    * ``remove_stopwords=True``（默认）时去掉停用词。

    返回顺序与原文一致，便于上层按顺序拼回短语特征。
    """
    if not text:
        return []
    tokens: list[str] = []
    for token in _iter_raw_tokens(text):
        if remove_stopwords and is_stopword(token):
            continue
        if len(token) == 1 and not keep_single_char and not token.isascii():
            continue
        tokens.append(token)
    return tokens


def tokenize_all(texts: Iterable[str], **kwargs) -> list[str]:
    """对多段文本批量切词，顺序拼接。"""
    out: list[str] = []
    for text in texts:
        out.extend(tokenize(text, **kwargs))
    return out
