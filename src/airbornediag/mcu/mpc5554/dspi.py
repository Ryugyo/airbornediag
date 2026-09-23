"""MPC5554 DSPI 的首批判据。

依据 MPC5553/5554 Microcontroller Reference Manual 第 20 章，实现 docs/scenarios.md
的 DS-01（自检失败且无证据）与 DS-02（TFUF 与工作模式自相矛盾），并补充第 20 章
确实给出判据的两类具体条件：RX FIFO 溢出（RFOF）与 TX/RX 运行状态（TXRXS）。

第 20 章的状态位能确认若干**具体条件**（例如 FIFO 溢出或下溢是否发生），但该章不含
错误处理或故障封闭章节，因此不能把这些条件归入某个故障分类，也不能据此确定异常根因。
两类判断的差别在本模块中分别体现：前者进入 confirmed_states，后者写入 insufficient_data。

本模块只使用观测中已解码的位域，不解析原始寄存器值。
"""

from __future__ import annotations

import re
from typing import Any, List, Optional, Tuple

from airbornediag.mcu.model import (
    Basis,
    ConfirmedState,
    Inconsistency,
    MissingItem,
    ToolResult,
    UnsupportedItem,
    unique_in_order,
)
from airbornediag.mcu.record import (
    FieldReading,
    RecordView,
    RegisterObservation,
    field_pairs,
    flag_value,
    resolve_field,
    uncriterioned_fields,
    unmergeable_item,
)

TOOL = "mpc5554.dspi"
PERIPHERAL = "DSPI"
CHIP = "MPC5554"
SOURCE = "MPC5554_RM"

# 手册 1.5.16：MPC5554 含四个 DSPI 模块（MPC5553 为三个）。实例清单按手册核对，
# 本版只处理这四个；记录中出现其它实例名时按不支持报告，不假定它存在。
INSTANCES = ("DSPI_A", "DSPI_B", "DSPI_C", "DSPI_D")
TARGET_PATTERN = re.compile(r"^(?:{})$".format("|".join(INSTANCES)))

# 本版有位域判据的字段：寄存器名 -> 字段名。用于把「已观测但本版没有判据」与
# 「未观测」区分开。
CRITERION_FIELDS = {
    "SR": ("TFUF", "RFOF", "TXRXS"),
    "MCR": ("MSTR",),
}

_SR_LOCATOR = "20.3.2.4, Table 20-6"
_MCR_LOCATOR = "20.3.2.1, Table 20-3"
_RUNSTATE_LOCATOR = "20.3.2.4, Table 20-6; 20.4.2, Table 20-17"
_CHAPTER_LOCATOR = "20.3.2.4, Table 20-6; 20.4.9"

# TFUF 与 MSTR 共同成立的判据：断定「矛盾」需要同时引用下溢的检测条件与
# 主从模式的编码。
_BASIS_TFUF_MODE = Basis(
    SOURCE,
    "20.3.2.4, Table 20-6; 20.3.2.1, Table 20-3",
    ("MPC5554-DSPI-SR-TFUF", "MPC5554-DSPI-MCR-MSTR"),
)
_BASIS_TFUF = Basis(SOURCE, _SR_LOCATOR, ("MPC5554-DSPI-SR-TFUF",))
_BASIS_RFOF = Basis(SOURCE, _SR_LOCATOR, ("MPC5554-DSPI-SR-FIFO-FLAGS",))
_BASIS_TXRXS = Basis(SOURCE, _RUNSTATE_LOCATOR, ("MPC5554-DSPI-SR-TXRXS",))
_BASIS_MSTR = Basis(SOURCE, _MCR_LOCATOR, ("MPC5554-DSPI-MCR-MSTR",))
_BASIS_SCOPE = Basis(SOURCE, _CHAPTER_LOCATOR, ("MPC5554-DSPI-CHAPTER-SCOPE",))

_CONFLICT_ID = "DS-TFUF-MODE-CONFLICT"


def matches_target(name: str) -> bool:
    """该模块实例是否由本工具处理，例如 DSPI_C。"""
    return bool(TARGET_PATTERN.match(name))


def _flag_mapper(raw: Any) -> Optional[int]:
    return flag_value(raw)


def _unreadable_item(target: str, register: str, field: str) -> UnsupportedItem:
    return UnsupportedItem(
        subject="{}.{}.{} 的取值".format(target, register, field),
        reason=(
            "该标志位的取值无法解释为置位或未置位。本版不把原始取值当作整数按位解释："
            "位编号尚未逐条核对手册入知识库，这样做没有可核对的依据，"
            "因此该观测不参与判断。"
        ),
    )


