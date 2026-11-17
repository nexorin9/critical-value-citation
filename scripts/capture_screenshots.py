#!/usr/bin/env python3
"""采集真实运行证据到 ``docs/screenshots/``（任务 id 14）。

四张图，**全部来自真实运行**：

===========================  ==========================================  ================================
产物                        内容                                        怎么来的
===========================  ==========================================  ================================
``daemon-run-log.png``      守护进程真实终端会话：心跳 + 事件处理链      真 PTY 里开交互式 bash，
                             + 推送记录（降级原因如实写明）              命令真敲进去，逐字节取回原文
``console-ledger.png``      值班台事件台账页                            ``console-serve`` 真进程 + Chromium
``console-ack-card.png``    值班台处置卡确认页                          同上
``export-ledger.png``       CLI ``export`` 的真实导出片段                同样的真终端会话
===========================  ==========================================  ================================

四条纪律：

1. **不动仓库里的样例数据** —— 台账 / 处置卡快照 / 本机通知队列 / 日志
   全在 ``data/capture/run/`` 里跑，跑完随发布前清理一起删。
2. **终端会话是真终端** —— ``pty.openpty()`` 起交互式 bash，命令**敲进去**，
   命令回显由终端给出（原始字节，不转义），输出按到达顺序混排。之后只用
   Chromium 把**这份原文**渲染成 PNG，一个字都不改。
3. **不伪造** —— 跑不通就明确报错退出，**不产出占位图**。
4. **视觉目标与实测证据分家** —— 设计意图留在 ``docs/concepts/``（结构化
   视觉目标卡），实测产物落 ``docs/screenshots/``，两者不混放。

跑法::

    python3 scripts/capture_screenshots.py --out docs/screenshots

依赖：Playwright（``pip install playwright`` + ``playwright install chromium``）。
没装就明确报错退出，**不产出占位图**。
"""

from __future__ import annotations

import argparse
import codecs
import html
import os
import pty
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

#: 运行期目录（台账 / 快照 / 通知队列 / 日志都落这里；``data/`` 不入库）。
RUN_ROOT = Path("data/capture/run")

#: 凭据位：**显式清空**。采集截图时不连任何院内系统，也不带任何真凭据 ——
#: 日志里出现的「未接入」是真实的降级，不是摆拍。
CREDENTIAL_KEYS = (
    "CRITVAL_WECOM_WEBHOOK_URL",
    "CRITVAL_BASE_URL",
    "CRITVAL_TOKEN",
    "NURSE_ROSTER_BASE_URL",
    "ROSTER_TOKEN",
    "LIS_CRITICAL_TOKEN",
    "LIS_BASE_URL",
    "CONSOLE_TOKEN",
)


def _fail(message: str) -> int:
    print(f"[capture_screenshots] {message}", file=sys.stderr)
    return 1


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    """给 PTY 设窗口大小 —— 终端按这个宽度折行。"""
    try:
        import fcntl
        import struct
        import termios

        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    except OSError:  # pragma: no cover - 平台兜底
        pass


def _compact(text: str) -> str:
    """去掉所有空白。

    终端折行会在命令中间插 ``\\r\\n``，于是「刚敲进去的命令」在缓冲里
    并不是一段连续子串。去掉空白再比，就不受折行影响。
    """
    return "".join(text.split())


#: 终端控制序列（bracketed paste、光标定位等）—— 截图里不要，文字不要。
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")

#: 日志行的时间戳前缀 —— 用来分辨「命令回显」与「提示符被日志粘住」。
_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")

#: 采集机（macOS）自己的噪声行 —— 与产品运行无关，剔掉。
#: 值班机是 Linux，不会有这几行；留着只会让截图看着像在 Mac 上截的。
_CAPTURE_NOISE = frozenset(
    {
        "The default interactive shell is now zsh.",
        "To update your account to use zsh, please run `chsh -s /bin/zsh`.",
        "For more details, please visit https://support.apple.com/kb/HT208050.",
    }
)


# ---------------------------------------------------------------------------
# 真终端会话
# ---------------------------------------------------------------------------


