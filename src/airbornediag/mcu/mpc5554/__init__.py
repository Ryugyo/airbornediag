"""MPC5554 专用诊断工具。

一个外设一个模块，模块内是普通函数：输入是故障记录的只读视图与一个模块实例名，
输出是一个 ToolResult。此处不建设通用规则引擎——规则数量增长到确实需要共享结构时
再考虑。TMS320F28335 目录为空：knowledge/sources/ 中尚无其资料，不能借用 MPC5554
的同名外设定义。
"""

from __future__ import annotations

from typing import List

from airbornediag.mcu.model import ToolResult
from airbornediag.mcu.mpc5554 import dspi, flexcan2
from airbornediag.mcu.record import RecordView

CHIP = "MPC5554"

# 本芯片可用的外设工具。新增外设时在此登记，分派逻辑不用改。
TOOLS = (flexcan2, dspi)

# 手册确认存在的模块实例。各工具按手册清单声明，这里汇总用于报告支持范围：
# 记录里出现清单外的实例名时按不支持报告，不假定该实例存在。
SUPPORTED_INSTANCES = tuple(name for tool in TOOLS for name in tool.INSTANCES)


def supports_target(name: str) -> bool:
    """该模块实例是否由本芯片的某个工具处理。"""
    return any(tool.matches_target(name) for tool in TOOLS)


def analyse(view: RecordView) -> List[ToolResult]:
    """对记录涉及的每个模块实例各跑一次对应工具。"""
    results: List[ToolResult] = []
    for target in view.targets():
        for tool in TOOLS:
            if tool.matches_target(target):
                results.append(tool.analyse_target(view, target))
                break
    return results


__all__ = [
    "CHIP",
    "SUPPORTED_INSTANCES",
    "TOOLS",
    "analyse",
    "dspi",
    "flexcan2",
    "supports_target",
]
