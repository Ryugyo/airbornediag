"""诊断提示词的构造与模型回答的解析。

分工：观测、工具确认的状态、输入矛盾、缺失信息与不支持项由程序整理后交给模型，
模型只负责解释、提出待验证的候选原因与检查建议。程序不把工具结论交给模型改写，
也不从模型回答里取这些结论。

**模型回答必须是约定的 JSON**。不符合约定时抛 AnalysisOutputError，不猜测、不修补、
不重试：一次不合规的回答要么是提示词没写清，要么是模型答不了这个问题，两种情况都
需要人看见原始回答，而不是被程序悄悄改写成一份看起来正常的报告。

长度控制：提示词按字符数计（``PROMPT_CHAR_LIMIT``），不引入 token 预算。字符数不
等于 token 数，这个上限用于避免把整条记录原样倒给模型，不代表服务端的输入上限；
服务端的输入长度限制需按实际部署核对（见 docs/development.md）。

超限时只整条移除**未被工具结论引用**的补充知识条目：观测、工具确认的状态、输入矛盾、
缺失信息、不支持项与工具引用的知识都是模型必须看到的内容，既不截断也不省略条数。
裁到无可再裁仍然超限时报错——按字符截断会让模型引用记录中并不存在的证据。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from airbornediag.knowledge.model import KnowledgeEntry
from airbornediag.llm.prompt import build_chat_prompt
from airbornediag.mcu.model import (
    Basis,
    DiagnosisResult,
    MissingItem,
    ToolResult,
    UnsupportedItem,
    unique_in_order,
)

SYSTEM_MESSAGE = (
    "你是机载系统故障诊断助手。你的任务是根据给定的观测、工具结论与知识条目，"
    "解释已确认的结论、提出待验证的候选原因，并给出后续检查建议。\n"
    "必须遵守：\n"
    "1. 只使用材料中给出的内容，不得引入材料以外的寄存器定义、位域含义、数值门限或结论。\n"
    "2. candidate_causes 的 supporting 与 contradicting 只能填写材料中列出的观测 id，"
    "不得编造 id，也不得引用其它结论。\n"
    "3. 工具确认的状态、输入矛盾与缺失信息由程序写入报告：不得改写、否认或替换，也不必抄写。\n"
    "4. 缺失信息表示未知，不等于正常，也不等于检测通过。\n"
    "5. candidate_causes 只在所给知识条目与本记录的观测共同支持某个原因时才提出；"
    "没有这样的原因就给空数组。\n"
    "6. 不给出未经校准的概率、百分比或置信度，也不给出材料未支持的具体器件型号与数值门限。\n"
    "7. 只输出约定的 JSON 对象，不输出 JSON 以外的说明文字。"
)

# 整段请求文本（含 system 段）的字符数上限。这是提示词侧的规模控制，不是服务端限制。
# 超限时只整条移除未被工具结论引用的补充知识条目，其余内容不截断、不省略。
PROMPT_CHAR_LIMIT = 8000

# 模型回答的顶层字段。其余字段不进入报告，只作为提示信息返回。
_CAUSES_KEY = "candidate_causes"
_CHECKS_KEY = "recommended_checks"
_CAUSE_FIELDS = ("statement", "supporting", "contradicting")

# 报错时附带的回答预览长度，便于排查而不至于把整段回答倒进日志。
PREVIEW_LIMIT = 300

_NO_STATES = "（本次没有工具确认的状态。）"
_NO_CONFLICTS = "（本次没有输入矛盾。）"
_NO_MISSING = "（本次没有缺失信息说明。）"
_NO_UNSUPPORTED = "（本次没有本版不支持的内容。）"
_NO_KNOWLEDGE = "（本次没有检索到相关知识条目。）"


class DiagnosisPromptError(Exception):
    """提示词无法按当前记录生成，例如记录规模超出提示词上限。"""


class AnalysisOutputError(Exception):
    """模型回答不符合约定，无法作为分析结果使用。"""

    def __init__(self, message: str, answer: str = "") -> None:
        self.answer = answer
        super().__init__(message)

    @property
    def preview(self) -> str:
        """回答的开头部分，供使用者核对模型实际返回了什么。"""
        text = self.answer.strip()
        if len(text) <= PREVIEW_LIMIT:
            return text
        return text[:PREVIEW_LIMIT] + "...（已截断）"


@dataclass(frozen=True)
class CandidateCause:
    """模型提出的待验证候选原因。id 由报告组装阶段编号，模型不提供。"""

    statement: str
    supporting: Tuple[str, ...]
    contradicting: Tuple[str, ...]


@dataclass(frozen=True)
class DiagnosisAnalysis:
    """解析并通过校验的模型回答。"""

    candidate_causes: Tuple[CandidateCause, ...]
    recommended_checks: Tuple[str, ...]
    # 回答里出现但报告契约没有对应字段的顶层字段名，由调用方提示给使用者。
    ignored_keys: Tuple[str, ...] = ()


# --- 提示词 -----------------------------------------------------------------


def _compact(value: Any) -> str:
    """把取值紧凑地写成一行，读起来比缩进的 JSON 省长度。"""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _context_lines(record: Mapping[str, Any], result: DiagnosisResult) -> List[str]:
    """设备与检测上下文。记录来源必须写明，模拟案例与真实设备记录不能混淆。"""
    origin = record.get("origin")
    origin_text = {
        "simulated": "模拟案例（不是真实设备记录）",
        "manual_capture": "人工采集（真实设备记录）",
        "device_log": "设备记录（真实设备记录）",
    }.get(origin, "未知（记录未说明来源）")

    test = record.get("test") if isinstance(record.get("test"), Mapping) else {}
    test_bits = []
    for label, key in (("检测", "id"), ("名称", "name"), ("对象", "target"), ("结论", "result")):
        value = test.get(key)
        if isinstance(value, str) and value.strip():
            test_bits.append("{}={}".format(label, value.strip()))

    lines = [
        "芯片：{}".format(record.get("device", {}).get("model") if isinstance(record.get("device"), Mapping) else "（缺省）"),
        "记录：{}；来源：{!r}（{}）".format(record.get("record_id") or "（缺省）", origin, origin_text),
    ]
    lines.append("检测：" + ("；".join(test_bits) if test_bits else "（缺省）"))
    for item in result.tool_results:
        lines.append("参与分析的对象：{}（外设 {}，工具 {}）".format(item.target, item.peripheral, item.tool))
    return lines


_NO_OBSERVATIONS = "（本次记录没有观测。缺观测表示未知，不等于正常。）"


def _observation_lines(record: Mapping[str, Any]) -> List[str]:
    """观测的逐条说明。

    观测 id 必须全部出现：候选原因只能引用这些 id，模型看不到的 id 它无法引用。
    内容既不截断也不省略条数——观测是模型唯一可以引用的对象，少给一条就可能让它
    引用记录中并不存在的证据。整段提示词放不下时由 ``build_diagnosis_prompt`` 报错。
    """
    observations = record.get("observations")
    if not isinstance(observations, list) or not observations:
        return [_NO_OBSERVATIONS]

    lines: List[str] = []
    for observation in observations:
        if not isinstance(observation, Mapping):
            continue
        observation_id = observation.get("id")
        if not isinstance(observation_id, str) or not observation_id.strip():
            continue
        kind = observation.get("kind")
        payload = {key: value for key, value in observation.items() if key not in ("id", "kind")}
        lines.append("- {} [{}] {}".format(observation_id, kind, _compact(payload)))
    return lines or [_NO_OBSERVATIONS]


def _state_lines(result: DiagnosisResult) -> List[str]:
    """工具确认的状态，全部列出。

    这些结论由程序写入报告、模型不得改写，因此条数也不省略：省略会让模型看不到
    已经确认的结论，进而把它们当成待验证的候选原因重新提出。
    """
    lines = [
        "- [{}] {}（证据：{}；依据：{}）".format(
            item.target,
            state.statement,
            "、".join(state.evidence),
            state.basis,
        )
        for item in result.tool_results
        for state in item.confirmed_states
    ]
    return lines or [_NO_STATES]


def _conflict_lines(result: DiagnosisResult) -> List[str]:
    lines = [
        "- [{}] {}（证据：{}）".format(item.target, conflict.statement, "、".join(conflict.evidence))
        for item in result.tool_results
        for conflict in item.inconsistencies
    ]
    return lines or [_NO_CONFLICTS]


def _missing_lines(missing: Sequence[Tuple[str, MissingItem]]) -> List[str]:
    lines = []
    for target, entry in missing:
        basis = ""
        if entry.basis is not None:
            basis = "（判定依据：{}）".format(entry.basis)
        lines.append("- [{}] 缺少 {}：{}{}".format(target, entry.missing, entry.reason, basis))
    return lines or [_NO_MISSING]


def _unsupported_lines(unsupported: Sequence[UnsupportedItem]) -> List[str]:
    lines = ["- {}".format(entry.subject) for entry in unsupported]
    return lines or [_NO_UNSUPPORTED]


def _knowledge_lines(knowledge: Sequence[KnowledgeEntry]) -> List[str]:
    """知识条目按原文完整送出。

    正文在字符数上截断会切断句子甚至词（例如停在「长度 = PROP」），残缺的句子本身
    就是诱导模型补全的钩子；被截掉的又往往是适用条件一类的限制性说明。
    """
    lines = []
    for entry in knowledge:
        lines.append(
            "- [{}] {}（{}）\n  {}".format(
                entry.entry_id,
                entry.title,
                entry.origin,
                entry.body,
            )
        )
    return lines or [_NO_KNOWLEDGE]


def _referenced_entry_ids(result: DiagnosisResult) -> Tuple[str, ...]:
    """工具结论引用到的知识条目 id，按首次出现顺序去重。

    遍历口径与 ``pipeline.referenced_entry_ids`` 一致：确认状态、输入矛盾与缺失信息
    三处的 ``basis.knowledge_refs``。就地实现是为了不与 ``pipeline`` 形成循环依赖
    （``pipeline`` 已经引用本模块）。
    """
    bases: List[Basis] = []
    for item in result.tool_results:
        bases.extend(state.basis for state in item.confirmed_states)
        bases.extend(conflict.basis for conflict in item.inconsistencies)
        bases.extend(entry.basis for entry in item.insufficient_data if entry.basis is not None)
    refs: List[str] = []
    for basis in bases:
        refs.extend(basis.knowledge_refs)
    return unique_in_order(refs)


# 只示例顶层结构，两个字段都给空数组：给出带具体观测 id 的样例会被照抄，
# 在记录没有观测时尤其明显（实测模型会把样例里的 id 原样写进回答）。
_OUTPUT_REQUIREMENTS = """只输出一个 JSON 对象，不要输出其他内容：
{"candidate_causes": [], "recommended_checks": []}
- candidate_causes：待验证的候选原因，没有这样的原因就给空数组。有原因时每一项是
  这样的对象：{"statement": "候选原因的说明", "supporting": ["观测 id"], "contradicting": []}
