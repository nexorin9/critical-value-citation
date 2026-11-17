"""运维 CLI 的统一框架：子命令注册表 + 统一 ``--json`` 输出 + 统一退出码。

运维通道（CLI）与主路径（常驻守护进程）是两回事：主路径负责「危急值来了怎么办」，
CLI 负责「运维怎么问它、怎么让它把话说明白」。本模块把散在 30 多个子命令里的
三件约定收敛到**一个入口**，让运维脚本与值班台可以按同一套契约解析输出：

* **子命令注册表** —— 每个子命令的「名字 / 帮助文本 / 参数 / 处理函数」登记在
  一张表里（``critval/cli_specs.py`` 放参数表，本模块放框架）。新增子命令只改
  表，不改解析器；解析器、帮助、派发都由注册表统一生成。
* **统一 ``--json`` 输出约定** —— 带 ``--json`` 的子命令，stdout 上**只有一个**
  JSON 文档（可被 ``json.loads(out)`` 直接解析）；文档顶层固定带三个保留键
  ``command`` / ``ok`` / ``exit_code``，子命令自己的数据键平铺在同一层。
  保留键的语义与源产品「把散落的辅助逻辑收敛到一个统一入口、统一返回结构」
  的做法同构（见 ``spec.md`` → ``## 开源对照``）。
* **统一退出码约定** —— ``0`` 成功 / ``1`` 业务异常 / ``2`` 参数错。
  参数错由 ``argparse`` 自身报出（argparse 默认就是 2），业务异常由本模块的
  ``run()`` 兜住：子命令只管返回约定的三个码之一，不用各自 try/except。

为什么值得单独做这一层：三十多个子命令各写各的输出格式时，运维侧只能靠
「人肉看终端」判断成败；统一之后，一条 ``python -m critval.cli xxx --json``
既能被人读，也能被脚本判成败 —— ``exit_code`` 字段让脚本不必解析业务文案。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence

__all__ = [
    "EXIT_BUSINESS",
    "EXIT_CONTRACT",
    "EXIT_OK",
    "EXIT_USAGE",
    "RESERVED_JSON_KEYS",
    "ArgSpec",
    "CommandRegistry",
    "CommandSpec",
    "emit_json",
    "emit_text",
]

#: 退出码约定：成功 / 业务异常 / 参数错。
EXIT_OK = 0
EXIT_BUSINESS = 1
EXIT_USAGE = 2

#: 退出码的人话说明（``--help`` 与 README 共用一份口径）。
EXIT_CONTRACT: dict[int, str] = {
    EXIT_OK: "成功",
    EXIT_BUSINESS: "业务异常",
    EXIT_USAGE: "参数错",
}

#: ``--json`` 文档里由框架占用的顶层键 —— 子命令自己的数据键不许占用这些名字。
RESERVED_JSON_KEYS = ("command", "ok", "exit_code")

Handler = Callable[[argparse.Namespace], int]

#: 当前正在执行的子命令名。``run()`` 派发时写入 —— ``emit_json`` 拿不到
#: 命令名时从这里兜底（子命令自己不必重复写一遍命令名）。
_CURRENT_COMMAND = {"name": ""}


def set_current_command(name: str) -> None:
    """登记当前执行的子命令名（框架内部使用）。"""
    _CURRENT_COMMAND["name"] = name


def current_command() -> str:
    """当前执行的子命令名（不在子命令里执行时返回空串）。"""
    return _CURRENT_COMMAND["name"]


@dataclass(frozen=True)
class ArgSpec:
    """一个命令行参数的声明。

    ``flags`` 是选项名（``("--db",)``），``kwargs`` 原样交给
    ``argparse.add_argument``（``help`` / ``default`` / ``required`` /
    ``choices`` / ``type`` / ``dest`` / ``action``）。
    """

    flags: tuple[str, ...]
    kwargs: dict[str, Any] = field(default_factory=dict)

    def add_to(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(*self.flags, **self.kwargs)


@dataclass
class CommandSpec:
    """一个子命令的完整声明。"""

    name: str
    help: str
    handler: Handler
    args: tuple[ArgSpec, ...] = ()
    #: 嵌套子动作的接线函数（``parse-queue`` 的 list / confirm / ignore 用）。
    add_args: Optional[Callable[[argparse.ArgumentParser], None]] = None
    #: 是否支持 ``--json``（常驻服务类命令可以关掉）。
    json_output: bool = True
    description: str = ""


class CommandRegistry:
    """子命令注册表 —— CLI 的唯一入口。

    用法：登记 ``CommandSpec`` → ``build_parser()`` 生成解析器 →
    ``run(argv)`` 派发。注册表本身不碰业务逻辑，只管「命令长什么样、
    输出长什么样、退出码是什么」。
    """

    def __init__(self, prog: str = "python -m critval.cli", description: str = "") -> None:
        self._specs: dict[str, CommandSpec] = {}
        self._prog = prog
        self._description = description

    # -- 登记 -------------------------------------------------------------

    def register(self, spec: CommandSpec) -> CommandSpec:
        if spec.name in self._specs:
            raise ValueError(f"子命令重复登记：{spec.name}")
        self._specs[spec.name] = spec
        return spec

    def get(self, name: str) -> CommandSpec:
        try:
            return self._specs[name]
        except KeyError:
            raise KeyError(f"没有这个子命令：{name}") from None

    def names(self) -> list[str]:
        return list(self._specs)

    def specs(self) -> list[CommandSpec]:
        return list(self._specs.values())

    # -- 解析器 -----------------------------------------------------------

    def build_parser(self) -> argparse.ArgumentParser:
        """按注册表生成解析器 —— 每个子命令的 ``--help`` 都可用。"""
        parser = argparse.ArgumentParser(prog=self._prog, description=self._description)
        subparsers = parser.add_subparsers(dest="command", metavar="子命令")
        for spec in self.specs():
            sub = subparsers.add_parser(
                spec.name,
                help=spec.help,
                description=spec.description or spec.help,
                formatter_class=argparse.RawDescriptionHelpFormatter,
            )
            for arg in spec.args:
                arg.add_to(sub)
            if spec.add_args is not None:
                spec.add_args(sub)
            if spec.json_output and "--json" not in sub._option_string_actions:
                sub.add_argument(
                    "--json",
                    action="store_true",
                    help="以 JSON 输出（机器可读；顶层带 command / ok / exit_code）",
                )
        return parser

    # -- 派发 -------------------------------------------------------------

    def run(self, argv: Optional[Sequence[str]] = None) -> int:
        """执行一个子命令并返回约定内的退出码。"""
        parser = self.build_parser()
        args = parser.parse_args(argv)
        if not getattr(args, "command", None):
            parser.print_help()
            return EXIT_USAGE
        spec = self.get(args.command)
        set_current_command(spec.name)
        try:
            code = spec.handler(args)
        except SystemExit:
            raise
        except FileNotFoundError as exc:
            print(f"文件不存在：{exc}", file=sys.stderr)
            return EXIT_USAGE
        except RuntimeError as exc:
            print(f"运行失败：{exc}", file=sys.stderr)
            return EXIT_BUSINESS
        if code not in EXIT_CONTRACT:
            print(
                f"子命令 {spec.name} 返回了约定外的退出码：{code}",
                file=sys.stderr,
            )
            return EXIT_BUSINESS
        return code


def emit_json(
    payload: dict[str, Any],
    command: str = "",
    ok: bool = True,
    exit_code: int = EXIT_OK,
) -> None:
    """统一 ``--json`` 输出：stdout 上只有一个 JSON 文档。

    顶层固定带 ``command`` / ``ok`` / ``exit_code`` 三个保留键；子命令自己的
    数据键平铺在同一层（不套 ``data`` —— 运维脚本读 ``payload["hits"]`` 就够，
    不必多跳一层）。

    ``command`` 留空时自动取当前执行的子命令名（框架在派发时登记）。
    """
    document: dict[str, Any] = dict(payload or {})
    document.setdefault("command", command or current_command())
    document.setdefault("ok", ok)
    document.setdefault("exit_code", exit_code)
    print(json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True))


def emit_text(text: str) -> None:
    """统一纯文本输出（``--json`` 之外的默认形态）。"""
    print(text)
