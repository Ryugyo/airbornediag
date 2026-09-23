"""FlexCAN2 判据的行为测试。

每个用例对应一条规则的成立、不成立、缺失前提或输入冲突，断言的是「给出了什么结论」
与「结论的边界写清楚了没有」，而不是措辞本身。用例之间不重复同一条逻辑：
FLTCONF 各分支的状态判定、LOM 歧义、读清除标志的三种前提、取值冲突各覆盖一次。
"""

from __future__ import annotations

import pytest

from airbornediag.mcu import RecordView
from airbornediag.mcu.mpc5554 import flexcan2
from mcu_helpers import conflict_ids, missing_texts, record, register, state_ids, unsupported_texts

TARGET = "CAN_A"


def analyse(observations, **kwargs) -> object:
    view = RecordView(record(target=TARGET, observations=observations, **kwargs))
    return flexcan2.analyse_target(view, TARGET)


# --- FLTCONF：故障封闭状态 -------------------------------------------------


def test_bus_off_confirms_state_without_claiming_root_cause() -> None:
    """总线关闭成立：确认该时刻的状态，并说明结论不涉及根因。"""
    result = analyse(
        [
            register("OBS-1", "CAN_A.ESR", {"FLTCONF": "bus_off"}, semantics="instantaneous"),
            register("OBS-2", "CAN_A.CR", {"BOFFREC": 1}, semantics="instantaneous"),
        ]
    )

    ids = state_ids(result)
    assert "FC-BUSOFF-STATE" in ids
    assert "FC-BUSOFF-BOFFREC-DISABLED" in ids
    # 「错误源仅为无应答」被排除，但这不等于找到了根因。
    assert "FC-BUSOFF-NO-ACK-ONLY" in ids
    bus_off = next(state for state in result.confirmed_states if state.id == "FC-BUSOFF-STATE")
    assert "不说明进入总线关闭前的" in bus_off.statement
    assert "也不判断异常位于本节点还是总线上的其他节点" in bus_off.statement
    assert result.insufficient_data == ()


def test_boffrec_zero_confirms_automatic_recovery_instead_of_disabled() -> None:
    """BOFFREC 为 0 时不给出「禁止自动恢复」的结论，只给出对应的那一条。"""
    result = analyse([register("OBS-1", "CAN_A.CR", {"BOFFREC": 0}, semantics="instantaneous")])

    ids = state_ids(result)
    assert ids == ["FC-BUSOFF-BOFFREC-ENABLED"]
    assert "FC-BUSOFF-BOFFREC-DISABLED" not in ids
    # 该配置位的结论不得被读成「总线关闭已经发生」。
    assert "不说明总线关闭是否已经发生" in result.confirmed_states[0].statement


def test_bus_off_with_txectr_requires_same_moment_evidence() -> None:
    """有 TXECTR 取值但无法确认采集时刻仍处于总线关闭时，不用它下任何结论。"""
    result = analyse(
        [
            register("OBS-1", "CAN_A.ESR", {"FLTCONF": "bus_off"}, semantics="instantaneous"),
            register("OBS-2", "CAN_A.ECR", {"TXECTR": 96}, semantics="instantaneous"),
        ]
    )

    assert "CAN_A.ECR.TXECTR" in result.used_fields
    assert "与 FLTCONF 处于同一时刻的 TXECTR 观测" in [
        item.missing for item in result.insufficient_data
    ]
    # 该取值不进入任何状态结论的观测依据。
    assert all("OBS-2" not in state.evidence for state in result.confirmed_states)


def test_error_passive_with_lom_clear_confirms_passive() -> None:
    """仅当 LOM 为 0 时，才把 FLTCONF=01 确认为错误被动。"""
    result = analyse(
        [
            register("OBS-1", "CAN_A.ESR", {"FLTCONF": "error_passive"}, semantics="instantaneous"),
            register("OBS-2", "CAN_A.CR", {"LOM": 0}, semantics="instantaneous"),
        ]
    )

    assert state_ids(result) == ["FC-ERROR-PASSIVE-STATE"]
    state = result.confirmed_states[0]
    assert state.evidence == ("OBS-1", "OBS-2")
    assert "不说明进入错误被动的原因" in state.statement


