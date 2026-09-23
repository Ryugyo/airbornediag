"""DSPI 判据的行为测试。

覆盖 DS-01（自检失败且无证据）、DS-02（TFUF 与工作模式矛盾），以及第 20 章确实
给出判据的两类具体条件：RX FIFO 溢出与 TX/RX 运行状态。
重点在两类判断的分界：具体条件进入 confirmed_states，故障状态与根因只报缺失。
"""

from __future__ import annotations

from airbornediag.mcu import RecordView
from airbornediag.mcu.mpc5554 import dspi
from mcu_helpers import conflict_ids, missing_texts, record, register, state_ids

TARGET = "DSPI_C"


def analyse(observations, **kwargs) -> object:
    view = RecordView(record(target=TARGET, observations=observations, **kwargs))
    return dspi.analyse_target(view, TARGET)


def sr(observation_id: str, fields, **kwargs):
    return register(observation_id, "DSPI_C.SR", fields, target=TARGET, **kwargs)


def mcr(observation_id: str, fields, **kwargs):
    return register(observation_id, "DSPI_C.MCR", fields, target=TARGET, **kwargs)


# --- DS-01：检测失败但没有任何观测 -----------------------------------------


def test_self_test_failure_without_observations_yields_no_conclusion() -> None:
    """检测报失败、记录中没有观测：不给故障状态，也不给根因。"""
    result = analyse([], result="fail", test_id="DSPI-SELF-TEST")

    assert result.confirmed_states == ()
    assert len(result.insufficient_data) == 1
    item = result.insufficient_data[0]
    assert TARGET in item.missing
    assert "DSPI-SELF-TEST" in item.reason
    # 引用检测结论不等于接受它：章节范围要一并说明。
    assert "不含错误处理或故障封闭章节" in item.reason
    assert "缺少观测表示未知，不等于该外设正常" in item.reason
    # 本规则只产出缺失项，规则出处挂在缺失项上，否则该判据无从核对。
    assert item.basis.knowledge_refs == ("MPC5554-DSPI-CHAPTER-SCOPE",)


def test_absence_of_observation_is_reported_for_a_passing_test_too() -> None:
    """检测通过也照报「没有观测」：缺失不被读成没有异常。"""
    passed = analyse([], result="pass", test_id="DSPI-SELF-TEST")

    assert passed.confirmed_states == ()
    assert len(passed.insufficient_data) == 1
    # 检测未报失败时，缺失的说明不引用该检测的结论。
    assert "DSPI-SELF-TEST" not in passed.insufficient_data[0].reason


# --- DS-02：TFUF 与工作模式 ------------------------------------------------


def test_tfuf_in_slave_mode_confirms_underflow() -> None:
    """从模式下 TFUF 置位：确认下溢这一具体条件。"""
    result = analyse(
        [
            mcr("OBS-1", {"MSTR": 0}, semantics="instantaneous"),
            sr("OBS-2", {"TFUF": 1}, semantics="instantaneous"),
        ]
    )

    assert state_ids(result) == ["DS-TFUF-UNDERFLOW"]
    statement = result.confirmed_states[0].statement
    assert "不说明发生的时刻与次数" in statement


def test_tfuf_in_master_mode_is_an_input_contradiction() -> None:
    """主模式下 TFUF 不可能置位：报矛盾，且不给任何状态结论。"""
    result = analyse(
        [
            mcr("OBS-1", {"MSTR": 1}, semantics="instantaneous"),
            sr("OBS-2", {"TFUF": 1}, semantics="instantaneous"),
        ]
    )

    assert conflict_ids(result) == ["DS-TFUF-MODE-CONFLICT"]
    assert result.confirmed_states == ()
    assert "澄清 TFUF 与 MSTR 矛盾所需的证据" in [
        item.missing for item in result.insufficient_data
    ]


def test_tfuf_without_mstr_is_not_determined() -> None:
    """缺少 MSTR 时无法区分真实下溢与输入矛盾：不给结论。"""
    result = analyse([sr("OBS-1", {"TFUF": 1}, semantics="instantaneous")])

    assert result.confirmed_states == ()
    assert result.inconsistencies == ()
    assert "DSPI_C.MCR 的 MSTR 取值" in [item.missing for item in result.insufficient_data]


def test_tfuf_not_set_confirms_nothing() -> None:
    """TFUF 未置位不构成任何结论：未发生下溢不等于该外设正常。"""
    result = analyse(
        [
            mcr("OBS-1", {"MSTR": 0}, semantics="instantaneous"),
            sr("OBS-2", {"TFUF": 0}, semantics="instantaneous"),
        ]
    )

    assert result.confirmed_states == ()
    assert result.unsupported == ()
    assert missing_texts(result) == ""


# --- RX FIFO 溢出与运行状态 ------------------------------------------------


def test_rfof_set_confirms_overflow() -> None:
    result = analyse([sr("OBS-1", {"RFOF": 1}, semantics="instantaneous")])

    assert state_ids(result) == ["DS-RFOF-OVERFLOW"]
    assert "不说明受影响数据的内容与数量" in result.confirmed_states[0].statement


def test_run_state_reports_both_directions() -> None:
    """TXRXS 的两个取值各自成立，且都不涉及故障状态。"""
    running = analyse([sr("OBS-1", {"TXRXS": 1}, semantics="instantaneous")])
    stopped = analyse([sr("OBS-1", {"TXRXS": 0}, semantics="instantaneous")])

    assert state_ids(running) == ["DS-TX-RX-RUN-STATE"]
    assert "RUNNING 状态" in running.confirmed_states[0].statement
    assert state_ids(stopped) == ["DS-TX-RX-RUN-STATE"]
    stopped_statement = stopped.confirmed_states[0].statement
    assert "STOPPED 状态" in stopped_statement
    # 停机状态是默认状态，不能读成异常，也不能读成正常。
    assert "不说明该状态是否符合预期" in stopped_statement


def test_contradiction_suppresses_the_other_conclusions() -> None:
    """出现矛盾时，同一记录中其他已成立的具体条件也不作为结论输出。"""
    result = analyse(
        [
            mcr("OBS-1", {"MSTR": 1}, semantics="instantaneous"),
            sr("OBS-2", {"TFUF": 1, "RFOF": 1, "TXRXS": 0}, semantics="instantaneous"),
        ]
    )

    assert conflict_ids(result) == ["DS-TFUF-MODE-CONFLICT"]
    assert result.confirmed_states == ()


def test_unreadable_flag_value_is_unsupported() -> None:
    result = analyse([sr("OBS-1", {"RFOF": "set"}, semantics="instantaneous")])

    assert result.confirmed_states == ()
    assert "DSPI_C.SR.RFOF 的取值" in [item.subject for item in result.unsupported]
