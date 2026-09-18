"""AirborneDiag 命令行入口。

当前仅提供使用说明与版本查询，诊断功能尚未实现。
"""

from __future__ import annotations

import argparse
from typing import Optional, Sequence

from airbornediag import __version__

PROG = "airbornediag"


def build_parser() -> argparse.ArgumentParser:
    """构造命令行参数解析器。"""
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="面向民机机载系统的智能故障诊断工具。",
        epilog="当前版本仅提供命令行入口，诊断功能尚未实现。",
    )
    parser.add_argument(
        "--version",
        action="version",
        version="%(prog)s {}".format(__version__),
        help="显示工程版本并退出。",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """命令行入口函数，返回进程退出码。"""
    parser = build_parser()
    parser.parse_args(argv)
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