def test_lom_set_blocks_error_passive_conclusion() -> None:
    """LOM 置位时 FLTCONF 同样显示为 01，因此不得给出错误被动结论。"""
    result = analyse(
        [
            register("OBS-1", "CAN_A.ESR", {"FLTCONF": "error_passive"}, semantics="instantaneous"),
            register("OBS-2", "CAN_A.CR", {"LOM": 1}, semantics="instantaneous"),
        ]
    )

    ids = state_ids(result)
    assert ids == ["FC-LOM-MODE"]
    assert "FC-ERROR-PASSIVE-STATE" not in ids


def test_error_passive_without_lom_is_not_determined() -> None:
    """缺少 LOM 时错误被动不可判定：不给结论，只报缺失。"""
    result = analyse(
        [register("OBS-1", "CAN_A.ESR", {"FLTCONF": "error_passive"}, semantics="instantaneous")]
    )

    assert result.confirmed_states == ()
    assert "CAN_A.CR 的 LOM 取值" in [item.missing for item in result.insufficient_data]
    # 该缺失项的依据是 LOM 会伪造 FLTCONF 这一条，而不是 FLTCONF 的编码本身。
    assert result.insufficient_data[0].basis.knowledge_refs == ("MPC5554-FlexCAN2-ESR-FLTCONF-LOM",)


def test_error_active_state_is_not_a_health_statement() -> None:
    """错误主动是正常态之一，但结论要说明它不等于检测通过。"""
    result = analyse(
        [register("OBS-1", "CAN_A.ESR", {"FLTCONF": "error_active"}, semantics="instantaneous")]
    )

    assert state_ids(result) == ["FC-ERROR-ACTIVE-STATE"]
    assert "不代表该外设工作正常" in result.confirmed_states[0].statement


def test_different_fltconf_values_are_not_merged() -> None:
    """两条观测的 FLTCONF 取值不同：不挑一个当作当前状态，也不声称来自不同时刻。"""
    result = analyse(
        [
            register(
                "OBS-1",
                "CAN_A.ESR",
                {"FLTCONF": "error_active"},
                semantics="instantaneous",
                at="2026-09-18T10:00:00+08:00",
                capture_id="CAP-1",
            ),
            register(
                "OBS-2",
                "CAN_A.ESR",
                {"FLTCONF": "bus_off"},
                semantics="instantaneous",
                at="2026-09-18T10:00:00+08:00",
                capture_id="CAP-1",
            ),
        ]
    )

    assert result.confirmed_states == ()
    assert "同一时刻的 FLTCONF 观测" in [item.missing for item in result.insufficient_data]
    text = missing_texts(result)
    assert "不把其中任一取值当作当前状态" in text
    assert "存在多个不同取值（OBS-1 为 error_active、OBS-2 为 bus_off）" in text
    # 本版不比较观测时刻，因此不能声称两个取值来自不同采集时刻。
    assert "不同采集时刻" not in text


def test_fltconf_marked_since_last_read_is_an_input_contradiction() -> None:
    """FLTCONF 是状态位，标成读清除位与手册矛盾，且不得据此确认当前状态。"""
    result = analyse(
        [register("OBS-1", "CAN_A.ESR", {"FLTCONF": "bus_off"}, semantics="since_last_read")]
    )

    assert conflict_ids(result) == ["FC-FLTCONF-SEMANTICS-CONFLICT"]
    assert result.confirmed_states == ()
    statement = result.inconsistencies[0].statement
    assert "FLTCONF 是状态位" in statement
    assert "不能据它确认采集时刻的故障封闭状态" in statement


