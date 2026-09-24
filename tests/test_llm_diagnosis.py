"""诊断提示词的构造与模型回答的解析。

分两部分：

- 提示词：观测 id、工具确认的状态、缺失信息与知识条目都必须出现在提示词里，且规模
  受 ``PROMPT_CHAR_LIMIT`` 控制，裁剪时在提示词内写明省略了多少条；
- 解析：回答必须是约定的 JSON，引用的观测必须存在。不合规一律报错，不修补、不重试。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Sequence

import pytest

from airbornediag.knowledge.model import KnowledgeEntry
from airbornediag.llm import diagnosis as diag
from airbornediag.llm.diagnosis import (
    PROMPT_CHAR_LIMIT,
    AnalysisOutputError,
    DiagnosisPromptError,
    build_diagnosis_prompt,
    parse_analysis,
)
from airbornediag.mcu.model import (
    Basis,
    ConfirmedState,
    DiagnosisResult,
    Inconsistency,
    MissingItem,
    ToolResult,
    UnsupportedItem,
)

BASIS = Basis("MPC5554_RM", "22.3.3.6", ("MPC5554-FlexCAN2-ESR-FLTCONF",))


def result(
    *,
    states: Sequence[ConfirmedState] = (),
    conflicts: Sequence[Inconsistency] = (),
    missing: Sequence[MissingItem] = (),
    unsupported: Sequence[UnsupportedItem] = (),
    target: str = "CAN_A",
) -> DiagnosisResult:
    """拼一份最小工具结果，只填本测试关心的内容。"""
    return DiagnosisResult(
        record_id="REC-TEST-001",
        chip="MPC5554",
        tool_results=(
            ToolResult(
                tool="mpc5554.flexcan2",
                chip="MPC5554",
                peripheral="FlexCAN2",
                target=target,
                used_fields=("{}.ESR.FLTCONF".format(target),),
                confirmed_states=tuple(states),
                inconsistencies=tuple(conflicts),
                insufficient_data=tuple(missing),
                unsupported=tuple(unsupported),
            ),
        ),
    )


def state(state_id: str, statement: str, evidence: Sequence[str] = ("OBS-1",)) -> ConfirmedState:
    return ConfirmedState(id=state_id, statement=statement, evidence=tuple(evidence), basis=BASIS)


def entry(entry_id: str, body: str = "正文") -> KnowledgeEntry:
    return KnowledgeEntry(
        entry_id=entry_id,
        chip="MPC5554",
        peripheral="FlexCAN2",
        kind="chip_knowledge",
        title="标题 {}".format(entry_id),
        body=body,
        locator="22.3.3.6",
        source_id="MPC5554_RM",
    )


def record(observations: Sequence[Dict[str, Any]] = (), **overrides: Any) -> Dict[str, Any]:
    data: Dict[str, Any] = {
        "schema_version": "0.1.1",
        "record_id": "REC-TEST-001",
        "origin": "simulated",
        "device": {"model": "MPC5554"},
        "test": {"id": "TEST-001", "target": "CAN_A", "result": "fail"},
        "observations": list(observations),
    }
    data.update(overrides)
    return data


def observation(observation_id: str, **fields: Any) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "id": observation_id,
        "kind": "register",
        "target": "CAN_A",
        "register": "CAN_A.ESR",
        "fields": {"FLTCONF": "bus_off"},
    }
    payload.update(fields)
    return payload


# --- 提示词内容 -------------------------------------------------------------


def test_prompt_is_chatml_and_carries_the_system_requirements() -> None:
    prompt = build_diagnosis_prompt(record([observation("OBS-1")]), result(), [])
    assert prompt.startswith("<|im_start|>system\n")
    assert prompt.endswith("<|im_start|>assistant\n")
    assert diag.SYSTEM_MESSAGE in prompt


def test_prompt_lists_every_observation_id() -> None:
    """候选原因只能引用提示词里出现过的观测 id，模型看不到的 id 它引用不了。"""
    observations = [observation("OBS-{}".format(index)) for index in range(1, 6)]
    prompt = build_diagnosis_prompt(record(observations), result(), [])
    for index in range(1, 6):
        assert "- OBS-{} ".format(index) in prompt


def test_prompt_states_the_record_origin() -> None:
    """模拟案例与真实设备记录不能混淆，来源必须写明。"""
    prompt = build_diagnosis_prompt(record(), result(), [])
    assert "模拟案例（不是真实设备记录）" in prompt

    real = build_diagnosis_prompt(record(origin="manual_capture"), result(), [])
    assert "人工采集（真实设备记录）" in real
    assert "模拟案例" not in real


def test_prompt_carries_tool_states_with_their_basis() -> None:
    prompt = build_diagnosis_prompt(
        record(),
        result(states=[state("FC-BUSOFF-STATE", "采集时刻 CAN_A 处于总线关闭状态。")]),
        [],
    )
    assert "采集时刻 CAN_A 处于总线关闭状态。" in prompt
    assert "MPC5554_RM 22.3.3.6" in prompt
    # 结论由程序写入报告，提示词里说明不得改写。
    assert "不得改写" in prompt


def test_prompt_carries_missing_and_unsupported_sections() -> None:
    prompt = build_diagnosis_prompt(
        record(),
        result(
            missing=[MissingItem("总线电气测量", "无法定位物理层成因", BASIS)],
            unsupported=[UnsupportedItem("CAN_A.CR 的位域 BOFFMSK", "本版没有该位域的判据。")],
        ),
        [],
    )
    assert "总线电气测量" in prompt
    assert "无法定位物理层成因" in prompt
    assert "CAN_A.CR 的位域 BOFFMSK" in prompt


def test_prompt_lists_knowledge_entries_with_ids_and_origin() -> None:
    prompt = build_diagnosis_prompt(record(), result(), [entry("MPC5554-FlexCAN2-ESR-FLTCONF")])
    assert "MPC5554-FlexCAN2-ESR-FLTCONF" in prompt
    assert "MPC5554_RM 22.3.3.6" in prompt


def test_empty_sections_say_so_instead_of_being_omitted() -> None:
    """没有可说的内容时也要明说，不能让模型以为那一节被漏掉了。"""
    prompt = build_diagnosis_prompt(record(), result(), [])
    assert diag._NO_STATES in prompt
    assert diag._NO_MISSING in prompt
    assert diag._NO_KNOWLEDGE in prompt


def test_record_without_observations_says_so() -> None:
    prompt = build_diagnosis_prompt(record(), result(), [])
    assert "没有观测" in prompt


# --- 提示词长度控制 ---------------------------------------------------------


def test_prompt_stays_within_the_character_limit() -> None:
    knowledge = [entry("E-{}".format(index), "很长的正文" * 40) for index in range(50)]
    prompt = build_diagnosis_prompt(record([observation("OBS-1")]), result(), knowledge)
    assert len(prompt) <= PROMPT_CHAR_LIMIT


def test_trimming_drops_knowledge_first_and_says_how_many(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """裁剪顺序是知识条目 → 不支持项 → 缺失信息，且省略条数写在提示词里。"""
    knowledge = [entry("E-{}".format(index), "正文" * 30) for index in range(6)]
    tool_result = result(
        missing=[MissingItem("缺失项", "原因", BASIS)],
        unsupported=[UnsupportedItem("不支持项", "原因")],
    )
    base = len(build_diagnosis_prompt(record(), tool_result, []))
    # 上限取「只放得下前两条知识条目」的位置。
    monkeypatch.setattr(diag, "PROMPT_CHAR_LIMIT", base + 120)

    prompt = build_diagnosis_prompt(record(), tool_result, knowledge)
    assert len(prompt) <= base + 120
    assert "省略了" in prompt and "知识条目" in prompt
    # 知识条目被裁，不支持项与缺失信息仍在。
    assert "不支持项" in prompt
    assert "缺失项" in prompt


def test_prompt_beyond_the_limit_without_anything_to_trim_is_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """观测与工具结论本身超限时不截断观测，直接报错：截断会让模型引用不存在的证据。"""
    monkeypatch.setattr(diag, "PROMPT_CHAR_LIMIT", 100)
    with pytest.raises(DiagnosisPromptError) as error:
        build_diagnosis_prompt(record([observation("OBS-1")]), result(), [entry("E-1")])
    assert "提示词上限" in str(error.value)


# --- 回答解析 ---------------------------------------------------------------


def answer(**overrides: Any) -> str:
    payload: Dict[str, Any] = {
        "candidate_causes": [
            {"statement": "候选原因", "supporting": ["OBS-1"], "contradicting": []}
        ],
        "recommended_checks": ["检查项"],
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


def test_valid_answer_is_parsed() -> None:
    parsed = parse_analysis(answer(), ["OBS-1"])
    assert len(parsed.candidate_causes) == 1
    assert parsed.candidate_causes[0].statement == "候选原因"
    assert parsed.candidate_causes[0].supporting == ("OBS-1",)
    assert parsed.recommended_checks == ("检查项",)
    assert parsed.ignored_keys == ()


def test_answer_may_be_wrapped_in_a_code_fence() -> None:
    """代码围栏与前后说明不影响解析：只把 JSON 本身找出来，不改写内容。"""
    text = "好的，分析如下：\n```json\n{}\n```\n以上。".format(answer())
    assert parse_analysis(text, ["OBS-1"]).recommended_checks == ("检查项",)


def test_answer_without_json_is_rejected() -> None:
    with pytest.raises(AnalysisOutputError) as error:
        parse_analysis("这次故障可能是总线短路。", ["OBS-1"])
    assert "没有 JSON 对象" in str(error.value)


def test_answer_with_broken_json_is_rejected() -> None:
    with pytest.raises(AnalysisOutputError) as error:
        parse_analysis('{"candidate_causes": [},', ["OBS-1"])
    assert "不是合法的 JSON" in str(error.value)


def test_answer_missing_a_required_key_is_rejected() -> None:
    with pytest.raises(AnalysisOutputError) as error:
        parse_analysis(json.dumps({"candidate_causes": []}), ["OBS-1"])
    assert "缺少 recommended_checks" in str(error.value)


def test_empty_candidate_causes_are_allowed() -> None:
    """证据不足时允许不提出候选原因。"""
    parsed = parse_analysis(
        json.dumps({"candidate_causes": [], "recommended_checks": ["补充采集"]}),
        ["OBS-1"],
    )
    assert parsed.candidate_causes == ()
    assert parsed.recommended_checks == ("补充采集",)


def test_fabricated_observation_reference_is_rejected() -> None:
    text = json.dumps(
        {
            "candidate_causes": [
                {"statement": "候选原因", "supporting": ["OBS-99"], "contradicting": []}
            ],
            "recommended_checks": [],
        }
    )
    with pytest.raises(AnalysisOutputError) as error:
        parse_analysis(text, ["OBS-1"])
    assert "不存在的观测 OBS-99" in str(error.value)
    assert "OBS-1" in str(error.value)


def test_blank_statement_is_rejected() -> None:
    text = json.dumps(
        {
            "candidate_causes": [
                {"statement": "  ", "supporting": [], "contradicting": []}
            ],
            "recommended_checks": [],
        }
    )
    with pytest.raises(AnalysisOutputError) as error:
        parse_analysis(text, ["OBS-1"])
    assert "statement" in str(error.value)


def test_cause_missing_a_field_is_rejected() -> None:
    text = json.dumps(
        {"candidate_causes": [{"statement": "候选原因", "supporting": []}], "recommended_checks": []}
    )
    with pytest.raises(AnalysisOutputError) as error:
        parse_analysis(text, ["OBS-1"])
    assert "缺少字段 contradicting" in str(error.value)


def test_unknown_top_level_key_is_reported_not_rejected() -> None:
    """契约没有对应字段的内容不进入报告，但要让使用者知道模型答了什么。"""
    parsed = parse_analysis(answer(confidence=0.8), ["OBS-1"])
    assert parsed.ignored_keys == ("confidence",)


def test_error_preview_shows_what_the_model_returned() -> None:
    text = "不是 JSON" * 100
    with pytest.raises(AnalysisOutputError) as error:
        parse_analysis(text, [])
    assert error.value.preview.startswith("不是 JSON")
    assert "已截断" in error.value.preview
    assert len(error.value.preview) < len(text)


def test_empty_observation_ids_make_any_reference_a_fabrication() -> None:
    with pytest.raises(AnalysisOutputError) as error:
        parse_analysis(answer(), [])
    assert "本记录没有观测" in str(error.value)
