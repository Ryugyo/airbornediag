"""诊断报告的组装，以及报告契约的校验逻辑。

组装（``build_report``）只做三件事：按输入记录的字段值原样保留 ``device``、``test``、
``observations``；把工具结果里的确认状态、输入矛盾、缺失信息与不支持项搬进 ``analysis``；
把模型给出的候选原因与检查建议接上去。模型不参与这部分：它改写不了工具确认的状态，
也改写不了观测回显。

组装阶段的唯一 id：工具内部的规则 id 不含模块实例名（例如 ``FC-BUSOFF-STATE``），
一条记录含多个同型号实例时两个实例会得到相同的规则 id。报告中的 id 因此统一写成
``<检测对象>/<规则 id>``，既唯一又可追溯到规则来源；候选原因由程序按出现顺序编号。

校验部分（``check_report`` 及各 check_* 函数）供两处共用：运行期的报告自检，以及
``scripts/validate_contracts.py`` 对仓库内示例与期望报告的检查。Schema 是结构与取值
约束的唯一来源，这里的代码只补充 Schema 表达不了的跨文档关系。

校验通过不代表诊断结论正确。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from airbornediag.mcu.model import DiagnosisResult

SCHEMA_DIR = Path("schemas")
RECORD_SCHEMA = "fault-record.schema.json"
REPORT_SCHEMA = "diagnostic-report.schema.json"

# 报告 Schema 用相对 $ref（fault-record.schema.json#/$defs/...）引用输入 Schema。
# 顶层 Schema 没有 $id，解析基线为空 URI，相对引用按原样解析，因此以文件名作为
# 注册表的键即可让跨文件 $ref 完全在本地解析：内容由调用方从 schemas/ 读入，
# 注册表未命中时 jsonschema 直接抛错，不发起任何网络请求。
SCHEMA_RESOURCES = (RECORD_SCHEMA, REPORT_SCHEMA)

FORMAT_CHECKER = Draft202012Validator.FORMAT_CHECKER

# oneOf/anyOf 失败时，一条错误最多列出多少条分支内约束，避免输出过长。
# 取 20 是留足余量：四个观测分支全部不成立时通常也只有十来条。
BRANCH_DETAIL_LIMIT = 20

# 运行期自检的问题清单前缀。报告这时还没有文件名，用一个固定标签定位。
RUNTIME_REPORT_WHERE = "本次报告"


class ReportContractError(Exception):
    """组装好的报告未通过契约校验。"""

    def __init__(self, problems: Sequence[str]) -> None:
        self.problems = tuple(problems)
        super().__init__("报告未通过契约校验，共 {} 项问题".format(len(self.problems)))


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


def load_schemas(schema_dir: Path, problems: Problems) -> Dict[str, Any]:
    """读取 schemas/ 下的 Schema 文件。"""
    schemas: Dict[str, Any] = {}
    for name in SCHEMA_RESOURCES:
        contents = load_json(Path(schema_dir) / name, problems)
        if isinstance(contents, dict):
            schemas[name] = contents
    return schemas


def build_validators(schemas: Mapping[str, Any]) -> Dict[str, Draft202012Validator]:
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


def load_validators(
    schema_dir: Path,
) -> Tuple[Dict[str, Any], Dict[str, Draft202012Validator]]:
    """读取 schemas/ 并构造校验器，返回 (Schema 字典, 校验器字典)。

    Schema 文件本身有问题时抛 ReportContractError：缺少 Schema 就不能声称报告合规。
    """
    problems = Problems()
    schemas = load_schemas(schema_dir, problems)
    if problems:
        raise ReportContractError(problems.items)
    return schemas, build_validators(schemas)


def schema_version_of(schemas: Mapping[str, Any]) -> str:
    """报告 Schema 中声明的格式版本。

    组装报告时直接取这里的固定值，避免同一版本号在代码与 Schema 中各写一份。
    """
    report_schema = schemas.get(REPORT_SCHEMA)
    version = None
    if isinstance(report_schema, Mapping):
        properties = report_schema.get("properties")
        if isinstance(properties, Mapping):
            field = properties.get("schema_version")
            if isinstance(field, Mapping):
                version = field.get("const")
    if not isinstance(version, str) or not version:
        raise ReportContractError(
            [
                "{}: 未声明 schema_version 的固定取值，无法确定报告的格式版本".format(
                    REPORT_SCHEMA
                )
            ]
        )
    return version


# --- 组装 -------------------------------------------------------------------


def derive_report_id(record_id: str) -> str:
    """由记录标识推出报告标识。

    ``REC-2026-0918-002`` 这样的记录标识把前缀换成 ``RPT-``，其余标识加 ``RPT-`` 前缀。
    同一记录重复生成报告时标识相同，因此报告按记录覆盖而不是累积。
    """
    text = record_id.strip()
    if text.upper().startswith("REC-"):
        return "RPT-" + text[len("REC-") :]
    return "RPT-" + text


def unique_state_id(target: str, rule_id: str) -> str:
    """报告中的状态 id：``<检测对象>/<规则 id>``。

    规则 id 不含模块实例名，同一记录含多个同型号实例时会重名；加上检测对象前缀后
    每条状态都能唯一识别并区分所属实例，规则来源也仍然可读。
    """
    return "{}/{}".format(target, rule_id)


def _basis_jsonable(basis: Any) -> Dict[str, str]:
    """报告的 basis 只有文献出处；知识条目 id 留在工具结果里，不进入报告。"""
    return {"source": basis.source, "locator": basis.locator}


def build_report(
    record: Mapping[str, Any],
    result: DiagnosisResult,
    *,
    report_id: str,
    schema_version: str,
    candidate_causes: Sequence[Mapping[str, Any]] = (),
    recommended_checks: Sequence[str] = (),
    report_status: str = "actual",
) -> Dict[str, Any]:
    """把输入记录、工具结果与模型分析组装成一份报告。

    ``candidate_causes`` 只提供语句与观测引用，id 由这里按顺序编号，避免模型自编的
    id 在报告里重复或缺失。``fault_state`` 与 ``root_cause`` 同样由这里判定。
    """
    confirmed: List[Dict[str, Any]] = []
    conflicts: List[Dict[str, Any]] = []
    missing: List[Dict[str, Any]] = []
    unsupported: List[Dict[str, Any]] = []

    for item in result.tool_results:
        for state in item.confirmed_states:
            confirmed.append(
                {
                    "id": unique_state_id(item.target, state.id),
                    "statement": state.statement,
                    "evidence": list(state.evidence),
                    "basis": _basis_jsonable(state.basis),
                }
            )
        for conflict in item.inconsistencies:
            conflicts.append(
                {
                    "id": unique_state_id(item.target, conflict.id),
                    "statement": conflict.statement,
                    "evidence": list(conflict.evidence),
                }
            )
        for entry in item.insufficient_data:
            missing.append({"missing": entry.missing, "reason": entry.reason})
        for entry in item.unsupported:
            unsupported.append({"subject": entry.subject, "reason": entry.reason})

    for entry in result.unsupported:
        unsupported.append({"subject": entry.subject, "reason": entry.reason})

    causes: List[Dict[str, Any]] = []
    for index, cause in enumerate(candidate_causes, start=1):
        causes.append(
            {
                "id": "CC-{}".format(index),
                "statement": cause["statement"],
                "supporting": list(cause.get("supporting", ())),
                "contradicting": list(cause.get("contradicting", ())),
            }
        )

    analysis: Dict[str, Any] = {
        # 只有手册判据确认的故障状态才算 confirmed，与「有没有确认状态」无关。
        "fault_state": "confirmed" if result.fault_confirmed else "not_determined",
        "root_cause": "candidates_only" if causes else "not_determined",
        "confirmed_states": confirmed,
        "candidate_causes": causes,
        "inconsistencies": conflicts,
        "insufficient_data": missing,
        "recommended_checks": list(recommended_checks),
        "unsupported": unsupported,
    }

    return {
        "schema_version": schema_version,
        "report_id": report_id,
        "record_ref": record.get("record_id"),
        "provenance": {
            "input_origin": record.get("origin"),
            "report_status": report_status,
        },
        # 三项回显：按输入记录的字段值原样保留，模型不得改写。
        "device": record.get("device"),
        "test": record.get("test"),
        "observations": record.get("observations", []),
        "analysis": analysis,
    }


# --- 校验 -------------------------------------------------------------------


def check_observation_ids(where: str, observations: Any, problems: Problems) -> List[str]:
    """检查观测 id 在一条记录内唯一，返回观测 id 列表。

    唯一性按字段取值判定，JSON Schema 无法表达，因此保留在这里。
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


def check_report(
    where: str,
    report: Any,
    *,
    record: Optional[Mapping[str, Any]],
    sources: Sequence[str],
    validator: Optional[Draft202012Validator] = None,
    problems: Problems,
) -> None:
    """检查一份报告：Schema、回显一致性与引用关系。

    ``record`` 为 None 表示没有找到对应的输入记录（仓库遍历时 report_ref 指向的记录
    不存在），此时只做 Schema 与引用检查。
    """
    check_schema(where, validator, report, problems)
    if not isinstance(report, dict):
        return

    if record is None:
        problems.add(where, "record_ref {} 未指向存在的输入记录".format(report.get("record_ref")))
        return

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