- supporting：必填且至少一条。只填上面观测一节里实际列出的 id，每个 id 写成带双引号的
  字符串。该节若写着「本次记录没有观测」，就没有可引用的 id，此时给不出候选原因，
  candidate_causes 必须是空数组。
- contradicting：只填上面列出的观测 id；没有就填空数组。
- recommended_checks：字符串数组，每一项是字符串本身，不是对象。检查项应依据上面给出的
  知识条目，优先针对「缺失信息」一节的内容，不要重复「工具确认的状态」一节已经确认的结论。"""


def _user_message(
    record: Mapping[str, Any],
    result: DiagnosisResult,
    knowledge: Sequence[KnowledgeEntry],
    unsupported: Sequence[UnsupportedItem],
    missing: Sequence[Tuple[str, MissingItem]],
    dropped_knowledge: int,
) -> str:
    """按固定顺序拼出提示词的 user 段。

    只有知识条目会被整条移除，且只移除未被工具结论引用的补充条目。观测、工具确认的
    状态、输入矛盾、缺失信息与不支持项一律完整给出——它们要么是模型必须遵守的结论，
    要么是它可以引用的对象。
    """
    sections: List[Tuple[str, List[str], str]] = [
        ("任务", ["根据下面的材料，给出这条故障记录的候选原因与后续检查建议。"], ""),
        ("设备与检测", _context_lines(record, result), ""),
        (
            "观测（候选原因只能引用这里的观测 id）",
            _observation_lines(record),
            "",
        ),
        (
            "工具确认的状态（由规则确认，不得改写，也不必抄写）",
            _state_lines(result),
            "",
        ),
        ("输入矛盾", _conflict_lines(result), ""),
        (
            "缺失信息（缺失表示未知，不等于正常，也不等于检测通过）",
            _missing_lines(missing),
            "",
        ),
        (
            "本版没有判据的内容（不参与判断，不能被当作正常或未观测）",
            _unsupported_lines(unsupported),
            "",
        ),
        (
            "知识条目（解释与候选原因的依据，来自手册的核对结果）",
            _knowledge_lines(knowledge),
            _dropped_note(dropped_knowledge, "知识条目"),
        ),
        ("输出要求", _OUTPUT_REQUIREMENTS.splitlines(), ""),
    ]

    blocks: List[str] = []
    for title, lines, note in sections:
        body = "\n".join(lines)
        if note:
            body += "\n" + note
        blocks.append("# {}\n{}".format(title, body))
    return "\n\n".join(blocks)


def _dropped_note(count: int, label: str) -> str:
    if not count:
        return ""
    return "（因提示词长度限制，省略了 {} 条{}。）".format(count, label)


def build_diagnosis_prompt(
    record: Mapping[str, Any],
    result: DiagnosisResult,
    knowledge: Sequence[KnowledgeEntry],
) -> str:
    """构造完整的 ChatML 诊断提示词。

    超过 ``PROMPT_CHAR_LIMIT`` 时只整条移除**未被工具结论引用**的补充知识条目，
    按送入顺序从末尾开始，每次移除都在提示词里写明省略了多少条。工具引用的知识是
    结论的依据，观测、状态、矛盾、缺失信息与不支持项则是模型要遵守或引用的内容，
    都不参与裁剪。裁到只剩被引用的知识仍然超限时抛 DiagnosisPromptError：必要内容
    本身超限就是报错，不能靠截断它们硬凑——截断会让候选原因引用不存在的证据。
    """
    unsupported = [entry for item in result.tool_results for entry in item.unsupported]
    unsupported.extend(result.unsupported)
    missing = [(item.target, entry) for item in result.tool_results for entry in item.insufficient_data]

    # 被工具结论引用的条目必须保留，其余是按位域名补充检索来的，可以整条移除。
    referenced = set(_referenced_entry_ids(result))
    droppable = [
        index for index, item in enumerate(knowledge) if item.entry_id not in referenced
    ]
    dropped_indices: List[int] = []

    while True:
        hidden = set(dropped_indices)
        kept_knowledge = [item for index, item in enumerate(knowledge) if index not in hidden]
        message = _user_message(
            record, result, kept_knowledge, unsupported, missing, len(dropped_indices)
        )
        prompt = build_chat_prompt(message, SYSTEM_MESSAGE)
        if len(prompt) <= PROMPT_CHAR_LIMIT:
            return prompt
        if droppable:
            # 从末尾开始移除，保留被引用条目之间原有的相对顺序。
            dropped_indices.append(droppable.pop())
        else:
            raise DiagnosisPromptError(
                "记录规模超出提示词上限（{} 字符）：移除全部未被工具引用的知识条目后"
                "仍有 {} 字符。本版的必要内容（观测、工具结论、矛盾、缺失信息与工具"
                "引用的知识）不截断也不省略——截断会让候选原因引用记录中并不存在的"
                "证据。请缩小记录范围后重试。".format(PROMPT_CHAR_LIMIT, len(prompt))
            )


# --- 回答解析 ---------------------------------------------------------------


def _extract_object(answer: str) -> Any:
    """取出回答中的 JSON 对象。

    允许回答带 Markdown 代码围栏或前后说明：取第一个 ``{`` 到最后一个 ``}`` 之间的
    文本再解析。这不是修补回答内容，只是把 JSON 本身找出来；解析失败即报错。
    """
    start = answer.find("{")
    end = answer.rfind("}")
    if start < 0 or end < start:
        raise AnalysisOutputError("模型回答中没有 JSON 对象。", answer)
    try:
        return json.loads(answer[start : end + 1])
    except json.JSONDecodeError as error:
        raise AnalysisOutputError("模型回答不是合法的 JSON：{}".format(error), answer)


def _require_text(value: Any, where: str, answer: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AnalysisOutputError("模型回答的 {} 缺失或不是非空字符串。".format(where), answer)
    return value.strip()


def _require_text_list(value: Any, where: str, answer: str) -> Tuple[str, ...]:
    if not isinstance(value, list):
        raise AnalysisOutputError("模型回答的 {} 不是数组。".format(where), answer)
    items: List[str] = []
    for index, item in enumerate(value):
        items.append(_require_text(item, "{}[{}]".format(where, index), answer))
    return tuple(items)


def _check_references(references: Sequence[str], observation_ids: Sequence[str], where: str, answer: str) -> None:
    """候选原因引用的观测必须存在于本记录中：引用不存在的证据即报告无效。"""
    known = set(observation_ids)
    unknown = [item for item in references if item not in known]
    if not unknown:
        return
    available = "、".join(observation_ids[:20]) or "（本记录没有观测）"
    if len(observation_ids) > 20:
        available += "、…"
    raise AnalysisOutputError(
        "模型回答的 {} 引用了不存在的观测 {}；本记录可引用的观测为：{}。"
        "报告不接受编造的证据引用。".format(where, "、".join(unknown), available),
        answer,
    )


def parse_analysis(answer: str, observation_ids: Sequence[str]) -> DiagnosisAnalysis:
    """解析并校验模型回答。

    ``observation_ids`` 是本记录全部观测的 id，候选原因的引用必须落在其中。
    ``candidate_causes`` 本身允许为空数组；但每一项的 ``supporting`` 必须至少引用
    一条观测——没有观测支持的排查项属于 ``recommended_checks``。这条校验只保证
    存在支持引用，不声称候选原因本身经过验证。
    """
    payload = _extract_object(answer)
    if not isinstance(payload, Mapping):
        raise AnalysisOutputError(
            "模型回答的顶层不是 JSON 对象，实际为 {}。".format(type(payload).__name__), answer
        )

    ignored = tuple(sorted(str(key) for key in payload if key not in (_CAUSES_KEY, _CHECKS_KEY)))

    if _CAUSES_KEY not in payload:
        raise AnalysisOutputError("模型回答缺少 {} 字段。".format(_CAUSES_KEY), answer)
    if _CHECKS_KEY not in payload:
        raise AnalysisOutputError("模型回答缺少 {} 字段。".format(_CHECKS_KEY), answer)

    raw_causes = payload[_CAUSES_KEY]
    if not isinstance(raw_causes, list):
        raise AnalysisOutputError("模型回答的 {} 不是数组。".format(_CAUSES_KEY), answer)

    causes: List[CandidateCause] = []
    for index, raw in enumerate(raw_causes):
        where = "{}[{}]".format(_CAUSES_KEY, index)
        if not isinstance(raw, Mapping):
            raise AnalysisOutputError("模型回答的 {} 不是对象。".format(where), answer)
        missing_fields = [name for name in _CAUSE_FIELDS if name not in raw]
        if missing_fields:
            raise AnalysisOutputError(
                "模型回答的 {} 缺少字段 {}。".format(where, "、".join(missing_fields)), answer
            )
        statement = _require_text(raw["statement"], "{}.statement".format(where), answer)
        supporting = _require_text_list(raw["supporting"], "{}.supporting".format(where), answer)
        if not supporting:
            # 契约要求候选原因只引用观测；没有任何观测支持的排查项属于通用建议。
            # 这一条只保证存在支持引用，不表示该原因已被验证。
            raise AnalysisOutputError(
                "模型回答的 {}.supporting 是空数组：候选原因必须引用至少一条观测。"
                "没有观测支持的排查项应放在 {} 里。".format(where, _CHECKS_KEY),
                answer,
            )
        contradicting = _require_text_list(
            raw["contradicting"], "{}.contradicting".format(where), answer
        )
        _check_references(supporting, observation_ids, "{}.supporting".format(where), answer)
        _check_references(contradicting, observation_ids, "{}.contradicting".format(where), answer)
        causes.append(
            CandidateCause(
                statement=statement, supporting=supporting, contradicting=contradicting
            )
        )

    checks = _require_text_list(payload[_CHECKS_KEY], _CHECKS_KEY, answer)
    return DiagnosisAnalysis(
        candidate_causes=tuple(causes), recommended_checks=checks, ignored_keys=ignored
    )
