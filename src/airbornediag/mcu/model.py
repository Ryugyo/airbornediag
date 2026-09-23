"""MCU 诊断工具的结果模型。

工具结果是诊断流程的中间产物，不是完整诊断报告：它记录规则能够确认的状态、输入中
的自相矛盾、因信息缺失而无法判定的内容，以及本版不支持的范围。候选根因由后续流程
结合知识检索与模型分析给出，工具结果中不出现。

每条结论都带观测证据、文献出处，并列出它在知识库中对应的条目 id：规则与知识依据
由此关联，避免同一判据在代码与知识文件里各维护一份。

缺失的信息单独成项。缺失表示未知，不表示正常，也不表示检测通过。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, List, Optional, Tuple


class McuError(Exception):
    """MCU 诊断工具失败。"""


class McuRecordError(McuError):
    """故障记录的结构无法按契约读取。"""


@dataclass(frozen=True)
class Basis:
    """一条结论的依据：报告引用的文献出处，以及对应的知识条目 id。"""

    source: str
    locator: str
    knowledge_refs: Tuple[str, ...] = ()

    def __str__(self) -> str:
        return "{} {}".format(self.source, self.locator)


@dataclass(frozen=True)
class ConfirmedState:
    """规则确认的状态。只有前提已被观测且成立时才产生。"""

    id: str
    statement: str
    evidence: Tuple[str, ...]
    basis: Basis


@dataclass(frozen=True)
class Inconsistency:
    """输入中自相矛盾之处。"""

    id: str
    statement: str
    evidence: Tuple[str, ...]
    basis: Basis


@dataclass(frozen=True)
class MissingItem:
    """缺失的信息及其导致的不可判定。

    ``basis`` 是判定「缺少它就无法下结论」所依据的规则出处，例如「该位是读清除位，
    没有覆盖区间起点就无法确定它反映哪段时间」。没有可引用的规则时为 None——例如
    记录里根本没有观测，此时不存在被应用的判据。
    """

    missing: str
    reason: str
    basis: Optional[Basis] = None


@dataclass(frozen=True)
class UnsupportedItem:
    """本版不支持的对象及原因。不支持的项不能当作正常。"""

    subject: str
    reason: str


@dataclass(frozen=True)
class ToolResult:
    """一个外设工具对一个检测对象给出的全部结果。"""

    tool: str
    chip: str
    peripheral: str
    target: str
    # 本次实际使用的字段，形如 CAN_A.ESR.FLTCONF。未列出的字段表示未观测，或本版没有
    # 该位域的判据（后者作为不支持项单独列出）。
    used_fields: Tuple[str, ...] = ()
    confirmed_states: Tuple[ConfirmedState, ...] = ()
    inconsistencies: Tuple[Inconsistency, ...] = ()
    insufficient_data: Tuple[MissingItem, ...] = ()
    unsupported: Tuple[UnsupportedItem, ...] = ()

    @property
    def is_empty(self) -> bool:
        """本次没有产生任何结论、矛盾、缺失或不支持说明。"""
        return not (
            self.confirmed_states
            or self.inconsistencies
            or self.insufficient_data
            or self.unsupported
        )


@dataclass(frozen=True)
class DiagnosisResult:
    """一次记录的全部工具结果。"""

    record_id: str
    chip: str
    tool_results: Tuple[ToolResult, ...] = ()
    unsupported: Tuple[UnsupportedItem, ...] = ()

    @property
    def supported(self) -> bool:
        """本次是否有工具处理了该记录。

        芯片不支持、或记录中没有任何本版支持的检测对象时均为 False。此时不能把
        结果当作「未发现异常」。
        """
        return bool(self.tool_results)

    def counts(self) -> dict:
        """各分类的条数，供命令行打印汇总。"""
        return {
            "对象": len(self.tool_results),
            "确认状态": sum(len(item.confirmed_states) for item in self.tool_results),
            "矛盾": sum(len(item.inconsistencies) for item in self.tool_results),
            "缺失信息": sum(len(item.insufficient_data) for item in self.tool_results),
            "不支持": len(self.unsupported)
            + sum(len(item.unsupported) for item in self.tool_results),
        }


def to_jsonable(result: DiagnosisResult) -> Any:
    """把结果转成可直接 json.dumps 的结构。

    ``supported`` 是派生量，单独写在最前面：只看结论列表的话，「本记录没有任何工具
    处理过」与「跑过工具但没得出状态」是同一个形状，容易被读成未发现异常。
    """
    payload = asdict(result)
    return dict(supported=result.supported, **payload)


def unique_in_order(values: List[str]) -> Tuple[str, ...]:
    """按出现顺序去重。"""
    seen: List[str] = []
    for value in values:
        if value not in seen:
            seen.append(value)
    return tuple(seen)