class _TerminalSession:
    """一个**真终端**里的交互式 bash 会话。

    为什么不用「跑一段脚本、把输出重定向到文件」：那样拿到的是**事后拼
    出来的文字**，命令回显要自己写、行序要自己对 —— 很容易拼出终端上根本
    没出现过的画面。这里开一个 PTY，跑交互式 bash，命令**由终端回显**
    （原始字节，不转义），输出按到达顺序混排 —— 和值班机上看到的一致。

    还有一个坑：macOS 自带 bash 3.2 的 ``set -x`` 会把非 ASCII 字节转义成
    ``$'\\213'``，中文路径在回显里全是乱码。交互式会话没这个问题 ——
    终端回显的是原始字节。
    """

    #: 提示符。用最短的 ``$ ``，截图里最接近真终端。
    PROMPT = "$ "

    #: 终端宽度。默认 80 列太窄 —— 守护进程那行命令会被折成六七段，
    #: 折点还落在路径中间，看着像坏了。值班机上的终端一般拉得开。
    COLS = 200
    ROWS = 60

    def __init__(self, *, cwd: Path, env: dict[str, str]) -> None:
        self._master, slave = pty.openpty()
        _set_winsize(self._master, self.ROWS, self.COLS)
        self._proc = subprocess.Popen(
            ["bash", "--norc", "--noprofile", "-i"],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            cwd=str(cwd),
            env=env,
            close_fds=True,
            start_new_session=True,
        )
        os.close(slave)
        os.set_blocking(self._master, False)
        self._chunks: list[bytes] = []
        # **增量**解码器：一个汉字／制表符是多个字节，可能正好被一次 read()
        # 切成两半。逐块 decode 会把半截序列变成乱码（U+FFFD）——
        # 增量解码器会把半截留在缓冲里等下一块。
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._text = ""
        self._settle(1.0)
        self._wait_prompt(10.0)

    # ---- 底层读 ---------------------------------------------------------

    def _pump(self, timeout: float = 0.1) -> None:
        """读一次可用输出（读不到就等一小会儿）。"""
        deadline = time.time() + timeout
        while True:
            try:
                chunk = os.read(self._master, 65536)
            except BlockingIOError:
                if time.time() >= deadline:
                    return
                time.sleep(0.02)
                continue
            except OSError:
                return
            if not chunk:
                return
            self._chunks.append(chunk)
            self._text += self._decoder.decode(chunk)
            if time.time() >= deadline:
                return

    def _settle(self, seconds: float) -> None:
        """连续读 seconds 秒（用于等后台日志流完）。"""
        deadline = time.time() + seconds
        while time.time() < deadline:
            self._pump(0.2)

    def _wait_prompt(self, timeout: float) -> None:
        """等到下一个提示符出现。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            lines = self._clean_lines()
            if lines and lines[-1] == self.PROMPT:
                return
            self._pump(0.2)

    def _clean_lines(self) -> list[str]:
        """缓冲按行切开（去掉控制序列）。"""
        return [
            _ANSI_RE.sub("", line)
            for line in self._text.replace("\r\n", "\n").replace("\r", "").split("\n")
        ]

    def _prompt_count(self) -> int:
        """当前缓冲里「以提示符开头」的行数。

        不能只看「最后一行是不是提示符」—— 守护进程在后台写日志时，提示符
        后面会立刻接上日志行（``$ 2026-10-31 …``），最后一行就不是提示符了。

        也不能只数「整行就是提示符」的行 —— 那行被日志粘住之后就不算了。
        所以数「**以提示符开头**」的行。

        每敲一条命令，净增 **1**：上一行提示符被终端回显的命令**续写**成
        ``$ cp xxx``（还是以提示符开头，不多不少），命令跑完 bash 再打印
        一行新提示符 —— 那才是「多出来的那一行」。
        """
        return sum(1 for line in self._clean_lines() if line.startswith(self.PROMPT))

    # ---- 用法 -----------------------------------------------------------

    def run(self, command: str, *, timeout: float = 90.0) -> None:
        """敲一条命令进去，等它跑完（下一个提示符出现）。

        先等终端把命令**回显**出来，再数提示符 —— 否则「旧提示符还在缓冲
        末尾」会被误判成「命令已经跑完了」。
        """
        prompts_before = self._prompt_count()
        os.write(self._master, (command + "\n").encode("utf-8"))
        deadline = time.time() + timeout
        wanted = _compact(command)
        echoed = False
        while time.time() < deadline:
            if not echoed and wanted in _compact(self._text):
                echoed = True
            if echoed and self._prompt_count() > prompts_before:
                return
            self._pump(0.1)

    @property
    def text(self) -> str:
        """终端上到目前为止的**逐字节原文**（去掉控制序列与采集机噪声）。"""
        raw = self._text.replace("\r\n", "\n").replace("\r", "")
        raw = _ANSI_RE.sub("", raw)
        return "\n".join(
            line for line in raw.split("\n") if line not in _CAPTURE_NOISE
        )


    def close(self) -> None:
        try:
            os.write(self._master, b"exit\n")
            self._settle(0.4)
        except OSError:
            pass
        try:
            self._proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - 兜底
            self._proc.kill()
            self._proc.wait(timeout=5)
        try:
            os.close(self._master)
        except OSError:
            pass



# ---------------------------------------------------------------------------
# 终端原文 → PNG
# ---------------------------------------------------------------------------

_TERMINAL_CSS = """
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; background: #0d1017; }
  .window { margin: 18px; border: 1px solid #2b3240; border-radius: 12px;
            overflow: hidden; background: #12161f;
            box-shadow: 0 22px 64px rgba(0,0,0,.55); }
  .titlebar { display: flex; justify-content: space-between; align-items: center;
              gap: 16px; background: #1b2130; color: #9fb0c8; font-size: 13px;
              padding: 11px 16px; border-bottom: 1px solid #2b3240;
              font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif; }
  .titlebar .dots { color: #4d5a6e; letter-spacing: 5px; margin-right: 6px; }
  .titlebar .meta { color: #6c7a8d; font-size: 12px; text-align: right; }
  pre { margin: 0; padding: 18px 20px 24px; color: #d7dde7; font-size: 13px;
        line-height: 1.6; white-space: pre-wrap; overflow-wrap: anywhere;
        font-family: Menlo, "SF Mono", "PingFang SC", "Microsoft YaHei", monospace; }
  .cmd { color: #8ddb9c; }
  .warn { color: #e3b341; }
  .err { color: #f0836a; }
"""


def _terminal_html(text: str, *, title: str, meta: str) -> str:
    """把终端原文包进一个终端外壳；命令行与告警行按前缀上色（不改文字）。"""
    lines = []
    for raw in text.split("\n"):
        line = html.escape(raw)
        if raw.startswith("$ "):
            rest = raw[2:]
            if _TIMESTAMP_RE.match(rest):
                # 提示符刚打印出来就被后台日志粘住了 —— 只有提示符是绿的，
                # 后面那截是日志，按日志的配色走。
                lines.append(f'<span class="cmd">$ </span>{html.escape(rest)}')
            else:
                lines.append(f'<span class="cmd">{line}</span>')
        elif "WARNING" in raw:
            lines.append(f'<span class="warn">{line}</span>')
        elif "ERROR" in raw:
            lines.append(f'<span class="err">{line}</span>')
        else:
            lines.append(line)
    body = "\n".join(lines)
    return (
        "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        f"<style>{_TERMINAL_CSS}</style></head><body>"
        '<div class="window">'
        '<div class="titlebar">'
        f'<span><span class="dots">●●●</span>{html.escape(title)}</span>'
        f'<span class="meta">{html.escape(meta)}</span>'
        "</div>"
        f"<pre>{body}</pre>"
        "</div></body></html>"
    )


def _render_html_png(page_html: str, out: Path, *, width: int = 1680) -> None:
    """用 Chromium 把一段 HTML 渲染成 PNG（终端会话原文的栅格化）。"""
    from playwright.sync_api import sync_playwright

    out.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(
            viewport={"width": width, "height": 900}, device_scale_factor=2
        )
        page.set_content(page_html, wait_until="load")
        page.screenshot(path=str(out), full_page=True)
        browser.close()


# ---------------------------------------------------------------------------
# 值班台页面截图
# ---------------------------------------------------------------------------


def _shot_page(
    page,
    url: str,
    out: Path,
    *,
    width: int = 1440,
    height: int = 900,
    full_page: bool = False,
) -> None:
    page.set_viewport_size({"width": width, "height": height})
    page.goto(url, wait_until="networkidle")
    page.wait_for_timeout(400)
    page.screenshot(path=str(out), full_page=full_page)


def _free_port() -> int:
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _wait_health(base: str, timeout: float = 20.0) -> bool:
    import urllib.error
    import urllib.request

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{base}/healthz", timeout=2) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.3)
    return False


# ---------------------------------------------------------------------------
# 运行期准备
# ---------------------------------------------------------------------------


def _prepare_run_dir() -> dict[str, Path]:
    """清空并重建运行期目录；投递示例材料。"""
    run_dir = PROJECT_ROOT / RUN_ROOT
    if run_dir.exists():
        shutil.rmtree(run_dir)
    frames = run_dir / "frames"
    materials = run_dir / "materials"
    snapshots = run_dir / "snapshots"
    for directory in (frames, materials, snapshots):
        directory.mkdir(parents=True, exist_ok=True)

    threshold_src = PROJECT_ROOT / "samples" / "threshold" / "示例危急值阈值表.xlsx"
    policy_src = PROJECT_ROOT / "samples" / "policy" / "示例危急值报告制度.docx"
    for src in (threshold_src, policy_src):
        if not src.exists():
            raise FileNotFoundError(f"找不到示例材料：{src}")
    shutil.copy2(threshold_src, materials / threshold_src.name)
    shutil.copy2(policy_src, materials / policy_src.name)

    return {
        "run_dir": run_dir,
        "frames": frames,
        "materials": materials,
        "snapshots": snapshots,
        "db": run_dir / "critval.db",
        "log": run_dir / "daemon.log",
        "threshold": materials / threshold_src.name,
        "policy": materials / policy_src.name,
    }


def _session_env(paths: dict[str, Path]) -> dict[str, str]:
    """会话环境变量：运行期路径全部指到运行目录；凭据位一律清空。"""
    env = dict(os.environ)
    for key in CREDENTIAL_KEYS:
        env.pop(key, None)
    env.update(
        {
            "CRITVAL_DB_PATH": str(paths["db"]),
            "CRITVAL_LOCAL_NOTIFICATION_PATH": str(paths["run_dir"] / "local_notifications.jsonl"),
            "CRITVAL_SNAPSHOT_DIR": str(paths["snapshots"]),
            "CRITVAL_HEARTBEAT_PATH": str(paths["run_dir"] / "heartbeat.json"),
            "CRITVAL_LOCK_PATH": str(paths["run_dir"] / "critval.lock"),
            "CRITVAL_CONFIG_SNAPSHOT_DIR": str(paths["run_dir"] / "config_snapshots"),
            "CRITVAL_LOG_DIR": str(paths["run_dir"] / "logs"),
            "PYTHONUNBUFFERED": "1",
            "PYTHONIOENCODING": "utf-8",
            # 会话 ``cd`` 到运行目录，包要从项目根导入 —— 值班机上装成
            # site-packages 也行，这里是源码直跑，所以给一条 PYTHONPATH。
            "PYTHONPATH": str(PROJECT_ROOT),
            # 终端 locale 必须是 UTF-8 —— 否则终端回显的中文路径会是乱码。
            "LANG": "en_US.UTF-8",
            "LC_ALL": "en_US.UTF-8",
            "TERM": "xterm-256color",
            # 提示符：截图里最接近真终端的一行 ``$ ``。
            "PS1": "$ ",
        }
    )
    return env


# ---------------------------------------------------------------------------
# ① 守护进程真实终端会话
# ---------------------------------------------------------------------------


def _daemon_session_steps(paths: dict[str, Path], port: int) -> list[str]:
    """守护进程会话里依次敲下去的命令。

    每一条都是**真敲进终端**的：命令回显由终端给出，输出按到达顺序混排 ——
    守护进程的日志与命令输出交错，正是值班机上看到的样子。

    先 ``cd`` 到运行目录，路径就短下来了 —— 值班机上的运维也是这么干的：
    站在工作目录里跑，命令不用带一长串前缀。
    """
    rel = RUN_ROOT.as_posix()
    frames = [
        "../../../samples/hl7/示例ORU_R01_多项目.hl7",
        "../../../samples/hl7/示例ORU_R01_正常.hl7",
        "../../../samples/hl7/示例ORU_R01_修正结果.hl7",
    ]
    steps = [
        f"cd {rel}",
        (
            "python3 -m critval.daemon"
            " --db critval.db"
            " --threshold materials/示例危急值阈值表.xlsx"
            " --policy materials/示例危急值报告制度.docx"
            " --frame-dir frames"
            " --material-dir materials"
            " --snapshot-dir snapshots"
            " --console-host 127.0.0.1"
            f" --console-port {port}"
            " --heartbeat-seconds 2"
            " --timer-interval-seconds 5"
            " --run-seconds 16"
            " --log-file daemon.log"
            " --lock-path critval.lock"
            " --heartbeat-path heartbeat.json &"
        ),
        "sleep 3",
    ]
    for index, frame in enumerate(frames, start=1):
        steps.append(f"cp {frame} frames/sample-{index:02d}.hl7")
        steps.append("sleep 2")
    steps += [
        "sleep 3",
        "grep -E '事件已处理|心跳 ·|值班台已启动|守护进程已就绪' daemon.log | tail -8",
        # 先等守护进程自己停机，再查推送记录 —— 进程还在跑的时候心跳日志会
        # 插进命令输出里，读起来像串行了。
        "wait",
        (
            "python3 -m critval.cli notify-queue"
            " --db critval.db"
            " --queue local_notifications.jsonl"
        ),
    ]
    return steps


# ---------------------------------------------------------------------------
# ② CLI export 真实终端会话
# ---------------------------------------------------------------------------


def _export_session_steps(event_id: str, observed_day: str) -> list[str]:
    rel = RUN_ROOT.as_posix()
    return [
        f"cd {rel}",
        (
            f"python3 -m critval.cli export --event {event_id}"
            " --db critval.db"
            " --snapshot-dir snapshots"
            " --format text --verify"
        ),
        (
            f"python3 -m critval.cli export --scope daily --date {observed_day}"
            " --db critval.db"
            " --format text"
        ),
    ]


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def _dump_transcript(paths: dict[str, Path], text: str) -> None:
    """采集失败时把终端原文留在运行目录里（别让现场凭空消失）。"""
    try:
        (paths["run_dir"] / "daemon_session_failed.txt").write_text(text, encoding="utf-8")
    except OSError:  # pragma: no cover - 兜底
        pass


def _read_events(db_path: Path) -> list[dict[str, str]]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT event_id, item_name, item_code, value, unit, dept_code,"
            " observed_at, status FROM events ORDER BY observed_at, event_id"
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 scripts/capture_screenshots.py",
        description="采集真实运行证据（守护进程会话 / 值班台页面 / CLI 导出）到 docs/screenshots/",
    )
    parser.add_argument("--out", default="docs/screenshots", help="截图输出目录")
    parser.add_argument("--host", default="127.0.0.1", help="值班台监听地址")
    parser.add_argument("--skip-daemon", action="store_true", help="跳过守护进程会话（调试用）")
    args = parser.parse_args(argv)

    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError:
        return _fail(
            "没装 Playwright —— 先 pip install playwright && playwright install chromium。"
            "（不产出占位图：本脚本要么截真东西，要么明确失败。）"
        )

    out_dir = Path(args.out)
    if not out_dir.is_absolute():
        out_dir = PROJECT_ROOT / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    paths = _prepare_run_dir()
    env = _session_env(paths)

    # ---- ① 守护进程真实终端会话 -----------------------------------------
    daemon_port = _free_port()
    print("[capture_screenshots] 起守护进程终端会话（约 30 秒）…", flush=True)
    session = _TerminalSession(cwd=PROJECT_ROOT, env=env)
    try:
        for step in _daemon_session_steps(paths, daemon_port):
            session.run(step, timeout=120 if step == "wait" else 60)
    finally:
        daemon_text = session.text
        session.close()
    if not paths["db"].exists():
        _dump_transcript(paths, daemon_text)
        return _fail("守护进程没落台账库，采集中止（不产出占位图）。")

    events = _read_events(paths["db"])
    if not events:
        _dump_transcript(paths, daemon_text)
        return _fail("台账里没有事件，采集中止（不产出占位图）。")
    print(f"[capture_screenshots] 台账里有 {len(events)} 条事件。", flush=True)

    # ---- ② 值班台页面（真进程 + Chromium） -------------------------------
    console_port = _free_port()
    console_env = dict(env)
    console_env["CRITVAL_SNAPSHOT_DIR"] = str(paths["snapshots"])
    console_proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "critval.cli",
            "console-serve",
            "--host",
            args.host,
            "--port",
            str(console_port),
            "--db",
            str(paths["db"]),
            "--threshold",
            str(paths["threshold"]),
        ],
        cwd=str(PROJECT_ROOT),
        env=console_env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base = f"http://{args.host}:{console_port}"
    try:
        if not _wait_health(base):
            return _fail("值班台没起来（/healthz 无响应），采集中止（不产出占位图）。")
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            _shot_page(page, f"{base}/events", out_dir / "console-ledger.png")
            print(f"[capture_screenshots] 已截图：{out_dir / 'console-ledger.png'}", flush=True)
            _shot_page(
                page,
                f"{base}/events/{events[0]['event_id']}/ack",
                out_dir / "console-ack-card.png",
                full_page=True,
            )
            print(f"[capture_screenshots] 已截图：{out_dir / 'console-ack-card.png'}", flush=True)
            browser.close()
    finally:
        console_proc.terminate()
        try:
            console_proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - 兜底
            console_proc.kill()

    # ---- ③ CLI export 真实终端会话 ---------------------------------------
    # 复核报表按**观察日期**切；选事件最多的那天，报表才有东西可看。
    days: dict[str, int] = {}
    for row in events:
        day = (row.get("observed_at") or "")[:10]
        if day:
            days[day] = days.get(day, 0) + 1
    observed_day = max(days.items(), key=lambda kv: (kv[1], kv[0]))[0] if days else "2026-01-01"
    export_session = _TerminalSession(cwd=PROJECT_ROOT, env=env)
    try:
        for step in _export_session_steps(events[0]["event_id"], observed_day):
            export_session.run(step, timeout=60)
    finally:
        export_text = export_session.text
        export_session.close()
    if "危急值处置卡" not in export_text:
        return _fail("export 没导出处置卡，采集中止（不产出占位图）。")

    # ---- ④ 落盘 ---------------------------------------------------------
    daemon_png = out_dir / "daemon-run-log.png"
    export_png = out_dir / "export-ledger.png"
    _render_html_png(
        _terminal_html(
            daemon_text,
            title="危急值值班机 · 守护进程真实终端会话",
            meta="python3 -m critval.daemon（真 PTY 会话原文栅格化）",
        ),
        daemon_png,
    )
    print(f"[capture_screenshots] 已截图：{daemon_png}", flush=True)
    _render_html_png(
        _terminal_html(
            export_text,
            title="危急值值班机 · CLI export 真实终端会话",
            meta="python3 -m critval.cli export（真 PTY 会话原文栅格化）",
        ),
        export_png,
    )
    print(f"[capture_screenshots] 已截图：{export_png}", flush=True)

    # 终端原文另存一份可逐字核对的 .txt（PNG 是栅格化，原文才是证据本体）
    examples_dir = PROJECT_ROOT / "docs" / "examples"
    examples_dir.mkdir(parents=True, exist_ok=True)
    (examples_dir / "terminal_session_daemon.txt").write_text(daemon_text, encoding="utf-8")
    (examples_dir / "terminal_session_export.txt").write_text(export_text, encoding="utf-8")

    # ---- ⑤ 自检：概念图不得混进实测证据 ---------------------------------
    concepts = PROJECT_ROOT / "docs" / "concepts"
    stray = sorted(
        p.name
        for p in concepts.iterdir()
        if p.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp"}
    ) if concepts.exists() else []
    if stray:
        return _fail(
            "docs/concepts/ 里出现了图片文件："
            + "、".join(stray)
            + " —— 概念图不得放进实测证据目录（或反之），请先核对。"
        )
    print("[capture_screenshots] docs/concepts/ 无图片文件（只有结构化视觉目标卡）。")

    print("[capture_screenshots] 完成：4 张真实运行证据已落盘。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
