"""MCU 片上资源与外设的诊断工具。

按芯片型号分目录组织：``mcu/mpc5554/`` 下每个外设一个模块，模块内是普通函数。
工具只做有依据的寄存器解码、状态解释与判据计算，产出诊断流程的中间结果；候选根因
与最终报告由后续环节结合知识检索与模型分析给出。

工具可以脱离模型服务独立运行：输入的故障记录由 JSON Schema 校验，知识依据以
文献出处与知识条目 id 的形式写在规则里。
"""

from airbornediag.mcu.model import (
    Basis,
    ConfirmedState,
    DiagnosisResult,
    Inconsistency,
    McuError,
    McuRecordError,
    MissingItem,
    ToolResult,
    UnsupportedItem,
    to_jsonable,
    unique_in_order,
)
from airbornediag.mcu.record import (
    FieldReading,
    RecordView,
    RegisterObservation,
    field_pairs,
    flag_value,
    resolve_field,
)
from airbornediag.mcu.tools import CHIP_MODULES, run_tools

__all__ = [
    "Basis",
    "CHIP_MODULES",
    "ConfirmedState",
    "DiagnosisResult",
    "FieldReading",
    "Inconsistency",
    "McuError",
    "McuRecordError",
    "MissingItem",
    "RecordView",
    "RegisterObservation",
    "ToolResult",
    "UnsupportedItem",
    "field_pairs",
    "flag_value",
    "resolve_field",
    "run_tools",
    "to_jsonable",
    "unique_in_order",
]
