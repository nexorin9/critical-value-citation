"""交付目录树核对：文档里写的树必须和仓库里的实际文件对得上（任务 id 16）。

**为什么要有这条**：发布前清理是「删东西」的动作，最容易出的错是**文档说的和
实际的不一致** —— 文档里列了个文件其实早没了，或者清理完多出一堆没人知道的
目录。这条用例把两处目录树（README「## 项目结构」与 ``docs/architecture.md``
的完整树）逐条抠出来核对：

* **文档 → 实际**：文档里写了的路径必须真实存在（运行期目录除外，见
  ``RUNTIME_DIRS``）—— 否则文档在骗人；
* **实际 → 文档**：项目根下不允许出现**文档里没写**的条目 —— 否则发布上去
  有没人知道是什么的东西。

两条方向都过，才叫「可以直接发布到 GitHub」。

**关于运行期产物**：``reports/`` / ``logs/`` / ``__pycache__`` 这类目录在**跑完
测试之后一定会出现**（测试自己会生成核对报告、Python 自己会写字节码缓存），
所以这里不断言「磁盘上不存在」，而是断言「**它们被 .gitignore 排除了**」——
发布走 git，进了 .gitignore 就等于没发布。磁盘层面的清理由发布前清理任务
做，并记在 ``progress.txt`` 里。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: 运行期目录：跑起来才有、已 gitignore、文档里会写但**发布时不存在**。
#:
#: ``data/`` 在其中：它是**可再生语料**（公开参考区间样例集 · Synthea 合成帧 ·
#: 评测集），由 ``scripts/fetch_data.py`` / ``scripts/gen_synth.py`` 重建，
#: 一律 gitignore —— **fresh clone 上本来就不存在**，所以不能断言它必须在盘上。
#: （2026-10-31 发布前清理实测：把 ``data/`` 移走再跑本文件，
#: 目录树核对原本会报「docs/architecture.md 里写了但仓库里没有：['data']」，
#: 那是在给一个**永远不会发布**的目录挑错。）
RUNTIME_DIRS = {
    "data",  # 可再生大语料与评测集（gitignore，脚本随时重建）
    "reports",  # 对比表 / 差异清单等运行期产物
    "logs",  # 守护进程日志
}

#: 项目根下允许存在、但**不写进目录树**的条目（工具链约定，不是产品文件）。
ROOT_ALLOWLIST = {
    ".git",
    ".gitignore",
    ".DS_Store",
    ".env",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".benchmarks",
    ".coverage",
    "coverage",
    "build",
    "dist",
    "node_modules",
    ".next",
    ".nuxt",
    "target",
}

#: 目录树里的行首：``│   ``（父级仍在）与 ``    ``（父级已结束）各占 4 格。
_LINE = re.compile(r"^((?:(?:│   |    ))*)(?:├── |└── )(.+?)\s*(?:#.*)?$")


def _code_blocks(markdown: str) -> list[tuple[str, list[str]]]:
    """把 markdown 拆成 ``(前置文本, 代码块行)`` 的列表。

    前置文本是该代码块**之前**最近的一段非代码内容 —— 用来按标题定位代码块
    （README 的目录树在「## 项目结构」标题下面，architecture.md 的目录树在
    正文里、块首行就是 ``critical-value-citation/``）。
    """
    blocks: list[tuple[str, list[str]]] = []
    context: list[str] = []
    current: list[str] | None = None
    for line in markdown.splitlines():
        if line.strip().startswith("```"):
            if current is None:
                current = []
            else:
                blocks.append(("\n".join(context), current))
                current = None
            continue
        if current is None:
            if line.strip():
                context.append(line)
        else:
            current.append(line)
    return blocks


def _tree_lines(markdown: str, selector: str) -> list[str]:
    """按 ``selector`` 取出目录树代码块里的原始行。

    ``selector`` 命中两种情况之一：

    * 是某个**标题**（如 ``## 项目结构``）—— 取该标题之后的第一个代码块；
    * 是代码块**首行**的一部分（如 ``critical-value-citation/``）—— 取首行
      含它的那个代码块。
    """
    blocks = _code_blocks(markdown)
    for context, body in blocks:
        if selector in context:
            return body
    for _context, body in blocks:
        if body and selector in body[0]:
            return body
    pytest.fail(f"{selector} 对应的目录树代码块没找到")


def _parse_tree(lines: list[str]) -> list[str]:
    """把树形缩进还原成完整相对路径列表。

    ``├── critval/`` / ``│   ├── daemon.py`` 这类行，靠前缀里的 ``│   `` 与
    ``    `` 判断父级还在不在，逐层拼出 ``critval/hl7/er7.py`` 这样的路径。

    一行里可以并排写多个路径（README 的精简树就是这么写的：``hl7/ kb/ index/``
    是三个**同级**目录，``config.py / models.py`` 是两个同级文件），按空白
    切开即可；``concepts/visual-goal.md`` 这种「目录/文件」写法则整体算一条。
    """
    stack: list[str] = []
    paths: list[str] = []
    for line in lines:
        match = _LINE.match(line)
        if not match:
            continue
        prefix, name = match.groups()
        depth = len(prefix) // 4
        # 一行里并排多个路径时，它们都是**同级**的
        for part in name.split():
            part = part.strip()
            if not part:
                continue
            del stack[depth:]
            stack.append(part.rstrip("/"))
            paths.append("/".join(stack))
    return paths


def _documented_paths(relative_doc: str, heading: str | None) -> list[str]:
    doc = PROJECT_ROOT / relative_doc
    assert doc.exists(), f"{relative_doc} 不存在"
    return _parse_tree(_tree_lines(doc.read_text(encoding="utf-8"), heading))


_DOC_SOURCES = (
    ("README.md", "## 项目结构"),
    ("docs/architecture.md", "critical-value-citation/"),
)


# ---------------------------------------------------------------------------
# 文档 → 实际
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("relative_doc, heading", _DOC_SOURCES)
def test_文档里写的每条路径都真实存在(relative_doc: str, heading: str) -> None:
    missing: list[str] = []
    for path in _documented_paths(relative_doc, heading):
        head = path.split("/", 1)[0]
        if head in RUNTIME_DIRS:
            continue  # 运行期目录，发布时不存在是对的
        if "*" in path:
            continue  # 通配写法（如 test_*.py），由下一条用例保证真有文件
        if not (PROJECT_ROOT / path).exists():
            missing.append(path)
    assert not missing, f"{relative_doc} 里写了但仓库里没有：{missing}"


@pytest.mark.parametrize("relative_doc, heading", _DOC_SOURCES)
def test_通配写法对应真实文件(relative_doc: str, heading: str) -> None:
    """``test_*.py`` 这类通配写法，必须真的匹配得到文件。"""
    for path in _documented_paths(relative_doc, heading):
        if "*" not in path:
            continue
        assert list(PROJECT_ROOT.glob(path)), f"{relative_doc}: {path} 没有匹配到文件"


# ---------------------------------------------------------------------------
# 实际 → 文档
# ---------------------------------------------------------------------------
def test_项目根没有文档里没写的条目() -> None:
    """发布目录里不许出现文档没交代过的东西（清理没清干净的最常见症状）。"""
    documented_heads: set[str] = set()
    for relative_doc, heading in _DOC_SOURCES:
        for path in _documented_paths(relative_doc, heading):
            documented_heads.add(path.split("/", 1)[0])

    unknown: list[str] = []
    for entry in sorted(PROJECT_ROOT.iterdir()):
        if entry.name in ROOT_ALLOWLIST or entry.name.startswith("."):
            continue
        if entry.name in documented_heads or entry.name in RUNTIME_DIRS:
            continue
        unknown.append(entry.name)
    assert not unknown, f"项目根下有文档没交代的条目：{unknown}"


#: 逐文件核对的子树 —— 这些目录里的文件**都是用户要找的入口**，漏写等于
#: 藏起来。``tests/`` 与 ``samples/`` 不在此列：前者按 ``test_*.py`` 通配交代，
#: 后者按目录整体交代（样例材料的清单在 docs/material_spec.md）。
_PER_FILE_SUBTREES = ("scripts", "critval")


def test_子目录里的文件都被目录树写到了() -> None:
    """``scripts/`` 与 ``critval/`` 下的每个文件都必须在完整目录树里点名。

    这条专治「加了新脚本忘了写进文档」—— 一条命令能跑的功能，用户却找不到
    它叫什么名字、干什么用。
    """
    documented: set[str] = set()
    for relative_doc, heading in _DOC_SOURCES:
        documented.update(_documented_paths(relative_doc, heading))

    missing: list[str] = []
    for subtree in _PER_FILE_SUBTREES:
        for path in sorted((PROJECT_ROOT / subtree).rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(PROJECT_ROOT).as_posix()
            if "__pycache__" in rel or path.name == "__init__.py":
                continue  # 包标记文件按惯例不逐个列
            if rel not in documented:
                missing.append(rel)
    assert not missing, f"目录树里漏写的文件：{missing}"


def test_docs_顶层文档都被目录树写到了() -> None:
    """``docs/`` 顶层的每份文档都要在目录树里点名（子目录整体交代即可）。"""
    documented: set[str] = set()
    for relative_doc, heading in _DOC_SOURCES:
        documented.update(_documented_paths(relative_doc, heading))

    missing = [
        path.name
        for path in sorted((PROJECT_ROOT / "docs").iterdir())
        if path.is_file() and f"docs/{path.name}" not in documented
    ]
    assert not missing, f"目录树里漏写的文档：{missing}"


# ---------------------------------------------------------------------------
# 运行期产物的发布口径
# ---------------------------------------------------------------------------
def _gitignore_text() -> str:
    return (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "pattern",
    [
        "data/",
        "reports/",
        "logs/",
        "__pycache__/",
        ".pytest_cache/",
        ".mypy_cache/",
        ".ruff_cache/",
        "*.py[cod]",
        "*.log",
        "*.tmp",
        "*.db",
        "*.sqlite",
        "*.sqlite3",
        ".env",
        "score.json",
    ],
)
def test_运行期产物都在_gitignore_里(pattern: str) -> None:
    """运行期产物必须被 .gitignore 排除 —— 进了 .gitignore 就等于没发布。"""
    assert pattern in _gitignore_text(), f".gitignore 没排除 {pattern}"


def test_发布目录里没有轮次元数据() -> None:
    """``score.json`` 是轮次元数据，**永远**不该出现在产品目录里。"""
    found = [str(p.relative_to(PROJECT_ROOT)) for p in PROJECT_ROOT.rglob("score.json")]
    assert not found, f"产品目录里混进了轮次元数据：{found}"


def test_发布目录里没有真实_env() -> None:
    """``.env`` 只能以 ``.env.example`` 的形式存在（凭据位样例，无真实密钥）。"""
    env_files = [p.name for p in PROJECT_ROOT.iterdir() if p.is_file() and p.name == ".env"]
    assert not env_files, "产品目录里出现了 .env（真实凭据文件不应入库）"
    assert (PROJECT_ROOT / ".env.example").exists(), ".env.example 缺失"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
