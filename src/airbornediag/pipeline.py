"""诊断流程编排：输入校验后的记录 → 工具分析 → 知识检索 → 模型分析 → 报告组装。

编排是固定的，不做模型自主选择工具，也不做跨外设的自动扩张：一条记录支持多少对象
由本版工具的分派决定（见 ``mcu/tools.py``）。

三步的顺序有先后依赖：

1. 工具先跑。芯片或全部检测对象都不在本版支持范围内时直接结束，不调用模型——没有
   任何工具结论时让模型凭空分析，只会得到没有依据的内容。
2. 知识检索以工具结论为起点：先取工具结果 ``basis.knowledge_refs`` 指向的条目（结论
   的依据必须进上下文），再用本次实际使用的位域名做关键词补充，去重后限制条数。检索
   只查询已构建的索引，不重新解析手册。
3. 最后调用模型。模型只负责解释、候选原因与检查建议；工具结论、观测回显与缺失信息
   由程序组装进报告，模型改写不了。

模型调用失败即本次失败：不降级为模拟回答，也不省略该环节继续出报告。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from airbornediag.knowledge.index import DEFAULT_INDEX_PATH, query_index
from airbornediag.knowledge.model import (
    DEFAULT_CURATED_DIR,
    DEFAULT_REGISTRY_PATH,
    KnowledgeEntry,
    load_curated,
    load_registry_sources,
)
from airbornediag.llm.client import MindIEClient
from airbornediag.llm.diagnosis import (
    PROMPT_CHAR_LIMIT,
    build_diagnosis_prompt,
    parse_analysis,
)
from airbornediag.mcu import run_tools
from airbornediag.mcu.model import Basis, DiagnosisResult, unique_in_order
from airbornediag.report import (
    REPORT_SCHEMA,
    RUNTIME_REPORT_WHERE,
    Problems,
    ReportContractError,
    build_report,
    check_report,
    derive_report_id,
    load_validators,
    schema_version_of,
)

# 知识条目的目标条数：工具结论引用的条目优先，其余按字段关键词补充到这个条数为止。
DEFAULT_MAX_KNOWLEDGE = 8

# 进度回调：编排过程只报告事实（做了什么、命中多少），不打印报告内容。
Progress = Callable[[str], None]


class PipelineInputError(Exception):
    """流程的输入或配置有问题，例如契约 Schema 读不到。"""


class UnsupportedRecordError(Exception):
    """记录合规，但芯片或全部检测对象不在本版支持范围内：不调用模型。"""

    def __init__(self, result: DiagnosisResult) -> None:
        self.result = result
        super().__init__("芯片或全部检测对象不在本版支持范围内，未运行任何工具")


@dataclass(frozen=True)
class PipelineOutcome:
    """一次完整诊断流程的产出。"""

    report: Dict[str, Any]
    # 本次送入模型的知识条目，按送入顺序。
    knowledge: Tuple[KnowledgeEntry, ...]
    # 工具结论引用、但知识库中找不到的条目 id。
    unresolved_refs: Tuple[str, ...]
    # 模型回答里本报告契约没有对应字段的顶层字段名。
    ignored_keys: Tuple[str, ...]
    prompt_chars: int
    model_seconds: float


def _noop(message: str) -> None:
    return None


def _observation_ids(record: Mapping[str, Any]) -> Tuple[str, ...]:
    """记录中全部观测的 id，供候选原因的引用校验使用。"""
    observations = record.get("observations")
    if not isinstance(observations, list):
        return ()
    ids: List[str] = []
    for item in observations:
        if isinstance(item, Mapping):
            observation_id = item.get("id")
            if isinstance(observation_id, str) and observation_id.strip():
                ids.append(observation_id.strip())
    return tuple(ids)


def _tool_bases(result: DiagnosisResult) -> List[Basis]:
    """工具结果中全部带出处的判据依据。"""
    bases: List[Basis] = []
    for item in result.tool_results:
        bases.extend(state.basis for state in item.confirmed_states)
        bases.extend(conflict.basis for conflict in item.inconsistencies)
        bases.extend(
            missing.basis for missing in item.insufficient_data if missing.basis is not None
        )
    return bases


def referenced_entry_ids(result: DiagnosisResult) -> Tuple[str, ...]:
    """工具结果引用的知识条目 id，按首次出现顺序去重。"""
    refs: List[str] = []
    for basis in _tool_bases(result):
        refs.extend(basis.knowledge_refs)
    return unique_in_order(refs)


def _query_words(result: DiagnosisResult) -> List[Tuple[str, str, str]]:
    """关键词补充用的查询词：本次实际使用的位域名。

    取 ``CAN_A.ESR.FLTCONF`` 的最后一段。模块实例名不作为查询词：``CAN_A`` 分词后
    会产生单字母词元，命中大量无关条目，而位域名在知识条目里是稳定的定位词。
    """
    words: List[Tuple[str, str, str]] = []
    seen: List[Tuple[str, str, str]] = []
    for item in result.tool_results:
        for used in item.used_fields:
            name = used.rsplit(".", 1)[-1]
            # 过短的词元（例如单字母）区分度不足，不作为查询词。
            if len(name) < 2:
                continue
            key = (item.chip, item.peripheral, name)
            if key not in seen:
                seen.append(key)
                words.append(key)
    return words


def collect_knowledge(
    result: DiagnosisResult,
    *,
    db_path: Path = DEFAULT_INDEX_PATH,
    curated_dir: Path = DEFAULT_CURATED_DIR,
    registry_path: Optional[Path] = DEFAULT_REGISTRY_PATH,
    max_items: int = DEFAULT_MAX_KNOWLEDGE,
    progress: Progress = _noop,
) -> Tuple[Tuple[KnowledgeEntry, ...], Tuple[str, ...]]:
    """按工具结果组织本次送入的知识条目。

    返回 (条目, 未找到的引用 id)。工具引用的条目全部保留：它们是确认状态与缺失信息
    的依据，缺了依据的结论没法解释。关键词补充只在总数未达 ``max_items`` 时进行，
    因此条目总数可能因引用过多而超过该上限，这种情况在进度信息里如实报告。
    """
    by_id = {entry.entry_id: entry for entry in load_curated(curated_dir, registry_path)}

    selected: List[KnowledgeEntry] = []
    chosen: List[str] = []
    unresolved: List[str] = []
    for ref in referenced_entry_ids(result):
        entry = by_id.get(ref)
        if entry is None:
            unresolved.append(ref)
            continue
        if entry.entry_id not in chosen:
            chosen.append(entry.entry_id)
            selected.append(entry)
    referenced_count = len(selected)

    for chip, peripheral, word in _query_words(result):
        if len(selected) >= max_items:
            break
        for entry in query_index(
            word, db_path=db_path, chip=chip, peripheral=peripheral, limit=1
        ):
            if entry.entry_id not in chosen:
                chosen.append(entry.entry_id)
                selected.append(entry)
            break

    progress(
        "知识检索：工具引用 {} 条，字段关键词补充 {} 条，共 {} 条（目标上限 {}）".format(
            referenced_count, len(selected) - referenced_count, len(selected), max_items
        )
    )
    if unresolved:
        progress(
            "知识检索：以下 id 被工具结论引用，但在知识库中找不到：{}。"
            "对应的结论仍以手册出处为据，但本次没有知识条目可送入模型。".format(
                "、".join(unresolved)
            )
        )
    return tuple(selected), tuple(unresolved)


def run_pipeline(
    record: Mapping[str, Any],
    *,
    client: MindIEClient,
    schema_dir: Path,
    db_path: Path = DEFAULT_INDEX_PATH,
    curated_dir: Path = DEFAULT_CURATED_DIR,
    registry_path: Optional[Path] = DEFAULT_REGISTRY_PATH,
    max_knowledge: int = DEFAULT_MAX_KNOWLEDGE,
    progress: Progress = _noop,
) -> PipelineOutcome:
    """对一条已通过输入契约校验的记录执行完整诊断流程。

    失败一律抛出异常，不返回部分结果：工具结果、知识检索或模型分析的缺失都不能用
    默认值补上。调用方按异常类型区分退出码。
    """
    result = run_tools(record)
    counts = result.counts()
    progress(
        "工具结果：{}".format("，".join("{} {}".format(key, value) for key, value in counts.items()))
    )
    if not result.supported:
        # 没有工具处理过这条记录：不调用模型，也不产出报告。
        raise UnsupportedRecordError(result)

    # 先读文件再调用模型：Schema、来源登记表或知识文件有问题时不必浪费一次模型调用。
    try:
        schemas, validators = load_validators(schema_dir)
    except ReportContractError as error:
        # Schema 文件本身不可用时属输入/配置问题，不是报告内容不合规。
        raise PipelineInputError("读取契约 Schema 失败：{}".format("；".join(error.problems)))
    report_version = schema_version_of(schemas)
    sources = tuple(load_registry_sources(Path(registry_path)).keys()) if registry_path else ()

    knowledge, unresolved = collect_knowledge(
        result,
        db_path=db_path,
        curated_dir=curated_dir,
        registry_path=registry_path,
        max_items=max_knowledge,
        progress=progress,
    )

    prompt = build_diagnosis_prompt(record, result, knowledge)
    progress("模型调用：提示词 {} 字符（提示词上限 {}）".format(len(prompt), PROMPT_CHAR_LIMIT))

    response = client.generate(prompt)
    progress("模型回答：{} 字符，耗时 {:.2f} s".format(len(response.text), response.elapsed_seconds))

    analysis = parse_analysis(response.text, _observation_ids(record))

    report = build_report(
        record,
        result,
        report_id=derive_report_id(str(record.get("record_id") or "")),
        schema_version=report_version,
        candidate_causes=[
            {
                "statement": cause.statement,
                "supporting": cause.supporting,
                "contradicting": cause.contradicting,
            }
            for cause in analysis.candidate_causes
        ],
        recommended_checks=analysis.recommended_checks,
    )

    checks = Problems()
    check_report(
        RUNTIME_REPORT_WHERE,
        report,
        record=record,
        sources=sources,
        validator=validators.get(REPORT_SCHEMA),
        problems=checks,
    )
    if checks:
        # 组装的报告自己就不合规时不能输出：报告是这份流程的交付物。
        raise ReportContractError(checks.items)

    return PipelineOutcome(
        report=report,
        knowledge=knowledge,
        unresolved_refs=unresolved,
        ignored_keys=analysis.ignored_keys,
        prompt_chars=len(prompt),
        model_seconds=response.elapsed_seconds,
    )
