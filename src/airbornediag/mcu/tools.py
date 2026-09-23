"""按芯片与检测对象分派诊断工具，汇总为一次记录的结果。

分派是固定的：按芯片选专用模块，再按模块实例名选外设工具。本版不做跨外设关联，
也不产生候选根因——那属于后续编排与模型分析的环节。

记录中不属于本版支持范围的内容在这里集中报告：芯片、检测对象、以及观测形式。
不支持的项按不支持报告，不能当作正常。
"""

from __future__ import annotations

from typing import Any, Iterator, Mapping

from airbornediag.mcu import mpc5554
from airbornediag.mcu.model import DiagnosisResult, UnsupportedItem
from airbornediag.mcu.record import RecordView

# 可选用的芯片专用模块。新增芯片时在此登记。
CHIP_MODULES = (mpc5554,)

# 已被判据使用的观测种类。其余种类只报告为未使用。
USED_OBSERVATION_KINDS = ("register",)


def _chip_module(chip: str) -> Any:
    for module in CHIP_MODULES:
        if module.CHIP == chip:
            return module
    return None


def _record_unsupported(view: RecordView, module: Any) -> Iterator[UnsupportedItem]:
    """记录中不属于本版支持范围的内容。"""
    for target in view.targets():
        if not module.supports_target(target):
            yield UnsupportedItem(
                subject="检测对象 {}".format(target),
                reason=(
                    "本版 {} 的工具只处理 CAN_x、DSPI_x 形式的模块实例，该对象没有"
                    "对应判据，因此不参与判断。不支持表示本版没有依据，"
                    "不等于该对象正常。".format(module.CHIP)
                ),
            )

    raw_only = [
        observation.observation_id
        for observation in view.register_observations()
        if observation.has_raw_value and not observation.fields
    ]
    if raw_only:
        yield UnsupportedItem(
            subject="寄存器原始值（{}）".format("、".join(raw_only)),
            reason=(
                "本版不解析寄存器原始值：位编号尚未逐条核对手册入知识库，"
                "按位解释没有可核对的依据。只提供 value、没有已解码位域的寄存器观测"
                "不参与判断。"
            ),
        )

    malformed = view.unparsable_register_observations()
    if malformed:
        yield UnsupportedItem(
            subject="寄存器名不合契约的观测（{}）".format("、".join(malformed)),
            reason=(
                "register 名必须写成 <模块实例>.<寄存器名> 的形式，否则无法确定"
                "该寄存器属于哪个外设。本版不猜测其归属，因此这些观测不参与判断。"
            ),
        )

    unused_kinds = [
        kind for kind in view.observation_kinds() if kind not in USED_OBSERVATION_KINDS
    ]
    if unused_kinds:
        yield UnsupportedItem(
            subject="观测种类：{}".format("、".join(unused_kinds)),
            reason=(
                "本版没有使用这些观测的判据，因此它们不参与判断。"
                "这不表示这些观测所描述的情况不存在或正常。"
            ),
        )


def run_tools(record: Mapping[str, Any]) -> DiagnosisResult:
    """对一条故障记录运行本版全部诊断工具。"""
    view = RecordView(record)
    chip = view.chip
    module = _chip_module(chip)

    if module is None:
        known = "、".join(item.CHIP for item in CHIP_MODULES)
        return DiagnosisResult(
            record_id=view.record_id,
            chip=chip,
            unsupported=(
                UnsupportedItem(
                    subject="芯片 {}".format(chip) if chip else "芯片型号（缺省）",
                    reason=(
                        "本版只有 {} 的诊断工具，没有该芯片的判据，因此不给出任何结论。"
                        "不同 MCU 的同名外设、寄存器与状态定义不能直接混用，"
                        "本工具不会借用其他芯片的定义。".format(known)
                    ),
                ),
            ),
        )

    tool_results = tuple(module.analyse(view))
    return DiagnosisResult(
        record_id=view.record_id,
        chip=chip,
        tool_results=tool_results,
        unsupported=tuple(_record_unsupported(view, module)),
    )
