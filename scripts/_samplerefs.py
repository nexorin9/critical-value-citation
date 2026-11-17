"""样例材料读取器（demo_card.py 与 e2e.py 共用）。

**走正式的制度解析器**（``critval/kb/policy.py``）：demo 与 e2e 两条演示
路径要读**同一份**样例制度，同一份条款解析实现不能在别处再写一遍
（此前这里有一份只认「通知对象：…；上报时限：…；记录去向：…」的简化
读取器，任务 id 5 落了正式解析器后已收口到一处）。

这里只做一件事：从样例制度里挑出**演示用**的那一条 —— 优先取三项事实
齐全的条款（通知对象 / 上报时限 / 记录去向），演示才完整。
"""

from __future__ import annotations

from pathlib import Path

from critval.kb.policy import load_policy_document
from critval.models import PolicyClause

__all__ = ["read_sample_clause"]


def read_sample_clause(policy_path: Path) -> PolicyClause:
    """从样例制度里读出**第一条三项事实齐全**的条款。"""
    document = load_policy_document(policy_path)
    if document.degraded:
        raise SystemExit(
            f"样例制度解析降级：{document.degrade_reason}（{policy_path}）"
        )
    if not document.clauses:
        raise SystemExit(f"样例制度里没读到条款：{policy_path}")

    complete = [
        clause
        for clause in document.clauses
        if not clause.missing_fields and clause.notify_target
    ]
    if complete:
        return complete[0]
    return document.clauses[0]
