"""校验故障记录与诊断报告的格式及引用关系。

字段、类型、枚举与分支约束由 schemas/ 下的 JSON Schema 声明，本脚本用
jsonschema 实际执行这些 Schema，不自行实现 Schema 解释器，也不重复维护
结构规则。脚本自身只补充 Schema 表达不了的跨文档关系：

- 观测 id 在一条记录内唯一、record_id 在 examples/ 内唯一；
- 报告引用的观测 id 必须存在于本报告的 observations 中；
- 结论引用的文献来源必须登记在 knowledge/source-registry.json 中；
- 报告的 device、test、observations 必须与输入记录一致（模型不得改写）；
- 每个输入记录都有对应的期望报告。

结构与引用检查与运行期报告自检是同一份实现（``airbornediag.report``）：
运行期调用 ``check_report`` 校验刚组装出的报告，本脚本用它校验仓库内的示例与
期望报告。两者的要求因此不会各自漂移。脚本只额外负责遍历仓库文件、检查
record_id 唯一性与「每个记录都有期望报告」。

本脚本不实现任何芯片诊断规则，也不判断诊断结论是否正确。
校验通过不代表诊断正确。

需要已安装本工程包（例如 ``pip install -e .``），因为要导入 ``airbornediag.report``。

用法：
    python scripts/validate_contracts.py [--root 工程根目录]

全部检查通过时退出码为 0，存在问题时打印问题清单并返回 1。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from jsonschema import Draft202012Validator

from airbornediag.report import (
    FORMAT_CHECKER,
    RECORD_SCHEMA,
    REPORT_SCHEMA,
    Problems,
    build_validators,
    check_observation_ids,
    check_report,
    check_schema,
    json_files,
    load_json,
    load_schemas,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

SCHEMA_DIR = "schemas"
REGISTRY = "knowledge/source-registry.json"
EXAMPLES_DIR = "examples"
FIXTURES_DIR = "tests/fixtures"


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
        record_ref = report.get("record_ref") if isinstance(report, dict) else None
        record = records.get(record_ref) if isinstance(record_ref, str) else None

        check_report(
            where,
            report,
            record=record,
            sources=sources,
            validator=validators.get(REPORT_SCHEMA),
            problems=problems,
        )
        if record is not None:
            referenced.append(record_ref)

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

    schemas = load_schemas(root / SCHEMA_DIR, problems)
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