def test_mislabeled_fltconf_reading_does_not_block_the_labeled_one() -> None:
    """语义不符的观测被排除在归并之外；排除后取值唯一，仍可确认状态。"""
    result = analyse(
        [
            register("OBS-1", "CAN_A.ESR", {"FLTCONF": "bus_off"}, semantics="instantaneous"),
            # 取值不同：若把它一并归并，会变成「取值不一致」而无法判定。
            register("OBS-2", "CAN_A.ESR", {"FLTCONF": "error_active"}, semantics="since_last_read"),
            register("OBS-3", "CAN_A.CR", {"BOFFREC": 0}, semantics="instantaneous"),
        ]
    )

    assert state_ids(result) == [
        "FC-BUSOFF-BOFFREC-ENABLED",
        "FC-BUSOFF-STATE",
        "FC-BUSOFF-NO-ACK-ONLY",
    ]
    assert conflict_ids(result) == ["FC-FLTCONF-SEMANTICS-CONFLICT"]
    # 语义不符的观测不出现在任何状态结论的观测依据里。
    assert all("OBS-2" not in state.evidence for state in result.confirmed_states)


def test_unreadable_fltconf_value_is_unsupported() -> None:
    """取值无法按手册编码解释时不猜：报不支持，并列出本版支持的写法。"""
    result = analyse([register("OBS-1", "CAN_A.ESR", {"FLTCONF": 3}, semantics="instantaneous")])

    assert result.confirmed_states == ()
    assert "CAN_A.ESR.FLTCONF 的取值" in [item.subject for item in result.unsupported]
    reason = result.unsupported[0].reason
    assert "bus_off/bus-off/1x" in reason
    assert "10、11" in reason


# --- 已观测但本版没有判据的位域 ---------------------------------------------


def test_field_without_criterion_is_reported_as_observed_not_missing() -> None:
    """ESR 的状态位中本版只有 FLTCONF 有判据，其余按不支持报告，不说成未观测。"""
    result = analyse(
        [
            register(
                "OBS-1",
                "CAN_A.ESR",
                {"TXWRN": 1, "BOFFINT": 1},
                semantics="instantaneous",
            )
        ]
    )

    assert result.confirmed_states == ()
    assert result.insufficient_data == ()
    item = result.unsupported[0]
    assert item.subject == "CAN_A.ESR 的位域 TXWRN、BOFFINT"
    assert "记录中已观测到 CAN_A.ESR" in item.reason
    assert "OBS-1" in item.reason
    assert "没有这些位域" in item.reason
    assert result.used_fields == ()


def test_register_without_criterion_is_reported_as_observed() -> None:
    result = analyse([register("OBS-1", "CAN_A.IMASK1", {"BUF31M": 1}, semantics="instantaneous")])

    assert result.unsupported[0].subject == "CAN_A.IMASK1 的观测"
    assert "没有该寄存器" in result.unsupported[0].reason


# --- ESR 读清除错误标志 ----------------------------------------------------


def test_flag_snapshot_states_its_window_and_limits() -> None:
    """快照结论必须写明覆盖区间，并说明不能代表当前状态。"""
    result = analyse(
        [
            register(
                "OBS-1",
                "CAN_A.ESR",
                {"ACKERR": 1, "STFERR": 0},
                semantics="since_last_read",
                covers_since="2026-09-18T10:00:00+08:00",
            )
        ]
    )

    assert state_ids(result) == ["FC-ESR-FLAG-SNAPSHOT"]
    statement = result.confirmed_states[0].statement
    assert "2026-09-18T10:00:00+08:00 至该次读取之间" in statement
    assert "ACKERR" in statement
    # 只报告置位的位，未置位的不逐条罗列，避免读成完整结论。
    assert "STFERR" not in statement
    assert "不能说明当前是否仍在发生同类错误" in statement


