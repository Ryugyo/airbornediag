"""AirborneDiag 命令行入口。

当前提供使用说明、版本查询、用于验证模型服务的 `llm` 子命令、用于构建和查询 MCU
知识库的 `kb` 子命令，以及用于运行 MCU 诊断工具的 `diag` 子命令。诊断工具产出的是
中间结果，完整的诊断报告与模型分析尚未实现。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, List, Mapping, Optional, Sequence

from jsonschema import Draft202012Validator

from airbornediag import __version__
from airbornediag.config import ConfigError, load_llm_config
from airbornediag.knowledge import (
    DEFAULT_CURATED_DIR,
    DEFAULT_INDEX_PATH,
    DEFAULT_LIMIT,
    DEFAULT_REGISTRY_PATH,
    KnowledgeEntry,
    KnowledgeError,
    KnowledgeIndexError,
    build_index,
    build_match_expression,
    query_index,
    segment,
)
from airbornediag.llm import (
    LLMConnectionError,
    LLMError,
    LLMHTTPError,
    LLMResponseError,
    LLMTimeoutError,
    MindIEClient,
    build_chat_prompt,
)
from airbornediag.mcu import Basis, DiagnosisResult, run_tools, to_jsonable

PROG = "airbornediag"

EXIT_OK = 0
# 2 与 argparse 的用法错误退出码一致，表示参数或配置有问题。
EXIT_CONFIG = 2
EXIT_CONNECTION = 3
EXIT_TIMEOUT = 4
EXIT_HTTP = 5
EXIT_RESPONSE = 6
EXIT_KB_DATA = 7
EXIT_KB_INDEX = 8
# 记录本身合规，但芯片或全部检测对象都不在本版支持范围内，没有任何工具运行。
EXIT_UNSUPPORTED = 9

# 输入契约的 Schema。校验规则以 Schema 为准，此处不重复维护字段约束。
DEFAULT_RECORD_SCHEMA = Path("schemas/fault-record.schema.json")
# 一条记录最多列出多少条 Schema 校验错误，避免输出过长。
SCHEMA_ERROR_LIMIT = 10


class DiagInputError(Exception):
    """故障记录文件缺失、不合 JSON，或未通过 Schema 校验。"""


def _positive_int(raw: str) -> int:
    """argparse 取值函数：只接受正整数。"""
    try:
        value = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError("必须是整数，当前为 {!r}".format(raw))
    if value <= 0:
        raise argparse.ArgumentTypeError("必须是正整数，当前为 {!r}".format(raw))
    return value


def build_parser() -> argparse.ArgumentParser:
    """构造命令行参数解析器。"""
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="面向民机机载系统的智能故障诊断工具。",
        epilog=(
            "当前提供模型服务调用验证、知识库构建与查询，以及 MCU 诊断工具；"
            "完整的诊断报告与模型分析尚未实现。"
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version="%(prog)s {}".format(__version__),
        help="显示工程版本并退出。",
    )

    subparsers = parser.add_subparsers(dest="command", metavar="命令")

    diag = subparsers.add_parser(
        "diag",
        help="对一条故障记录运行 MCU 诊断工具并打印工具结果。",
        description=(
            "对一条故障记录运行本版 MCU 诊断工具，打印工具判断、观测依据、规则来源"
            "与缺失信息。这是诊断流程的中间结果，不是完整诊断报告：候选根因与"
            "知识检索由后续环节完成，本命令不访问模型服务。"
        ),
        epilog=(
            "失败时按类型返回不同的退出码：{} 记录缺失、不合 JSON 或未通过 Schema "
            "校验，{} 芯片或全部检测对象不在本版支持范围内。".format(
                EXIT_CONFIG, EXIT_UNSUPPORTED
            )
        ),
    )
    diag.add_argument(
        "record",
        help="故障记录 JSON 文件的路径，例如 examples/REC-2026-0918-002.json。",
    )
    diag.add_argument(
        "--json",
        action="store_true",
        help="以 JSON 输出工具结果，便于后续流程直接读取。",
    )
    diag.add_argument(
        "--schema",
        default=str(DEFAULT_RECORD_SCHEMA),
        help="输入契约的 Schema 路径，默认为 {}。".format(DEFAULT_RECORD_SCHEMA),
    )

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

    kb = subparsers.add_parser(
        "kb",
        help="构建和查询 MCU 知识库的全文索引。",
        description=(
            "构建和查询 MCU 知识库的全文索引。索引由本地 SQLite 的 FTS5 实现，"
            "运行期只查询已构建的索引，不重新解析原始手册。本命令不访问模型服务。"
        ),
    )
    kb_subparsers = kb.add_subparsers(dest="kb_command", metavar="操作")

    kb_build = kb_subparsers.add_parser(
        "build",
        help="按 knowledge/curated/ 下的知识文件全量重建索引。",
        description=(
            "按 knowledge/curated/ 下的知识文件全量重建索引。每次构建都重新创建索引表，"
            "因此重复执行不会产生重复条目，修改知识文件后重新构建即可生效。"
        ),
    )
    kb_build.add_argument(
        "--db",
        default=str(DEFAULT_INDEX_PATH),
        help="索引文件路径，默认为 {}。".format(DEFAULT_INDEX_PATH),
    )
    kb_build.add_argument(
        "--curated-dir",
        default=str(DEFAULT_CURATED_DIR),
        help="知识文件目录，默认为 {}。".format(DEFAULT_CURATED_DIR),
    )
    kb_build.add_argument(
        "--registry",
        default=str(DEFAULT_REGISTRY_PATH),
        help="来源登记表路径，默认为 {}。".format(DEFAULT_REGISTRY_PATH),
    )

    kb_query = kb_subparsers.add_parser(
        "query",
        help="查询知识库，打印命中的知识条目及其出处。",
        description=(
            "按关键词查询知识库并打印命中的条目、内容与出处。查询词可中英混用，"
            "汉字按子串匹配，英文寄存器名按词匹配。无匹配时返回空结果并提示，退出码仍为 0。"
        ),
        epilog=(
            "失败时按类型返回不同的退出码：{} 知识文件或来源登记表有问题，"
            "{} 索引缺失、损坏或版本不符。".format(EXIT_KB_DATA, EXIT_KB_INDEX)
        ),
    )
    kb_query.add_argument(
        "text",
        nargs="?",
        help="查询关键词。省略时从标准输入读取。",
    )
    kb_query.add_argument(
        "--chip",
        help="按芯片过滤，精确匹配且忽略大小写，例如 MPC5554。",
    )
    kb_query.add_argument(
        "--peripheral",
        help="按外设过滤，精确匹配且忽略大小写，例如 FlexCAN2。",
    )
    kb_query.add_argument(
        "--limit",
        type=_positive_int,
        default=DEFAULT_LIMIT,
        help="返回条数上限，默认为 {}。".format(DEFAULT_LIMIT),
    )
    kb_query.add_argument(
        "--db",
        default=str(DEFAULT_INDEX_PATH),
        help="索引文件路径，默认为 {}。".format(DEFAULT_INDEX_PATH),
    )
    # 供 main 在只给出 `kb` 而未给出操作时打印该子命令的用法。
    parser.kb_parser = kb
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


def _knowledge_exit_code(error: KnowledgeError) -> int:
    """知识库错误对应的退出码。"""
    if isinstance(error, KnowledgeIndexError):
        return EXIT_KB_INDEX
    return EXIT_KB_DATA


def _run_kb_build(args: argparse.Namespace) -> int:
    """执行 kb build 子命令，返回进程退出码。"""
    try:
        count = build_index(
            db_path=Path(args.db),
            curated_dir=Path(args.curated_dir),
            registry_path=Path(args.registry),
        )
    except KnowledgeError as error:
        print("知识库错误：{}".format(error), file=sys.stderr)
        return _knowledge_exit_code(error)

    print("已写入 {} 条知识条目：{}".format(count, args.db))
    return EXIT_OK


def _format_hit(ordinal: int, entry: KnowledgeEntry) -> str:
    """把一条命中结果排成可读的多行文本。"""
    lines = [
        "[{}] {}".format(ordinal, entry.entry_id),
        "标题：{}".format(entry.title),
        "芯片/外设：{} / {}    类型：{}".format(entry.chip, entry.peripheral, entry.kind),
        "依据：{}".format(entry.origin),
    ]
    if entry.quote:
        lines.append("原文：{}".format(entry.quote))
    lines.append("内容：{}".format(entry.body))
    return "\n".join(lines)


def _run_kb_query(args: argparse.Namespace) -> int:
    """执行 kb query 子命令，返回进程退出码。

    命中的条目走标准输出，查询条件与命中数走标准错误，便于把结果直接接到下游。
    """
    text = _resolve_prompt(args.text)
    if text is None:
        print("没有可查询的文本：请在命令行给出查询词，或通过标准输入传入。", file=sys.stderr)
        return EXIT_CONFIG
    if not segment(text).split():
        print("查询内容中没有可检索的文字：{!r}".format(text), file=sys.stderr)
        return EXIT_CONFIG

    try:
        hits = query_index(
            text,
            db_path=Path(args.db),
            chip=args.chip,
            peripheral=args.peripheral,
            limit=args.limit,
        )
    except KnowledgeError as error:
        print("知识库错误：{}".format(error), file=sys.stderr)
        return _knowledge_exit_code(error)

    print("索引：{}".format(args.db), file=sys.stderr)
    print("查询：{}".format(build_match_expression(text)), file=sys.stderr)
    filters: List[str] = []
    if args.chip:
        filters.append("芯片={}".format(args.chip))
    if args.peripheral:
        filters.append("外设={}".format(args.peripheral))
    if filters:
        print("过滤：{}".format("  ".join(filters)), file=sys.stderr)
    print("命中：{} 条（上限 {}）".format(len(hits), args.limit), file=sys.stderr)

    if not hits:
        print("没有匹配的知识条目。", file=sys.stderr)
        return EXIT_OK

    print("\n\n".join(_format_hit(index, entry) for index, entry in enumerate(hits, start=1)))
    return EXIT_OK


def _read_json(path: Path, description: str) -> Any:
    """读取 JSON 文件，失败时抛出带文件名的输入错误。"""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise DiagInputError("{}不存在：{}".format(description, path))
    except (OSError, UnicodeDecodeError) as error:
        raise DiagInputError("读取{} {} 失败：{}".format(description, path, error))
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        raise DiagInputError("{} {} 不是合法 JSON：{}".format(description, path, error))


def _load_record(record_path: Path, schema_path: Path) -> Mapping[str, Any]:
    """读取故障记录并按输入契约的 Schema 校验。

    字段约束以 Schema 为准，这里不重复实现字段规则。Schema 中的 format 关键字只在
    安装了对应格式校验器时才执行，因此日期时间格式与报告校验一样，仍然只按字符串校验。
    """
    record = _read_json(record_path, "故障记录")
    if not isinstance(record, dict):
        raise DiagInputError("故障记录 {} 的顶层必须是 JSON 对象。".format(record_path))

    schema = _read_json(schema_path, "Schema 文件")
    errors = sorted(
        Draft202012Validator(schema).iter_errors(record),
        key=lambda item: item.json_path,
    )
    if errors:
        lines = ["故障记录 {} 未通过 Schema 校验：".format(record_path)]
        for error in errors[:SCHEMA_ERROR_LIMIT]:
            message = error.message
            if len(message) > 160:
                message = message[:157] + "..."
            lines.append("  - {}: {}".format(error.json_path, message))
        if len(errors) > SCHEMA_ERROR_LIMIT:
            lines.append("  - 另有 {} 条未列出".format(len(errors) - SCHEMA_ERROR_LIMIT))
        raise DiagInputError("\n".join(lines))
    return record


def _format_basis(basis: Basis) -> str:
    """依据的可读形式：文献出处，附对应的知识条目 id。"""
    text = str(basis)
    if basis.knowledge_refs:
        text += "（知识条目 {}）".format("、".join(basis.knowledge_refs))
    return text


def _format_diagnosis(result: DiagnosisResult) -> str:
    """把工具结果排成可读文本。"""
    lines = [
        "记录：{}".format(result.record_id or "（缺省）"),
        "芯片：{}".format(result.chip or "（缺省）"),
    ]
    if not result.supported:
        lines.append("")
        lines.append("本次没有可用的诊断工具，未给出任何结论。")

    for index, item in enumerate(result.tool_results, start=1):
        lines.append("")
        lines.append(
            "[{}] {}    外设 {}    对象 {}".format(
                index, item.tool, item.peripheral, item.target
            )
        )
        lines.append("    使用字段：{}".format("、".join(item.used_fields) or "（无）"))

        if item.confirmed_states:
            lines.append("    确认状态：")
            for state in item.confirmed_states:
                lines.append("      - {}".format(state.id))
                lines.append("        依据：{}".format(_format_basis(state.basis)))
                lines.append("        证据：{}".format("、".join(state.evidence)))
                lines.append("        {}".format(state.statement))

        if item.inconsistencies:
            lines.append("    输入矛盾：")
            for conflict in item.inconsistencies:
                lines.append("      - {}".format(conflict.id))
                lines.append("        依据：{}".format(_format_basis(conflict.basis)))
                lines.append("        证据：{}".format("、".join(conflict.evidence)))
                lines.append("        {}".format(conflict.statement))

        if item.insufficient_data:
            lines.append("    缺失信息：")
            for missing in item.insufficient_data:
                lines.append("      - 缺少 {}".format(missing.missing))
                if missing.basis is not None:
                    lines.append("        判定依据：{}".format(_format_basis(missing.basis)))
                lines.append("        {}".format(missing.reason))

        if item.unsupported:
            lines.append("    不支持：")
            for unsupported in item.unsupported:
                lines.append("      - {}".format(unsupported.subject))
                lines.append("        {}".format(unsupported.reason))

        if item.is_empty:
            if item.used_fields:
                lines.append("    本次未产生结论：已使用的字段未触发任何判据。")
            else:
                lines.append("    本次未产生结论：没有本工具可判读的寄存器位域。")
            lines.append("    未触发判据不等于该外设正常。")

    if result.unsupported:
        lines.append("")
        lines.append("本版不支持：")
        for unsupported in result.unsupported:
            lines.append("  - {}".format(unsupported.subject))
            lines.append("    {}".format(unsupported.reason))

    return "\n".join(lines)


def _run_diag(args: argparse.Namespace) -> int:
    """执行 diag 子命令，返回进程退出码。"""
    try:
        record = _load_record(Path(args.record), Path(args.schema))
    except DiagInputError as error:
        print("输入错误：{}".format(error), file=sys.stderr)
        return EXIT_CONFIG

    result = run_tools(record)

    if args.json:
        print(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2))
    else:
        print(_format_diagnosis(result))

    counts = result.counts()
    print(
        "工具结果：{}".format("，".join("{} {}".format(key, value) for key, value in counts.items())),
        file=sys.stderr,
    )
    if not result.supported:
        print("芯片或全部检测对象不在本版支持范围内，未运行任何工具。", file=sys.stderr)
    print(
        "说明：记录中未观测的字段、以及本版没有判据的字段，均视为未知，不作为正常。",
        file=sys.stderr,
    )
    return EXIT_OK if result.supported else EXIT_UNSUPPORTED


def main(argv: Optional[Sequence[str]] = None) -> int:
    """命令行入口函数，返回进程退出码。"""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "llm":
        return _run_llm(args)

    if args.command == "diag":
        return _run_diag(args)

    if args.command == "kb":
        if args.kb_command == "build":
            return _run_kb_build(args)
        if args.kb_command == "query":
            return _run_kb_query(args)
        # 只给出 kb 而未给出操作时打印该子命令的用法。
        parser.kb_parser.print_help()
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
