"""MPC5554 FlexCAN2 的首批判据。

依据 MPC5553/5554 Microcontroller Reference Manual 第 22 章，实现 docs/scenarios.md
的 FC-01（总线关闭）与 FC-02（错误被动与只听模式歧义）。每个规则标识对应一条可核对
的判据，并与 knowledge/curated/mpc5554-flexcan2.json 中的条目 id 关联。

本模块只使用观测中已解码的位域，不解析原始寄存器值：位编号尚未逐条核对手册入知识库，
按位解释原始值没有可核对的依据。

缺失的信息一律作为缺失项返回，不跳过判据，也不补成正常取值。
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

TOOL = "mpc5554.flexcan2"
PERIPHERAL = "FlexCAN2"
CHIP = "MPC5554"
SOURCE = "MPC5554_RM"

# 手册 22.1：MPC5554 含三个 FlexCAN2 模块。实例清单按手册核对，本版只处理这三个；
# 记录中出现其它实例名时按不支持报告，不假定它存在。
INSTANCES = ("CAN_A", "CAN_B", "CAN_C")
TARGET_PATTERN = re.compile(r"^(?:{})$".format("|".join(INSTANCES)))

# 手册中 CANx_ESR 的读清除错误标志位（22.3.3.6，Table 22-11）。
ERROR_FLAGS = ("BIT1ERR", "BIT0ERR", "ACKERR", "CRCERR", "FRMERR", "STFERR")

# 本版有位域判据的字段：寄存器名 -> 字段名。用于把「已观测但本版没有判据」与
# 「未观测」区分开：两者都不参与判断，但前者是记录里确实给出、被本版丢弃的取值。
CRITERION_FIELDS = {
    "ESR": ("FLTCONF",) + ERROR_FLAGS,
    "CR": ("LOM", "BOFFREC"),
    "ECR": ("TXECTR",),
}

# FLTCONF 的三种编码（22.3.3.6，Table 22-11）。取值的写法按手册的 00/01/1X 与
# 示例记录使用的符号名各接受一种，不接受整数：该字段的位宽与编码未逐条核对入知识库。
_BUS_OFF = ("bus_off", "bus-off", "1x")
_ERROR_PASSIVE = ("error_passive", "error-passive", "01")
_ERROR_ACTIVE = ("error_active", "error-active", "00")

# 本版接受的 FLTCONF 写法，用于取值落在其外时给出明确提示。
_FLTCONF_ACCEPTED = "bus_off/bus-off/1x、error_passive/error-passive/01、error_active/error-active/00"

_ESR_LOCATOR = "22.3.3.6, Figure 22-8, Table 22-11"
_CR_LOCATOR = "22.3.3.2, Table 22-8"
_ECR_LOCATOR = "22.3.3.5"

_BASIS_FLTCONF = Basis(SOURCE, _ESR_LOCATOR, ("MPC5554-FlexCAN2-ESR-FLTCONF",))
_BASIS_FLAGS = Basis(SOURCE, _ESR_LOCATOR, ("MPC5554-FlexCAN2-ESR-READCLEAR",))
_BASIS_LOM_AMBIGUITY = Basis(SOURCE, "22.3.3.6, Table 22-11", ("MPC5554-FlexCAN2-ESR-FLTCONF-LOM",))
_BASIS_BUSOFF_TXECTR = Basis(SOURCE, _ECR_LOCATOR, ("MPC5554-FlexCAN2-ECR-BUSOFF-TXECTR",))
_BASIS_SINGLE_NODE = Basis(SOURCE, _ECR_LOCATOR, ("MPC5554-FlexCAN2-ECR-SINGLE-NODE",))
_BASIS_BOFFREC = Basis(SOURCE, _CR_LOCATOR, ("MPC5554-FlexCAN2-CR-BOFFREC",))
_BASIS_LOM = Basis(SOURCE, _CR_LOCATOR, ("MPC5554-FlexCAN2-CR-LOM",))


def matches_target(name: str) -> bool:
    """该模块实例是否由本工具处理，例如 CAN_A。"""
    return bool(TARGET_PATTERN.match(name))


def fltconf_code(raw: Any) -> Optional[str]:
    """把 FLTCONF 的取值规整为 state 名；无法解释时返回 None。"""
    if not isinstance(raw, str):
        return None
    text = raw.strip().lower()
    if text in _BUS_OFF:
        return "bus_off"
    if text in _ERROR_PASSIVE:
        return "error_passive"
    if text in _ERROR_ACTIVE:
        return "error_active"
    return None


def _flag_mapper(raw: Any) -> Optional[int]:
    return flag_value(raw)


def _unreadable_item(target: str, register: str, field: str) -> UnsupportedItem:
    return UnsupportedItem(
        subject="{}.{}.{} 的取值".format(target, register, field),
        reason=(
            "该位域的取值无法按手册的编码解释。本版不把原始取值当作整数按位解释："
            "位编号尚未逐条核对手册入知识库，这样做没有可核对的依据，"
            "因此该观测不参与判断。"
        ),
    )


def _fltconf_unsupported(target: str) -> UnsupportedItem:
    """FLTCONF 取值不在本版支持的写法内时的提示。"""
    return UnsupportedItem(
        subject="{}.ESR.FLTCONF 的取值".format(target),
        reason=(
            "本版不把原始取值当作整数按位解释，FLTCONF 只接受手册编码与符号名这几组写法："
            "{}（大小写不敏感）。手册中属于 1X 的其它位组合（10、11）本版尚未实现，"
            "同样报为不支持。该取值不在支持的写法内，因此不参与判断："
            "不支持表示本版没有依据，不等于该字段未观测，也不等于取值正常。".format(
                _FLTCONF_ACCEPTED
            )
        ),
    )


def _unsupported_field_items(target: str, observations: Tuple[RegisterObservation, ...]):
    """已观测但本版没有判据的寄存器与位域。

    这些取值出现在记录里，只是本版没有对应判据；报告时不能说成「未观测」。
    """
    items = []
    for register, fields, observation_ids in uncriterioned_fields(observations, CRITERION_FIELDS):
        known = register in CRITERION_FIELDS
        label = "{}.{} 的位域 {}".format(target, register, "、".join(fields)) if known else (
            "{}.{} 的观测".format(target, register)
        )
        items.append(
            UnsupportedItem(
                subject=label,
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
    """分析一个 FlexCAN2 模块实例，返回工具结果。"""
    observations = view.register_observations_for(target)
    used: List[str] = []
    states: List[ConfirmedState] = []
    conflicts: List[Inconsistency] = []
    missing: List[MissingItem] = []
    unsupported: List[UnsupportedItem] = []

    def use(register: str, field: str) -> None:
        used.append("{}.{}.{}".format(target, register, field))

    # --- FLTCONF：故障封闭状态 -------------------------------------------
    fltconf_pairs = field_pairs(observations, "FLTCONF", "ESR")
    # FLTCONF 是状态位（22.3.3.6），契约要求状态位标注 instantaneous。标成
    # since_last_read 与手册矛盾，这类观测的取值不用于确认采集时刻的状态。
    fltconf_usable = tuple(
        pair for pair in fltconf_pairs if pair[0].semantics != "since_last_read"
    )
    fltconf: Optional[FieldReading] = None
    if fltconf_pairs:
        use("ESR", "FLTCONF")
        mislabeled = tuple(
            observation.observation_id
            for observation, _ in fltconf_pairs
            if observation.semantics == "since_last_read"
        )
        if mislabeled:
            conflicts.append(
                Inconsistency(
                    id="FC-FLTCONF-SEMANTICS-CONFLICT",
                    statement=(
                        "观测 {} 把 FLTCONF 标注为 semantics=since_last_read（读清除位），"
                        "但手册 22.3.3.6 规定 FLTCONF 是状态位、不被读取操作清除。"
                        "两者不能同时成立：该取值不用于判断，也不能据它确认采集时刻的"
                        "故障封闭状态。".format("、".join(mislabeled))
                    ),
                    evidence=mislabeled,
                    basis=_BASIS_FLAGS,
                )
            )
        if fltconf_usable:
            fltconf = resolve_field(list(fltconf_usable), fltconf_code)
            if fltconf.unreadable:
                unsupported.append(_fltconf_unsupported(target))
            if fltconf.conflicting:
                missing.append(
                    unmergeable_item(
                        "同一时刻的 FLTCONF 观测",
                        fltconf,
                        "本次不给出故障封闭状态结论，也不把其中任一取值当作当前状态。",
                        _BASIS_FLTCONF,
                    )
                )

    # --- LOM：只听模式 ----------------------------------------------------
    lom_pairs = field_pairs(observations, "LOM", "CR")
    lom: Optional[FieldReading] = None
    if lom_pairs:
        use("CR", "LOM")
        lom = resolve_field(lom_pairs, _flag_mapper)
        if lom.unreadable:
            unsupported.append(_unreadable_item(target, "CR", "LOM"))
        if lom.conflicting:
            missing.append(
                unmergeable_item(
                    "可用于判定只听模式的时刻",
                    lom,
                    "本次无法判定该模块的只听模式配置。",
                    _BASIS_LOM,
                )
            )

    # --- BOFFREC：总线关闭恢复方式 ---------------------------------------
    boffrec_pairs = field_pairs(observations, "BOFFREC", "CR")
    if boffrec_pairs:
        use("CR", "BOFFREC")
        boffrec = resolve_field(boffrec_pairs, _flag_mapper)
        if boffrec.unreadable:
            unsupported.append(_unreadable_item(target, "CR", "BOFFREC"))
        if boffrec.conflicting:
            missing.append(
                unmergeable_item(
                    "可用于判定总线关闭恢复方式的时刻",
                    boffrec,
                    "本次无法判定该模块的总线关闭恢复方式。",
                    _BASIS_BOFFREC,
                )
            )
        elif boffrec.value == 1:
            states.append(
                ConfirmedState(
                    id="FC-BUSOFF-BOFFREC-DISABLED",
                    statement=(
                        "在 {}.CR 的 BOFFREC 位为 1 期间，模块不会自动从总线关闭恢复，"
                        "将保持该状态直至该位被清除。本结论只描述该配置位的作用："
                        "不说明总线关闭是否已经发生，也不判断该配置是设计意图还是配置错误。".format(target)
                    ),
                    evidence=boffrec.evidence,
                    basis=_BASIS_BOFFREC,
                )
            )
        elif boffrec.value == 0:
            states.append(
                ConfirmedState(
                    id="FC-BUSOFF-BOFFREC-ENABLED",
                    statement=(
                        "在 {}.CR 的 BOFFREC 位为 0 期间，模块按 CAN 规范 2.0B 自动尝试"
                        "从总线关闭恢复；恢复过程仍需总线上出现 128 次「11 个连续隐性位」。"
                        "本结论只描述该配置位的作用：不说明总线关闭是否已经发生，"
                        "不说明恢复需要多久，也不说明总线关闭的成因。".format(target)
                    ),
                    evidence=boffrec.evidence,
                    basis=_BASIS_BOFFREC,
                )
            )

    # --- LOM 置位：只看模式 ------------------------------------------------
    if lom is not None and lom.value == 1:
        states.append(
            ConfirmedState(
                id="FC-LOM-MODE",
                statement=(
                    "{}.CR 的 LOM 为 1，模块处于只听模式：以 CAN 错误被动方式工作、"
                    "冻结全部错误计数器、接收报文但不发送应答，且无法发送任何报文。"
                    "本结论只说明该配置位与工作模式，不说明该配置的成因。".format(target)
                ),
                evidence=lom.evidence,
                basis=_BASIS_LOM,
            )
        )

    # --- FLTCONF 判定 -----------------------------------------------------
    bus_off = fltconf is not None and fltconf.value == "bus_off"
    if bus_off:
        states.append(
            ConfirmedState(
                id="FC-BUSOFF-STATE",
                statement=(
                    "采集时刻 {} 处于总线关闭（bus off）状态。本结论只描述该时刻的"
                    "故障封闭状态：不说明总线关闭发生的时刻，不说明进入总线关闭前的"
                    "错误类型分布，也不判断异常位于本节点还是总线上的其他节点。".format(target)
                ),
                evidence=fltconf.evidence,
                basis=_BASIS_FLTCONF,
            )
        )
        states.append(
            ConfirmedState(
                id="FC-BUSOFF-NO-ACK-ONLY",
                statement=(
                    "进入总线关闭前的错误累积不可能仅来自无其他节点应答（ACK 错误）。"
                    "本结论只排除「错误源仅为无应答」这一条路径：不排除总线上只有一个节点，"
                    "单节点总线若同时存在位错误、填充错误等其他错误源，发送错误计数器"
                    "仍可持续累积并进入总线关闭。"
                ),
                evidence=fltconf.evidence,
                basis=_BASIS_SINGLE_NODE,
            )
        )
    elif fltconf is not None and fltconf.value == "error_active":
        states.append(
            ConfirmedState(
                id="FC-ERROR-ACTIVE-STATE",
                statement=(
                    "采集时刻 {} 处于错误主动（error active）状态。本结论只描述该时刻的"
                    "故障封闭状态，不代表该外设工作正常，也不代表检测通过。".format(target)
                ),
                evidence=fltconf.evidence,
                basis=_BASIS_FLTCONF,
            )
        )
    elif fltconf is not None and fltconf.value == "error_passive":
        if lom is None or lom.conflicting or lom.value is None:
            missing.append(
                MissingItem(
                    "{}.CR 的 LOM 取值".format(target),
                    "手册 22.3.3.6 说明 LOM 置位时 FLTCONF 会被强制显示为「错误被动」，"
                    "因此仅凭 FLTCONF 为 01 无法区分真实的错误被动与只听模式。"
                    "缺少 LOM 的取值，模块的故障封闭状态不可判定，也无法判断该模块"
                    "当前是否允许发送报文。",
                    basis=_BASIS_LOM_AMBIGUITY,
                )
            )
        elif lom.value == 0:
            states.append(
                ConfirmedState(
                    id="FC-ERROR-PASSIVE-STATE",
                    statement=(
                        "{} 处于错误被动（error passive）状态。本结论以 {}.CR 的 LOM 为 0 "
                        "为前提：LOM 置位时 FLTCONF 同样显示为错误被动，缺少 LOM 时该状态"
                        "不可判定。本结论不说明进入错误被动的原因，也不说明错误计数器"
                        "此后的变化方向。".format(target, target)
                    ),
                    evidence=unique_in_order(list(fltconf.evidence) + list(lom.evidence)),
                    basis=_BASIS_FLTCONF,
                )
            )

    # --- TXECTR 的语义 ----------------------------------------------------
    txectr_pairs = field_pairs(observations, "TXECTR", "ECR")
    if bus_off and txectr_pairs:
        txectr_observation = txectr_pairs[0][0]
        use("ECR", "TXECTR")
        fltconf_observation = fltconf_usable[0][0] if fltconf_usable else None
        missing.append(
            MissingItem(
                "与 FLTCONF 处于同一时刻的 TXECTR 观测",
                "手册 22.3.3.5 规定进入总线关闭时 TXECTR 被复位为零并与另一个内部计数器"
                "级联，改为计数总线上「11 个连续隐性位」的出现次数，此时它已不是发送错误"
                "计数。要把 {} 的取值用于任何判断，都需先确认该次采集时模块仍处于总线关闭；"
                "该观测与 FLTCONF 观测{}来自不同的寄存器读取，契约中 capture_id 只表示"
                "同一次读取动作、不表示不同寄存器之间同步，因此这个前提未被观测，"
                "该 TXECTR 取值的语义是否已经改变无法判定。".format(
                    txectr_observation.observation_id,
                    "（{}）".format(fltconf_observation.observation_id)
                    if fltconf_observation is not None
                    else "",
                ),
                basis=_BASIS_BUSOFF_TXECTR,
            )
        )

    # --- 读清除错误标志的快照 ---------------------------------------------
    for observation in observations:
        if observation.register_name != "ESR":
            continue
        present = [name for name in ERROR_FLAGS if observation.has_field(name)]
        if not present:
            continue
        for name in present:
            use("ESR", name)

        if observation.semantics == "instantaneous":
            conflicts.append(
                Inconsistency(
                    id="FC-ESR-FLAG-SEMANTICS-CONFLICT",
                    statement=(
                        "观测 {} 把 {} 的 {} 标注为 semantics=instantaneous（瞬时状态位），"
                        "但手册 22.3.3.6 规定这些位是读清除位，只反映上次读取以来发生的"
                        "错误条件，且读取动作本身会清除它们。两者不能同时成立：该观测的"
                        "覆盖区间无法确定，其中的取值不用于判断。".format(
                            observation.observation_id, observation.register, "、".join(present)
                        )
                    ),
                    evidence=(observation.observation_id,),
                    basis=_BASIS_FLAGS,
                )
            )
        elif observation.semantics != "since_last_read":
            missing.append(
                MissingItem(
                    "观测 {}（{}）的 semantics（该次采集的语义）".format(
                        observation.observation_id, observation.register
                    ),
                    "手册 22.3.3.6 说明 {} 为读清除位，其内容只反映上次读取以来的情况；"
                    "缺少 semantics 就无法确定这些取值覆盖的时间区间，"
                    "因此 {} 的取值不用于判断。".format("、".join(present), observation.register),
                    basis=_BASIS_FLAGS,
                )
            )
        elif not observation.covers_since:
            missing.append(
                MissingItem(
                    "观测 {}（{}）覆盖区间的起点 covers_since".format(
                        observation.observation_id, observation.register
                    ),
                    "手册 22.3.3.6 说明 {} 为读清除位且本次读取会将其清除；"
                    "缺少覆盖区间的起点，就无法确定这些取值对应的时间范围，"
                    "也无法判断它们是否与观测到的其他状态属于同一时期。".format("、".join(present)),
                    basis=_BASIS_FLAGS,
                )
            )
        else:
            raised = [name for name in present if flag_value(observation.field(name)) == 1]
            unreadable = [
                name for name in present if flag_value(observation.field(name)) is None
            ]
            if unreadable:
                unsupported.append(_unreadable_item(target, "ESR", "、".join(unreadable)))
            states.append(
                ConfirmedState(
                    id="FC-ESR-FLAG-SNAPSHOT",
                    statement=_snapshot_statement(target, observation, raised, present),
                    evidence=(observation.observation_id,),
                    basis=_BASIS_FLAGS,
                )
            )

    if not observations:
        missing.append(
            MissingItem(
                "{} 的寄存器观测".format(target),
                "记录中没有 {} 的寄存器观测，本工具无法确认该外设的任何状态。"
                "缺少观测表示未知，不等于该外设正常，也不等于检测通过。".format(target),
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


def _snapshot_statement(
    target: str,
    observation: RegisterObservation,
    raised: List[str],
    present: List[str],
) -> str:
    """错误标志快照的结论。边界写在语句里：只覆盖已观测的区间与已观测的位。"""
    window = "{} 至该次读取之间".format(observation.covers_since)
    if raised:
        head = "在{}，{}.ESR 记录了以下错误条件：{}。".format(window, target, "、".join(raised))
    else:
        head = "在{}，{}.ESR 中已观测的错误标志位（{}）均未置位。".format(
            window, target, "、".join(present)
        )
    tail = (
        "这些位为读清除位，本次读取动作已将其清除，因此该快照只覆盖上述区间，"
        "不能说明当前是否仍在发生同类错误；本次未观测的其它位域仍为未知，"
        "本结论不代表该外设工作正常。"
    )
    return head + tail