def _unsupported_field_items(target: str, observations: Tuple[RegisterObservation, ...]):
    """已观测但本版没有判据的寄存器与位域。

    这些取值出现在记录里，只是本版没有对应判据；报告时不能说成「未观测」。
    """
    items = []
    for register, fields, observation_ids in uncriterioned_fields(observations, CRITERION_FIELDS):
        known = register in CRITERION_FIELDS
        items.append(
            UnsupportedItem(
                subject=(
                    "{}.{} 的位域 {}".format(target, register, "、".join(fields))
                    if known
                    else "{}.{} 的观测".format(target, register)
                ),
                reason=(
                    "记录中已观测到 {}.{} 的 {}（观测 {}），但本版{}的判据，"
                    "因此它们不参与判断。不支持表示本版没有依据，"
                    "不等于这些取值未观测，也不等于取值正常。".format(
                        target,
                        register,
                        "、".join(fields),
                        "、".join(observation_ids),
                        "没有这些位域" if known else "没有该寄存器",
                    )
                ),
            )
        )
    return items


def analyse_target(view: RecordView, target: str) -> ToolResult:
    """分析一个 DSPI 模块实例，返回工具结果。"""
    observations = view.register_observations_for(target)
    used: List[str] = []
    states: List[ConfirmedState] = []
    conflicts: List[Inconsistency] = []
    missing: List[MissingItem] = []
    unsupported: List[UnsupportedItem] = []

    def use(register: str, field: str) -> None:
        used.append("{}.{}.{}".format(target, register, field))

    def read(register: str, field: str) -> Optional[FieldReading]:
        pairs = field_pairs(observations, field, register)
        if not pairs:
            return None
        use(register, field)
        reading = resolve_field(pairs, _flag_mapper)
        if reading.unreadable:
            unsupported.append(_unreadable_item(target, register, field))
        return reading

    mstr = read("MCR", "MSTR")
    tfuf = read("SR", "TFUF")
    rfof = read("SR", "RFOF")
    txrxs = read("SR", "TXRXS")

    # 同一字段存在多个不同取值时统一报为不可合并：本版不做时序分析，无法确定哪个取值
    # 对应当前状态，但不把这些取值说成未观测。
    for reading, subject, consequence, basis in (
        (mstr, "可用于判定 DSPI 工作模式的时刻", "本次无法确定该 DSPI 的工作模式。", _BASIS_MSTR),
        (tfuf, "可用于判定 TFUF 对应工作模式的时刻", "本次不给出 TFUF 相关的状态结论。", _BASIS_TFUF),
        (rfof, "可用于判定 RX FIFO 是否溢出的观测", "本次不给出 RX FIFO 溢出结论。", _BASIS_RFOF),
        (txrxs, "可用于判定 TX/RX 运行状态的观测", "本次不给出 TX/RX 运行状态结论。", _BASIS_TXRXS),
    ):
        if reading is not None and reading.conflicting:
            missing.append(unmergeable_item(subject, reading, consequence, basis))

    # --- DS-02：TFUF 与工作模式 ------------------------------------------
    if tfuf is not None and not tfuf.conflicting:
        if tfuf.value == 1:
            if mstr is not None and mstr.value == 1:
                conflicts.append(
                    Inconsistency(
                        id=_CONFLICT_ID,
                        statement=(
                            "{target}.SR 的 TFUF 置位表示发生了发送 FIFO 下溢，"
                            "但依据 Table 20-6，该下溢条件仅对工作在 SPI 从模式的 DSPI 检测；"
                            "{target}.MCR 的 MSTR 为 1 表示主机模式。两者不能同时成立，"
                            "属输入自相矛盾。在矛盾澄清之前，本工具不给出任何依赖 TFUF 与 "
                            "MSTR 的状态结论；同一次读取中不依赖这两个字段的判据不受影响。".format(
                                target=target
                            )
                        ),
                        evidence=unique_in_order(list(tfuf.evidence) + list(mstr.evidence)),
                        basis=_BASIS_TFUF_MODE,
                    )
                )
            elif mstr is not None and mstr.value == 0:
                states.append(
                    ConfirmedState(
                        id="DS-TFUF-UNDERFLOW",
                        statement=(
                            "{target} 工作在 SPI 从模式，其 TX FIFO 发生了下溢（TFUF 置位）："
                            "从模式下 TX FIFO 为空、而外部 SPI 主控发起了一次传输。"
                            "本结论只说明下溢发生，不说明发生的时刻与次数，"
                            "也不说明外部主控为何在 TX FIFO 为空时发起传输。".format(target=target)
                        ),
                        evidence=tfuf.evidence,
                        basis=_BASIS_TFUF,
                    )
                )
            elif mstr is None or (mstr.value is None and not mstr.conflicting):
                missing.append(
                    MissingItem(
                        "{}.MCR 的 MSTR 取值".format(target),
                        "手册规定 TFUF 的下溢条件仅对工作在 SPI 从模式的 DSPI 检测，"
                        "而 {} 的 TFUF 已置位。记录中没有可用的 MSTR 取值，"
                        "该 TFUF 既可能是从模式下真实发生的下溢，也可能是主机模式下的"
                        "输入自相矛盾，二者无法区分，因此本次不给出该状态的结论。".format(target),
                        basis=_BASIS_TFUF_MODE,
                    )
                )

    # --- RX FIFO 溢出 -----------------------------------------------------
    if rfof is not None and rfof.value == 1:
        states.append(
            ConfirmedState(
                id="DS-RFOF-OVERFLOW",
                statement=(
                    "{} 的 RX FIFO 发生了溢出：RX FIFO 与移位寄存器都已满时，"
                    "又发起了一次传输。本结论只说明溢出发生，"
                    "不说明受影响数据的内容与数量。".format(target)
                ),
                evidence=rfof.evidence,
                basis=_BASIS_RFOF,
            )
        )

    # --- TX/RX 运行状态 ---------------------------------------------------
    if txrxs is not None and txrxs.value in (0, 1):
        if txrxs.value == 1:
            statement = (
                "{target} 的 TX/RX 操作已启用，采集时刻处于 RUNNING 状态。"
                "本结论只说明运行状态，不说明传输的数据是否正确，"
                "也不说明检测结论的成因。".format(target=target)
            )
        else:
            statement = (
                "{target} 的 TX/RX 操作被禁用，采集时刻处于 STOPPED 状态；"
                "该状态下主模式不发起任何传输、从模式不应答任何传输。"
                "本结论只说明运行状态，不说明该状态是否符合预期，"
                "也不说明检测结论的成因。".format(target=target)
            )
        states.append(
            ConfirmedState(
                id="DS-TX-RX-RUN-STATE",
                statement=statement,
                evidence=txrxs.evidence,
                basis=_BASIS_TXRXS,
            )
        )

    # --- 没有可用证据 -----------------------------------------------------
    if not observations:
        test_applies = view.test_target == target and view.test_result == "fail"
        if test_applies:
            test_id = view.test_id or "本次检测"
            head = "检测 {} 报失败，但记录中没有 {} 的任何寄存器观测。".format(test_id, target)
        else:
            head = "记录中没有 {} 的任何寄存器观测。".format(target)
        missing.append(
            MissingItem(
                "{} 的寄存器观测（DSPIx_SR 的状态与标志位、DSPIx_MCR 的 MSTR）".format(target),
                head
                + "第 20 章不含错误处理或故障封闭章节：DSPIx_SR 的状态位能确认若干"
                "具体条件（FIFO 溢出/下溢、TX/RX 运行状态、传输完成），但该章没有把这些"
                "条件归入故障分类的规则；仅凭检测结论无法把这次失败归入某个故障状态，"
                "也无法在「传输未启动」「传输完成但数据不符」「接收缓冲异常」之间区分，"
                "因此本次不给出 {} 的故障状态与根因结论。"
                "缺少观测表示未知，不等于该外设正常。".format(target),
                basis=_BASIS_SCOPE,
            )
        )

    # 输入自相矛盾时只抑制依赖矛盾数据的判断：矛盾涉及 TFUF 与 MSTR 两个字段，
    # 基于它们的结论不输出；其它位域的判据不依赖这两个字段，证据独立，仍然有效。
    if any(item.id == _CONFLICT_ID for item in conflicts):
        missing.append(
            MissingItem(
                "澄清 TFUF 与 MSTR 矛盾所需的证据",
                "在矛盾澄清之前，本工具不给出任何依赖 TFUF 与 MSTR 的状态结论。"
                "同一次读取中不依赖这两个字段、证据独立的判据（RX FIFO 溢出、"
                "TX/RX 运行状态）仍然成立；但该次读取整体是否自洽尚未确认。",
                basis=_BASIS_TFUF_MODE,
            )
        )

    unsupported.extend(_unsupported_field_items(target, observations))

    return ToolResult(
        tool=TOOL,
        chip=CHIP,
        peripheral=PERIPHERAL,
        target=target,
        used_fields=unique_in_order(used),
        confirmed_states=tuple(states),
        inconsistencies=tuple(conflicts),
        insufficient_data=tuple(missing),
        unsupported=tuple(unsupported),
    )
