"""AirborneDiag 命令行入口。

当前提供使用说明、版本查询，以及用于验证模型服务的 `llm` 子命令。
故障诊断功能尚未实现。
"""

from __future__ import annotations

import argparse
import sys
from typing import Optional, Sequence

from airbornediag import __version__
from airbornediag.config import ConfigError, load_llm_config
from airbornediag.llm import (
    LLMConnectionError,
    LLMError,
    LLMHTTPError,
    LLMResponseError,
    LLMTimeoutError,
    MindIEClient,
    build_chat_prompt,
)

PROG = "airbornediag"

EXIT_OK = 0
# 2 与 argparse 的用法错误退出码一致，表示参数或配置有问题。
EXIT_CONFIG = 2
EXIT_CONNECTION = 3
EXIT_TIMEOUT = 4
EXIT_HTTP = 5
EXIT_RESPONSE = 6


def build_parser() -> argparse.ArgumentParser:
    """构造命令行参数解析器。"""
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="面向民机机载系统的智能故障诊断工具。",
        epilog="当前提供命令行入口与模型服务调用验证，故障诊断功能尚未实现。",
    )
    parser.add_argument(
        "--version",
        action="version",
        version="%(prog)s {}".format(__version__),
        help="显示工程版本并退出。",
    )

    subparsers = parser.add_subparsers(dest="command", metavar="命令")

    llm = subparsers.add_parser(
        "llm",
        help="向 MindIE 服务发送一段文本并打印回答。",
        description=(
            "向 MindIE 服务发送一段文本并打印回答，用于验证模型服务连通性。"
            "这是模型调用验证，不是故障诊断：本命令不读取故障记录，也不产出诊断报告。"
        ),
        epilog=(
            "失败时按类型返回不同的退出码：{} 连接失败，{} 超时，{} HTTP 错误，"
            "{} 响应格式异常。".format(EXIT_CONNECTION, EXIT_TIMEOUT, EXIT_HTTP, EXIT_RESPONSE)
        ),
    )
    llm.add_argument(
        "--prompt",
        "-p",
        help="要发送的文本。省略时从标准输入读取。",
    )
    llm.add_argument(
        "--system",
        help="可选的 system 提示文本，用于构造 Qwen2.5 的 ChatML 提示。",
    )
    llm.add_argument(
        "--max-tokens",
        type=int,
        help="单次请求最大生成 token 数，默认为配置值。",
    )
    llm.add_argument(
        "--timeout",
        type=float,
        help="单次请求超时秒数，默认为配置值。",
    )
    llm.add_argument(
        "--base-url",
        help="覆盖服务地址，例如 http://127.0.0.1:1025。",
    )
    return parser


def _resolve_prompt(argument: Optional[str]) -> Optional[str]:
    """取得待发送文本：优先取命令行参数，否则读标准输入。"""
    if argument is not None:
        return argument if argument.strip() else None
    if sys.stdin is None or sys.stdin.isatty():
        return None
    text = sys.stdin.read()
    return text if text.strip() else None


def _run_llm(args: argparse.Namespace) -> int:
    """执行 llm 子命令，返回进程退出码。"""
    try:
        config = load_llm_config(
            base_url=args.base_url,
            max_tokens=args.max_tokens,
            timeout=args.timeout,
        )
    except ConfigError as error:
        print("配置错误：{}".format(error), file=sys.stderr)
        return EXIT_CONFIG

    prompt = _resolve_prompt(args.prompt)
    if prompt is None:
        print("没有可发送的文本：请用 --prompt 指定，或通过标准输入传入。", file=sys.stderr)
        return EXIT_CONFIG

    # 诊断信息走标准错误，标准输出只留回答，便于管道使用。
    print("端点：{}".format(config.endpoint), file=sys.stderr)
    print("模型：{}".format(config.model), file=sys.stderr)
    if config.api_key:
        print("认证：已配置（值不显示）", file=sys.stderr)

    client = MindIEClient(config)
    full_prompt = build_chat_prompt(prompt, args.system)
    try:
        response = client.generate(full_prompt)
    except LLMTimeoutError as error:
        print("请求超时：{}".format(error), file=sys.stderr)
        return EXIT_TIMEOUT
    except LLMConnectionError as error:
        print("连接失败：{}".format(error), file=sys.stderr)
        print("请确认 MindIE 服务已启动，且该地址从本机可达。", file=sys.stderr)
        return EXIT_CONNECTION
    except LLMHTTPError as error:
        print("HTTP 错误：{}".format(error), file=sys.stderr)
        return EXIT_HTTP
    except LLMResponseError as error:
        print("响应格式异常：{}".format(error), file=sys.stderr)
        return EXIT_RESPONSE
    except LLMError as error:  # 兜底，避免把异常栈直接抛给使用者
        print("调用失败：{}".format(error), file=sys.stderr)
        return EXIT_RESPONSE

    print("耗时：{:.2f} s".format(response.elapsed_seconds), file=sys.stderr)
    if not response.text:
        print("注意：服务返回了空回答。", file=sys.stderr)
    print(response.text)
    return EXIT_OK


def main(argv: Optional[Sequence[str]] = None) -> int:
    """命令行入口函数，返回进程退出码。"""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "llm":
        return _run_llm(args)

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
