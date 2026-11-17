"""实测证据的诚实性自检（任务 id 14）。

**为什么要有这个测试**：README 里「概念 / 实测对照」这一节的价值，全靠
「实测那侧真的是跑出来的」撑着。只要有人往 ``docs/screenshots/`` 里塞一张
设计稿、或者拿概念图冒充实测，这一节就变成骗人的东西。所以把规矩写成
断言：

* 四张采集产物**必须在**、且不是空文件；
* ``docs/concepts/`` 里**不能有图片** —— 视觉目标是结构化卡片，不是图；
* README 必须**引用**这四张图（引用不到 = 采集白做了）；
* 终端会话原文（``.txt``）必须存在、且**没有乱码**（U+FFFD）—— 原文是
  证据本体，图片只是它的栅格化。
"""

from __future__ import annotations

from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: 本任务采集的四张真实产物。
CAPTURED = (
    "daemon-run-log.png",
    "console-ledger.png",
    "console-ack-card.png",
    "export-ledger.png",
)

#: 终端会话的逐字原文（PNG 是栅格化，原文才是证据本体）。
TRANSCRIPTS = (
    "terminal_session_daemon.txt",
    "terminal_session_export.txt",
)

#: 概念目录里**允许**放的东西（本环境无图像生成能力，只有结构化目标卡）。
CONCEPT_ALLOWED_SUFFIXES = {".md"}


def test_captured_screenshots_exist_and_are_not_empty() -> None:
    """四张实测产物都在，且不是 0 字节的空文件。"""
    for name in CAPTURED:
        path = PROJECT_ROOT / "docs" / "screenshots" / name
        assert path.exists(), f"缺实测截图：{path}"
        assert path.stat().st_size > 10_000, f"{name} 太小，不像真实截图"


def test_concepts_directory_has_no_images() -> None:
    """视觉目标目录里不许出现图片 —— 图片属于实测证据，概念只能是卡片。"""
    concepts = PROJECT_ROOT / "docs" / "concepts"
    assert concepts.exists(), "概念目录不该被删掉"
    for path in concepts.iterdir():
        assert path.suffix.lower() in CONCEPT_ALLOWED_SUFFIXES, (
            f"docs/concepts/ 里出现了图片 {path.name} —— "
            "概念与实测必须分家，否则「实测证据」就不可信了"
        )


def test_screenshots_dir_contains_no_stray_non_png() -> None:
    """实测目录里只放 PNG（终端原文另存 docs/examples/，不混进来）。"""
    shots = PROJECT_ROOT / "docs" / "screenshots"
    for path in shots.iterdir():
        if path.is_file():
            assert path.suffix.lower() == ".png", f"{path.name} 不该在实测目录里"


def test_readme_references_every_captured_screenshot() -> None:
    """README 的「实测证据」必须把这四张都引用上。"""
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    for name in CAPTURED:
        assert f"docs/screenshots/{name}" in readme, f"README 没引用 {name}"
    assert "## 概念 / 实测对照" in readme
    assert "### 实测证据" in readme


def test_readme_has_honest_gap_note() -> None:
    """必须写清「哪侧是目标、哪侧是实测、差在哪」。"""
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    assert "诚实标注" in readme
    assert "视觉目标" in readme
    assert "实测证据" in readme


def test_transcripts_exist_and_are_clean_utf8() -> None:
    """终端原文在，且没有替换字符（U+FFFD）—— 说明没被读截断过。"""
    for name in TRANSCRIPTS:
        path = PROJECT_ROOT / "docs" / "examples" / name
        assert path.exists(), f"缺终端原文：{path}"
        text = path.read_text(encoding="utf-8")
        assert "�" not in text, f"{name} 里有乱码（U+FFFD），说明读的时候切坏了多字节字符"
        assert len(text.splitlines()) > 20, f"{name} 内容太少，不像一次真实会话"


def test_transcripts_contain_real_run_markers() -> None:
    """原文里必须有真跑出来的痕迹：心跳、事件处理链、推送记录、导出物。"""
    daemon = (PROJECT_ROOT / "docs" / "examples" / "terminal_session_daemon.txt").read_text(
        encoding="utf-8"
    )
    assert "critval.daemon" in daemon
    assert "心跳" in daemon
    assert "事件已处理" in daemon
    assert "notify-queue" in daemon
    assert "守护进程已停止" in daemon

    export = (PROJECT_ROOT / "docs" / "examples" / "terminal_session_export.txt").read_text(
        encoding="utf-8"
    )
    assert "危急值处置卡" in export
    assert "处置依据（四要素）" in export
    assert "危急值复核日报" in export


@pytest.mark.parametrize("name", CAPTURED)
def test_capture_script_produces_this_file(name: str) -> None:
    """采集脚本里确实写出了这个文件名（改名了要同步改这里）。"""
    script = (PROJECT_ROOT / "scripts" / "capture_screenshots.py").read_text(
        encoding="utf-8"
    )
    assert name in script, f"采集脚本里找不到 {name}，产物名对不上"
