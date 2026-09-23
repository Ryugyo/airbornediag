"""诊断工具分派与结果模型的测试。

分派测试关心的是「不属于本版支持范围的内容有没有被明确报告出来」：不支持的芯片、
检测对象、只能拿到原始值的观测、不合契约的寄存器名、没有判据的观测种类。
每个用例都同时断言「不支持不被当成正常」。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterator, List, Tuple

import pytest

from airbornediag.knowledge import load_curated
from airbornediag.mcu import Basis, run_tools, to_jsonable
from airbornediag.mcu.mpc5554 import dspi, flexcan2
from mcu_helpers import other_observation, record, register, unsupported_texts

REPO_ROOT = Path(__file__).resolve().parents[1]
CURATED_DIR = REPO_ROOT / "knowledge" / "curated"
REGISTRY = REPO_ROOT / "knowledge" / "source-registry.json"


def test_unsupported_chip_yields_no_conclusion() -> None:
    """没有该芯片的判据时不给任何结论，也不借用其他芯片的定义。"""
    result = run_tools(record(chip="TMS320F28335", target="eCAN_A"))

    assert result.supported is False
    assert result.tool_results == ()
    assert len(result.unsupported) == 1
    item = result.unsupported[0]
    assert "TMS320F28335" in item.subject
    assert "不能直接混用" in item.reason


def test_missing_chip_model_is_reported_as_missing_not_guessed() -> None:
    """芯片型号缺省时不猜，按缺省报告。"""
    data = record()
    del data["device"]

    result = run_tools(data)

    assert result.supported is False
    assert "缺省" in result.unsupported[0].subject


def test_unsupported_target_is_reported_and_not_analysed() -> None:
    """本版没有判据的检测对象被明确报告，且不产生任何工具结果。"""
    data = record(target="eMIOS_0", observations=[])

    result = run_tools(data)

    assert result.supported is False
    assert result.tool_results == ()
    assert "eMIOS_0" in result.unsupported[0].subject
    assert "不等于该对象正常" in result.unsupported[0].reason


def test_raw_value_only_observations_are_reported_as_unsupported() -> None:
    """只有原始值的观测不参与判断，并说明原因是不解析未核对的位编号。"""
    result = run_tools(
        record(observations=[register("OBS-1", "CAN_A.ESR", value=0x1234)])
    )

    text = unsupported_texts(result)
    assert "OBS-1" in text
    assert "位编号尚未逐条核对手册" in text
    # 观测未被使用，因此该对象只报缺失，不给结论。
    assert result.tool_results[0].confirmed_states == ()


def test_register_name_without_module_prefix_is_not_attributed() -> None:
    """register 名不合契约时不猜测归属。"""
    result = run_tools(record(observations=[register("OBS-1", "ESR", {"FLTCONF": "bus_off"})]))

    assert "寄存器名不合契约的观测（OBS-1）" in unsupported_texts(result)
    assert result.tool_results[0].confirmed_states == ()


def test_observation_kinds_without_rules_are_reported_as_unused() -> None:
    """没有判据的观测种类被列出，并说明未使用不等于正常。"""
    result = run_tools(
        record(
            observations=[
                other_observation("OBS-1", "can_frame", direction="tx", identifier="0x123"),
                other_observation("OBS-2", "timeout", waiting_for="ACK"),
            ]
        )
    )

    text = unsupported_texts(result)
    assert "can_frame、timeout" in text
    assert "不表示这些观测所描述的情况不存在或正常" in text


def test_each_supported_target_gets_its_own_tool_result() -> None:
    """记录涉及多个模块实例时逐个分析，检测对象排在寄存器所属模块之前。"""
    result = run_tools(
        record(
            target="DSPI_C",
            observations=[
                register("OBS-1", "DSPI_C.SR", {"TXRXS": 0}, target="DSPI_C"),
                register("OBS-2", "CAN_A.ESR", {"FLTCONF": "error_active"}, target="CAN_A"),
            ],
        )
    )

    assert result.supported is True
    assert [(item.tool, item.target) for item in result.tool_results] == [
        ("mpc5554.dspi", "DSPI_C"),
        ("mpc5554.flexcan2", "CAN_A"),
    ]
    assert len(result.tool_results) == 2


def test_result_is_json_serialisable() -> None:
    """命令行 --json 依赖该转换，因此它必须能被标准 json 序列化。"""
    result = run_tools(
        record(
            observations=[
                register("OBS-1", "CAN_A.ESR", {"FLTCONF": "bus_off"}),
                register("OBS-2", "CAN_A.CR", {"BOFFREC": 1}),
            ]
        )
    )

    payload = json.loads(json.dumps(to_jsonable(result), ensure_ascii=False))
    assert payload["record_id"] == "REC-TEST-001"
    assert payload["chip"] == "MPC5554"
    assert payload["supported"] is True
    states = payload["tool_results"][0]["confirmed_states"]
    assert [state["id"] for state in states] == [
        "FC-BUSOFF-BOFFREC-DISABLED",
        "FC-BUSOFF-STATE",
        "FC-BUSOFF-NO-ACK-ONLY",
    ]
    assert payload["tool_results"][0]["confirmed_states"][0]["basis"]["knowledge_refs"] == [
        "MPC5554-FlexCAN2-CR-BOFFREC"
    ]


# --- 判据与知识条目的关联 ---------------------------------------------------


def _module_bases() -> Iterator[Tuple[str, Basis]]:
    """规则里以常量声明的依据，例如 _BASIS_FLTCONF。"""
    for module in (flexcan2, dspi):
        for value in vars(module).values():
            if isinstance(value, Basis):
                yield module.TOOL, value


def _conclusion_bases() -> Iterator[Tuple[str, Basis]]:
    """实际跑出来的结论所携带的依据，覆盖只在运行期构造的依据。"""
    rich = record(
        target="CAN_A",
        observations=[
            register("OBS-1", "CAN_A.ESR", {"FLTCONF": "bus_off"}),
            register("OBS-2", "CAN_A.CR", {"LOM": 1, "BOFFREC": 0}),
            register("OBS-3", "CAN_A.ECR", {"TXECTR": 12}),
            register(
                "OBS-4",
                "CAN_A.ESR",
                {"ACKERR": 1},
                semantics="since_last_read",
                covers_since="2026-09-18T10:00:00+08:00",
            ),
            register("OBS-5", "DSPI_C.SR", {"RFOF": 1, "TXRXS": 1}, target="DSPI_C"),
            register("OBS-6", "DSPI_C.MCR", {"MSTR": 1}, target="DSPI_C"),
        ],
    )
    for tool_result in run_tools(rich).tool_results:
        for item in list(tool_result.confirmed_states) + list(tool_result.inconsistencies):
            yield tool_result.tool, item.basis
        # 缺失项也带规则出处，只是没有可引用的规则时为 None。
        for missing in tool_result.insufficient_data:
            if missing.basis is not None:
                yield tool_result.tool, missing.basis


def _curated_ids() -> List[str]:
    return [entry.entry_id for entry in load_curated(CURATED_DIR, REGISTRY)]


@pytest.mark.parametrize("collect", [_module_bases, _conclusion_bases])
def test_every_basis_knowledge_reference_exists(collect: Any) -> None:
    """判据引用的知识条目必须真实存在，避免规则与知识库各自漂移。"""
    known = set(_curated_ids())

    references = [(tool, basis) for tool, basis in collect()]
    assert references, "没有收集到任何依据，检查收集方式"
    for tool, basis in references:
        assert basis.source, "{} 的依据缺少来源".format(tool)
        assert basis.locator, "{} 的依据缺少手册定位".format(tool)
        assert basis.knowledge_refs, "{} 的依据没有关联知识条目".format(tool)
        unknown = [ref for ref in basis.knowledge_refs if ref not in known]
        assert unknown == [], "{} 引用了不存在的知识条目：{}".format(tool, unknown)


def test_every_rule_id_is_unique_and_prefixed() -> None:
    """规则标识按外设前缀命名，避免后续编排时两条规则重名。"""
    rich = record(
        target="CAN_A",
        observations=[
            register("OBS-1", "CAN_A.ESR", {"FLTCONF": "bus_off"}),
            register("OBS-2", "CAN_A.CR", {"LOM": 1, "BOFFREC": 1}),
            register(
                "OBS-3",
                "CAN_A.ESR",
                {"BIT1ERR": 1},
                semantics="since_last_read",
                covers_since="2026-09-18T10:00:00+08:00",
            ),
            register("OBS-4", "DSPI_C.SR", {"TFUF": 1, "RFOF": 1}, target="DSPI_C"),
            register("OBS-5", "DSPI_C.MCR", {"MSTR": 0}, target="DSPI_C"),
        ],
    )

    seen: Dict[str, str] = {}
    for tool_result in run_tools(rich).tool_results:
        for item in list(tool_result.confirmed_states) + list(tool_result.inconsistencies):
            assert item.id not in seen, "规则标识重复：{}".format(item.id)
            seen[item.id] = tool_result.tool
            assert item.id.startswith("FC-") or item.id.startswith("DS-")
            assert item.statement and item.evidence
