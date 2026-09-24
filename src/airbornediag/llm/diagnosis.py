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
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from airbornediag.knowledge.model import KnowledgeEntry
from airbornediag.llm.prompt import build_chat_prompt
from airbornediag.mcu.model import DiagnosisResult, MissingItem, ToolResult, UnsupportedItem

SYSTEM_MESSAGE = (
    "你是机载系统故障诊断助手。你的任务是根据给定的观测、工具结论与知识条目，"
    "解释已确认的结论、提出待验证的候选原因，并给出后续检查建议。\n"
    "必须遵守：\n"
    "1. 只使用材料中给出的内容，不得引入材料以外的寄存器定义、位域含义、数值门限或结论。\n"
    "2. candidate_causes 的 supporting 与 contradicting 只能填写材料中列出的观测 id，"
    "不得编造 id，也不得引用其它结论。\n"
    "3. 工具确认的状态、输入矛盾与缺失信息由程序写入报告：不得改写、否认或替换，也不必抄写。\n"
    "4. 缺失信息表示未知，不等于正常，也不等于检测通过。\n"
    "5. 材料中没有足够证据时，candidate_causes 给空数组，不要为凑数提出原因。\n"
    "6. 不给出未经校准的概率、百分比或置信度，也不给出材料未支持的具体器件型号与数值门限。\n"
    "7. 只输出约定的 JSON 对象，不输出 JSON 以外的说明文字。"
)

# 整段请求文本（含 system 段）的字符数上限。这是提示词侧的规模控制，不是服务端限制。
PROMPT_CHAR_LIMIT = 8000
# 单条知识正文的字符数上限。
KNOWLEDGE_BODY_LIMIT = 240
# 观测逐条列出的条数上限，超出部分只列 id，便于模型引用。
OBSERVATION_LIMIT = 30
# 单行观测的字符数上限。
OBSERVATION_LINE_LIMIT = 240
# 超出上限后仍列出的观测 id 个数。
OBSERVATION_ID_LIMIT = 40
# 工具确认状态的条数上限。
STATE_LIMIT = 24

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


def _cut(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


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


def _observation_lines(record: Mapping[str, Any]) -> List[str]:
    """观测的逐条说明。

    观测 id 必须全部出现：候选原因只能引用这些 id，模型看不到的 id 它无法引用。
    条数超出上限时，剩余观测只列 id 而不再列内容。
    """
    observations = record.get("observations")
    if not isinstance(observations, list) or not observations:
        return ["（本次记录没有观测。缺观测表示未知，不等于正常。）"]

    lines: List[str] = []
    hidden_ids: List[str] = []
    for observation in observations:
        if not isinstance(observation, Mapping):
            continue
        observation_id = observation.get("id")
        if not isinstance(observation_id, str) or not observation_id.strip():
            continue
        if len(lines) >= OBSERVATION_LIMIT:
            hidden_ids.append(observation_id)
            continue
        kind = observation.get("kind")
        payload = {key: value for key, value in observation.items() if key not in ("id", "kind")}
        lines.append(
            _cut(
                "- {} [{}] {}".format(observation_id, kind, _compact(payload)),
                OBSERVATION_LINE_LIMIT,
            )
        )

    if hidden_ids:
        shown = hidden_ids[:OBSERVATION_ID_LIMIT]
        rest = len(hidden_ids) - len(shown)
        note = "（其余 {} 条观测未列出内容，其 id 为：{}{}）".format(
            len(hidden_ids),
            "、".join(shown),
            "、…（另有 {} 条未列出）".format(rest) if rest else "",
        )
        lines.append(note)
    return lines


def _state_lines(result: DiagnosisResult) -> List[str]:
    lines: List[str] = []
    total = 0
    for item in result.tool_results:
        for state in item.confirmed_states:
            total += 1
            if len(lines) >= STATE_LIMIT:
                continue
            lines.append(
                "- [{}] {}（证据：{}；依据：{}）".format(
                    item.target,
                    state.statement,
                    "、".join(state.evidence),
                    state.basis,
                )
            )
    if total > len(lines):
        lines.append("（因条数上限，另有 {} 条确认状态未列出。）".format(total - len(lines)))
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
    lines = []
    for entry in knowledge:
        lines.append(
            "- [{}] {}（{}）\n  {}".format(
                entry.entry_id,
                entry.title,
                entry.origin,
                _cut(entry.body, KNOWLEDGE_BODY_LIMIT),
            )
        )
    return lines or [_NO_KNOWLEDGE]


_OUTPUT_REQUIREMENTS = """只输出一个 JSON 对象，不要输出其他内容：
{"candidate_causes": [{"statement": "候选原因的说明", "supporting": ["OBS-1"], "contradicting": []}], "recommended_checks": ["检查项"]}
- candidate_causes：待验证的候选原因。没有足够证据时给空数组，不要凑数。
- supporting / contradicting：只填上面列出的观测 id；没有就填空数组。
- recommended_checks：可执行的后续检查项，包含没有案例证据支持的通用排查项。"""


def _user_message(
    record: Mapping[str, Any],
    result: DiagnosisResult,
    knowledge: Sequence[KnowledgeEntry],
    unsupported: Sequence[UnsupportedItem],
    missing: Sequence[Tuple[str, MissingItem]],
    dropped: Mapping[str, int],
) -> str:
    """按固定顺序拼出提示词的 user 段。

    裁剪发生在末尾几节：知识、不支持项、缺失信息。工具确认的状态与被引用的观测不
    参与裁剪——它们要么是模型必须遵守的结论，要么是它可以引用的对象。
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
            _dropped_note(dropped.get("missing", 0), "缺失信息"),
        ),
        (
            "本版没有判据的内容（不参与判断，不能被当作正常或未观测）",
            _unsupported_lines(unsupported),
            _dropped_note(dropped.get("unsupported", 0), "不支持项"),
        ),
        (
            "知识条目（解释与候选原因的依据，来自手册的核对结果）",
            _knowledge_lines(knowledge),
            _dropped_note(dropped.get("knowledge", 0), "知识条目"),
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

    超过 ``PROMPT_CHAR_LIMIT`` 时按知识条目、不支持项、缺失信息的顺序从末尾裁减，
    每次裁减都在提示词里写明省略了多少条。裁到无可再裁仍然超限时抛
    DiagnosisPromptError：此时不能靠截断观测内容硬凑，那样会让模型引用不存在的证据。
    """
    kept_knowledge = list(knowledge)
    unsupported = [entry for item in result.tool_results for entry in item.unsupported]
    unsupported.extend(result.unsupported)
    missing = [(item.target, entry) for item in result.tool_results for entry in item.insufficient_data]
    dropped = {"knowledge": 0, "unsupported": 0, "missing": 0}

    while True:
        message = _user_message(
            record, result, kept_knowledge, unsupported, missing, dropped
        )
        prompt = build_chat_prompt(message, SYSTEM_MESSAGE)
        if len(prompt) <= PROMPT_CHAR_LIMIT:
            return prompt
        if kept_knowledge:
            kept_knowledge.pop()
            dropped["knowledge"] += 1
        elif unsupported:
            unsupported.pop()
            dropped["unsupported"] += 1
        elif missing:
            missing.pop()
            dropped["missing"] += 1
        else:
            raise DiagnosisPromptError(
                "记录规模超出提示词上限（{} 字符）：仅观测与工具结论就有 {} 字符。"
                "请缩小记录范围后重试，本版不截断观测内容——截断会让候选原因引用"
                "记录中并不存在的证据。".format(PROMPT_CHAR_LIMIT, len(prompt))
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
