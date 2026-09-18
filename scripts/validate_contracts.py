"""校验故障记录与诊断报告的格式及引用关系。

字段、类型、枚举与分支约束由 schemas/ 下的 JSON Schema 声明，本脚本用
jsonschema 实际执行这些 Schema，不自行实现 Schema 解释器，也不重复维护
结构规则。脚本自身只补充 Schema 表达不了的跨文档关系：

- 观测 id 在一条记录内唯一、record_id 在 examples/ 内唯一；
- 报告引用的观测 id 必须存在于本报告的 observations 中；
- 结论引用的文献来源必须登记在 knowledge/source-registry.json 中；
- 报告的 device、test、observations 必须与输入记录一致（模型不得改写）；
- 每个输入记录都有对应的期望报告。

本脚本不实现任何芯片诊断规则，也不判断诊断结论是否正确。
校验通过不代表诊断正确。

用法：
    python scripts/validate_contracts.py [--root 工程根目录]

全部检查通过时退出码为 0，存在问题时打印问题清单并返回 1。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

REPO_ROOT = Path(__file__).resolve().parents[1]

SCHEMA_DIR = "schemas"
RECORD_SCHEMA = "fault-record.schema.json"
REPORT_SCHEMA = "diagnostic-report.schema.json"
REGISTRY = "knowledge/source-registry.json"
EXAMPLES_DIR = "examples"
FIXTURES_DIR = "tests/fixtures"

# 报告 Schema 用相对 $ref（fault-record.schema.json#/$defs/...）引用输入 Schema。
# 顶层 Schema 没有 $id，解析基线为空 URI，相对引用按原样解析，因此以文件名作为
# 注册表的键即可让跨文件 $ref 完全在本地解析：内容由本脚本从 schemas/ 读入，
# 注册表未命中时 jsonschema 直接抛错，不发起任何网络请求。
SCHEMA_RESOURCES = (RECORD_SCHEMA, REPORT_SCHEMA)

FORMAT_CHECKER = Draft202012Validator.FORMAT_CHECKER

# oneOf/anyOf 失败时，一条错误最多列出多少条分支内约束，避免输出过长。
# 取 20 是留足余量：四个观测分支全部不成立时通常也只有十来条。
BRANCH_DETAIL_LIMIT = 20


class Problems:
    """收集校验问题，避免在第一个错误处中断。"""

    def __init__(self) -> None:
        self.items: List[str] = []

    def add(self, where: str, message: str) -> None:
        self.items.append("{}: {}".format(where, message))

    def __bool__(self) -> bool:
        return bool(self.items)


def load_json(path: Path, problems: Problems) -> Optional[Any]:
    """读取 JSON 文件，失败时记录问题并返回 None。"""
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        problems.add(str(path), "文件不存在")
    except UnicodeDecodeError as error:
        problems.add(str(path), "不是有效的 UTF-8 文本：{}".format(error))
    except json.JSONDecodeError as error:
        problems.add(str(path), "JSON 解析失败：{}".format(error))
    return None


def json_files(directory: Path) -> List[Path]:
    """返回目录下的 JSON 文件，按名称排序。"""
    if not directory.is_dir():
        return []
    return sorted(directory.glob("*.json"))


def flatten(error: Any) -> List[Any]:
    """把错误的嵌套上下文摊平成叶子错误：叶子才是真正不成立的那条约束。"""
    if not error.context:
        return [error]
    leaves: List[Any] = []
    for child in error.context:
        leaves.extend(flatten(child))
    return leaves


def shorten(message: str) -> str:
    if len(message) > 160:
        return message[:157] + "..."
    return message


def describe(error: Any) -> str:
    """把一条 Schema 校验错误渲染成可读文本。

    oneOf/anyOf 失败时，jsonschema 的父错误会把整个实例打印出来，既长又不说清
    哪条约束不成立。这里改为列出各分支中不成立的约束（去重后最多若干条），由
    阅读者判断哪条是真正的原因。不做“猜哪条分支最接近”的启发式：分支之间的
    差异只有 Schema 知道，脚本不去重复这套知识。
    """
    if error.validator in ("oneOf", "anyOf") and error.context:
        lines = ["{}: 不匹配 {} 的任何分支，以下约束均未满足：".format(error.json_path, error.validator)]
        leaves = flatten(error)
        # 越靠近具体字段的约束越可能是真正的原因，先列深的。
        leaves.sort(key=lambda leaf: (-len(list(leaf.absolute_path)), leaf.json_path))
        details: List[str] = []
        for leaf in leaves:
            item = "{}: {}".format(leaf.json_path, shorten(leaf.message))
            if item not in details:
                details.append(item)
        for item in details[:BRANCH_DETAIL_LIMIT]:
            lines.append("· {}".format(item))
        if len(details) > BRANCH_DETAIL_LIMIT:
            lines.append("· 另有 {} 条未列出".format(len(details) - BRANCH_DETAIL_LIMIT))
        return "\n".join(lines)
    return "{}: {}".format(error.json_path, shorten(error.message))


def check_schema(
    where: str,
    validator: Optional[Draft202012Validator],
    instance: Any,
    problems: Problems,
) -> None:
    """用 Schema 校验一份实例，把错误计入问题清单。"""
    if validator is None:
        return
    for error in sorted(validator.iter_errors(instance), key=lambda item: item.json_path):
        problems.add(where, "Schema 校验失败：{}".format(describe(error)))


def build_schemas(root: Path, problems: Problems) -> Dict[str, Any]:
    """读取 schemas/ 下的 Schema 文件。"""
    schemas: Dict[str, Any] = {}
    for name in SCHEMA_RESOURCES:
        contents = load_json(root / SCHEMA_DIR / name, problems)
        if isinstance(contents, dict):
            schemas[name] = contents
    return schemas


def build_validators(schemas: Dict[str, Any]) -> Dict[str, Draft202012Validator]:
    """为输入契约与输出契约各构造一个校验器。

    两个 Schema 注册进同一份本地引用注册表，报告 Schema 中的跨文件 $ref 由此
    在本地解析，不访问网络。
    """
    registry = Registry().with_resources(
        (name, Resource.from_contents(contents)) for name, contents in schemas.items()
    )
    return {
        name: Draft202012Validator(contents, registry=registry, format_checker=FORMAT_CHECKER)
        for name, contents in schemas.items()
    }


def check_observation_ids(where: str, observations: Any, problems: Problems) -> List[str]:
    """检查观测 id 在一条记录内唯一，返回观测 id 列表。

    唯一性按字段取值判定，JSON Schema 无法表达，因此保留在脚本中。
    """
    ids: List[str] = []
    if not isinstance(observations, list):
        return ids
    for index, observation in enumerate(observations):
        if not isinstance(observation, dict):
            continue
        observation_id = observation.get("id")
        if not isinstance(observation_id, str):
            continue
        if observation_id in ids:
            problems.add(where, "observations[{}] 的观测 id {} 重复".format(index, observation_id))
        ids.append(observation_id)
    return ids


def check_evidence_refs(where: str, report: Dict[str, Any], observation_ids: List[str], problems: Problems) -> None:
    """检查报告中的证据引用是否都指向本报告内的观测。"""
    analysis = report.get("analysis", {})
    if not isinstance(analysis, dict):
        return
    references: List[tuple] = []

    for index, state in enumerate(analysis.get("confirmed_states", [])):
        references.append(("confirmed_states[{}].evidence".format(index), state.get("evidence")))
    for index, cause in enumerate(analysis.get("candidate_causes", [])):
        references.append(("candidate_causes[{}].supporting".format(index), cause.get("supporting")))
        references.append(("candidate_causes[{}].contradicting".format(index), cause.get("contradicting")))
    for index, item in enumerate(analysis.get("inconsistencies", [])):
        references.append(("inconsistencies[{}].evidence".format(index), item.get("evidence")))

    for label, values in references:
        if not isinstance(values, list):
            continue
        for value in values:
            if value not in observation_ids:
                problems.add(where, "{} 引用了不存在的观测 {}".format(label, value))


def collect_sources(report: Dict[str, Any]) -> List[str]:
    """收集报告中引用到的全部文献来源标识。"""
    analysis = report.get("analysis", {})
    if not isinstance(analysis, dict):
        return []
    sources: List[str] = []
    for state in analysis.get("confirmed_states", []):
        basis = state.get("basis")
        if isinstance(basis, dict) and isinstance(basis.get("source"), str):
            sources.append(basis["source"])
    return sources


def check_records(
    root: Path,
    validators: Dict[str, Draft202012Validator],
    problems: Problems,
) -> Dict[str, Dict[str, Any]]:
    """校验输入记录，返回 record_id 到记录的映射。"""
    records: Dict[str, Dict[str, Any]] = {}
    paths = json_files(root / EXAMPLES_DIR)
    if not paths:
        problems.add(str(root / EXAMPLES_DIR), "没有找到输入记录")

    for path in paths:
        record = load_json(path, problems)
        where = path.name
        check_schema(where, validators.get(RECORD_SCHEMA), record, problems)

        if not isinstance(record, dict):
            continue

        record_id = record.get("record_id")
        if isinstance(record_id, str) and record_id:
            if record_id in records:
                problems.add(where, "record_id {} 重复".format(record_id))
            records[record_id] = record
        else:
            problems.add(where, "record_id 缺失或不是非空字符串")

        check_observation_ids(where, record.get("observations", []), problems)

    return records


def check_reports(
    root: Path,
    records: Dict[str, Dict[str, Any]],
    validators: Dict[str, Draft202012Validator],
    sources: List[str],
    problems: Problems,
) -> None:
    """校验期望报告的回显一致性与引用关系，结构由 Schema 校验。"""
    reports = json_files(root / FIXTURES_DIR)
    if not reports:
        problems.add(str(root / FIXTURES_DIR), "没有找到期望报告")

    referenced: List[str] = []

    for path in reports:
        report = load_json(path, problems)
        where = path.name
        check_schema(where, validators.get(REPORT_SCHEMA), report, problems)

        if not isinstance(report, dict):
            continue

        record_ref = report.get("record_ref")
        if not isinstance(record_ref, str) or record_ref not in records:
            problems.add(where, "record_ref {} 未指向存在的输入记录".format(record_ref))
            continue
        referenced.append(record_ref)

        record = records[record_ref]

        # 主程序按输入记录的字段值保留这三项，模型不得改写；
        # 输入缺省 observations 时，报告使用已解析出的空数组。
        for key in ("device", "test"):
            if report.get(key) != record.get(key):
                problems.add(where, "{} 与输入记录不一致，模型不得改写该字段".format(key))
        if report.get("observations") != record.get("observations", []):
            problems.add(where, "observations 与输入记录不一致，模型不得改写该字段")

        provenance = report.get("provenance", {})
        if isinstance(provenance, dict) and provenance.get("input_origin") != record.get("origin"):
            problems.add(where, "provenance.input_origin 与输入记录的 origin 不一致")

        observation_ids = check_observation_ids(where, report.get("observations", []), problems)
        check_evidence_refs(where, report, observation_ids, problems)

        for source in collect_sources(report):
            if source not in sources:
                problems.add(where, "basis.source {} 未登记在来源登记表中".format(source))

    for record_id in records:
        if record_id not in referenced:
            problems.add(str(root / EXAMPLES_DIR), "输入记录 {} 没有对应的期望报告".format(record_id))


def collect_source_ids(registry: Any, problems: Problems) -> List[str]:
    """读取来源登记表中的来源标识。"""
    sources: List[str] = []
    if not isinstance(registry, dict):
        return sources
    for index, source in enumerate(registry.get("sources", [])):
        if not isinstance(source, dict):
            problems.add(REGISTRY, "sources[{}] 不是对象".format(index))
            continue
        source_id = source.get("id")
        if not isinstance(source_id, str) or not source_id:
            problems.add(REGISTRY, "sources[{}] 缺少 id".format(index))
            continue
        if source_id in sources:
            problems.add(REGISTRY, "来源 id {} 重复".format(source_id))
        sources.append(source_id)
    return sources


def validate(root: Path) -> Problems:
    """执行全部校验，返回收集到的问题。"""
    problems = Problems()

    schemas = build_schemas(root, problems)
    validators = build_validators(schemas)
    registry = load_json(root / REGISTRY, problems)
    sources = collect_source_ids(registry, problems)

    records = check_records(root, validators, problems)
    check_reports(root, records, validators, sources, problems)
    return problems


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="校验契约文件的结构与引用关系。")
    parser.add_argument(
        "--root",
        default=str(REPO_ROOT),
        help="工程根目录，默认为本脚本所在工程的根目录。",
    )
    args = parser.parse_args(argv)

    problems = validate(Path(args.root))
    if problems:
        print("契约校验未通过，共 {} 项问题：".format(len(problems.items)))
        for item in problems.items:
            print("  - {}".format(item.replace("\n", "\n    ")))
        return 1

    print("契约校验通过：输入记录与期望报告通过 Schema 校验，回显一致性与引用关系成立。")
    print("注意：本校验只覆盖格式与引用，不代表诊断结论正确。")
    if "date-time" not in FORMAT_CHECKER.checkers:
        print("注意：当前环境未安装日期时间格式校验器，Schema 中 format: date-time 未被执行，")
        print("      日期时间只按字符串约束校验，时区偏移未被机器检查。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