def test_flag_snapshot_with_no_flag_raised_is_not_a_pass() -> None:
    """观测到的标志位都未置位，也要写明这不等于检测通过。"""
    result = analyse(
        [
            register(
                "OBS-1",
                "CAN_A.ESR",
                {"ACKERR": 0, "BIT1ERR": 0},
                semantics="since_last_read",
                covers_since="2026-09-18T10:00:00+08:00",
            )
        ]
    )

    statement = result.confirmed_states[0].statement
    assert "均未置位" in statement
    assert "本次未观测的其它位域仍为未知" in statement
    assert "本结论不代表该外设工作正常" in statement


def test_flag_snapshot_without_semantics_is_not_used() -> None:
    """缺少 semantics 时无法确定覆盖区间，标志位取值不用于判断。"""
    result = analyse([register("OBS-1", "CAN_A.ESR", {"ACKERR": 1})])

    assert result.confirmed_states == ()
    assert "观测 OBS-1（CAN_A.ESR）的 semantics" in missing_texts(result)


def test_flag_snapshot_without_cover_start_is_not_used() -> None:
    """标记为 since_last_read 但缺少区间起点时同样不下结论。"""
    result = analyse(
        [register("OBS-1", "CAN_A.ESR", {"ACKERR": 1}, semantics="since_last_read")]
    )

    assert result.confirmed_states == ()
    assert "覆盖区间的起点 covers_since" in missing_texts(result)


def test_flags_marked_instantaneous_conflict_with_the_manual() -> None:
    """把读清除位标成瞬时状态位，属输入自相矛盾。"""
    result = analyse(
        [register("OBS-1", "CAN_A.ESR", {"ACKERR": 1}, semantics="instantaneous")]
    )

    assert conflict_ids(result) == ["FC-ESR-FLAG-SEMANTICS-CONFLICT"]
    # 矛盾记录中的取值不进入状态结论。
    assert result.confirmed_states == ()


# --- 证据缺失与字段使用 ----------------------------------------------------


def test_no_observations_reports_unknown_not_normal() -> None:
    result = analyse([])

    assert result.confirmed_states == ()
    assert "CAN_A 的寄存器观测" in [item.missing for item in result.insufficient_data]
    assert "不等于该外设正常" in missing_texts(result)
    # 没有任何观测时不存在被应用的判据，缺失项不挂依据。
    assert result.insufficient_data[0].basis is None


def test_used_fields_cover_only_observed_fields() -> None:
    """used_fields 只列实际用到的位域，便于核对判据用了哪些观测。"""
    result = analyse(
        [register("OBS-1", "CAN_A.ESR", {"FLTCONF": "error_active"}, semantics="instantaneous")]
    )

    assert result.used_fields == ("CAN_A.ESR.FLTCONF",)


@pytest.mark.parametrize(
    "verdict, expected_id",
    [("bus_off", "FC-BUSOFF-STATE"), ("error_passive", "FC-ERROR-PASSIVE-STATE")],
)
def test_fltconf_accepts_the_manual_spelling(verdict: str, expected_id: str) -> None:
    """FLTCONF 的取值按场景示例的写法解析；错误被动另需 LOM 为 0。"""
    observations = [
        register("OBS-1", "CAN_A.ESR", {"FLTCONF": verdict}, semantics="instantaneous")
    ]
    if verdict == "error_passive":
        observations.append(
            register("OBS-2", "CAN_A.CR", {"LOM": 0}, semantics="instantaneous")
        )

    assert expected_id in state_ids(analyse(observations))


def test_unparsable_observation_text_is_kept_out_of_conclusions() -> None:
    """无法解释的取值只出现在不支持项里，不出现在任何结论的观测依据中。"""
    result = analyse(
        [
            register("OBS-1", "CAN_A.ESR", {"FLTCONF": "unknown_state"}),
            register("OBS-2", "CAN_A.CR", {"LOM": "maybe"}),
        ]
    )

    assert result.confirmed_states == ()
    text = unsupported_texts(result)
    assert "CAN_A.ESR.FLTCONF 的取值" in text
    assert "CAN_A.CR.LOM 的取值" in text
