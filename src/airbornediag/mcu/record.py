"""故障记录的只读访问。

诊断工具不直接索引记录字典，统一经本模块读取，目的是把契约中「字段缺省即为未知」
这一条约定收在一处：取不到的字段一律返回 None，绝不补零、补正常，也不把不同采集
时刻的数据合并。

本模块只做读取，不判断记录是否合规。合规性由 schemas/ 下的 JSON Schema 校验，
命令行入口在诊断之前先执行该校验。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Mapping, Optional, Tuple

from airbornediag.mcu.model import McuRecordError

# register 观测的字段名形如 CAN_A.ESR，模块实例与寄存器名以点分隔。
REGISTER_SEPARATOR = "."


def flag_value(raw: Any) -> Optional[int]:
    """把标志位取值规整为 0 或 1；无法解释时返回 None（表示未知）。"""
    if isinstance(raw, bool):
        return int(raw)
    if isinstance(raw, int) and raw in (0, 1):
        return raw
    if isinstance(raw, str) and raw.strip() in ("0", "1"):
        return int(raw.strip())
    return None


@dataclass(frozen=True)
class RegisterObservation:
    """一条 register 观测。"""

    observation_id: str
    module: str
    register: str
    register_name: str
    at: Optional[str]
    semantics: Optional[str]
    covers_since: Optional[str]
    capture_id: Optional[str]
    fields: Mapping[str, Any]
    has_raw_value: bool

    def has_field(self, name: str) -> bool:
        return name in self.fields

    def field(self, name: str) -> Any:
        """位域取值。字段缺省返回 None，表示未知。"""
        return self.fields.get(name)


def _text(container: Mapping[str, Any], key: str) -> Optional[str]:
    """取出可缺省的非空字符串字段。缺省或类型不符时返回 None。"""
    value = container.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _split_register(register: str) -> Tuple[Optional[str], Optional[str]]:
    """把 CAN_A.ESR 拆成模块实例与寄存器名。不含分隔符时返回 (None, None)。"""
    if REGISTER_SEPARATOR not in register:
        return None, None
    module, _, name = register.partition(REGISTER_SEPARATOR)
    module = module.strip()
    name = name.strip()
    if not module or not name or REGISTER_SEPARATOR in name:
        return None, None
    return module, name


@dataclass(frozen=True)
class FieldReading:
    """同一字段在多次观测中的归并结果。

    同一字段被多次观测时，只有取值一致才能合并成一个结论：取值不同说明它在观测
    区间内发生了变化，属于不同时刻的事实，本版没有可判定「当前」取值的时间基准。
    """

    value: Any = None
    # 取值可用的观测 id。
    evidence: Tuple[str, ...] = ()
    # (观测 id, 归并后的取值)，用于说明取值不一致。
    readings: Tuple[Tuple[str, Any], ...] = ()
    # 取值无法按手册解释的观测 id。
    unreadable: Tuple[str, ...] = ()

    @property
    def observed(self) -> bool:
        """该字段是否至少被观测过一次，无论取值是否可用。"""
        return bool(self.readings or self.unreadable)

    @property
    def conflicting(self) -> bool:
        return self.value is None and len({value for _, value in self.readings}) > 1

    def describe(self) -> str:
        """把多次观测的取值排成「OBS-1 为 x、OBS-2 为 y」，用于说明取值不一致。"""
        return "、".join(
            "{} 为 {}".format(observation_id, value) for observation_id, value in self.readings
        )


def field_pairs(
    observations: Tuple[RegisterObservation, ...],
    field_name: str,
    register_name: Optional[str] = None,
) -> List[Tuple[RegisterObservation, Any]]:
    """收集某位域在各观测中的原始取值。

    ``register_name`` 为 None 时匹配任意寄存器。字段缺省的观测不计入：缺省表示未知。
    """
    pairs: List[Tuple[RegisterObservation, Any]] = []
    for observation in observations:
        if register_name is not None and observation.register_name != register_name:
            continue
        if not observation.has_field(field_name):
            continue
        pairs.append((observation, observation.field(field_name)))
    return pairs


def resolve_field(
    pairs: List[Tuple[RegisterObservation, Any]],
    mapper: Any,
) -> FieldReading:
    """把同一字段的多次观测归并成一个可用取值。

    ``mapper`` 负责把原始取值转成可判定的写法，返回 None 表示无法解释该取值。
    """
    readings: List[Tuple[str, Any]] = []
    unreadable: List[str] = []
    for observation, raw in pairs:
        mapped = mapper(raw)
        if mapped is None:
            unreadable.append(observation.observation_id)
        else:
            readings.append((observation.observation_id, mapped))

    if not readings:
        return FieldReading(unreadable=tuple(unreadable))

    if len({value for _, value in readings}) > 1:
        return FieldReading(readings=tuple(readings), unreadable=tuple(unreadable))

    return FieldReading(
        value=readings[0][1],
        evidence=tuple(item[0] for item in readings),
        readings=tuple(readings),
        unreadable=tuple(unreadable),
    )


class RecordView:
    """故障记录的只读视图。"""

    def __init__(self, record: Mapping[str, Any]) -> None:
        if not isinstance(record, Mapping):
            raise McuRecordError("故障记录必须是 JSON 对象。")
        self._record = record

    @property
    def raw(self) -> Mapping[str, Any]:
        return self._record

    @property
    def record_id(self) -> str:
        return _text(self._record, "record_id") or ""

    @property
    def origin(self) -> Optional[str]:
        """记录来源。模拟案例与真实设备记录据此区分。"""
        return _text(self._record, "origin")

    @property
    def chip(self) -> str:
        """芯片型号。契约要求 device.model 必填。"""
        device = self._record.get("device")
        if not isinstance(device, Mapping):
            return ""
        return _text(device, "model") or ""

    @property
    def test(self) -> Mapping[str, Any]:
        test = self._record.get("test")
        return test if isinstance(test, Mapping) else {}

    @property
    def test_id(self) -> Optional[str]:
        """检测标识。"""
        return _text(self.test, "id")

    @property
    def test_result(self) -> Optional[str]:
        """检测结论 pass / fail。未执行的检测不产生记录。"""
        return _text(self.test, "result")

    @property
    def test_target(self) -> Optional[str]:
        """检测对象，通常为模块实例。"""
        return _text(self.test, "target")

    @property
    def observations(self) -> List[Mapping[str, Any]]:
        items = self._record.get("observations")
        if not isinstance(items, list):
            # 缺省与空数组含义相同：未采集。
            return []
        return [item for item in items if isinstance(item, Mapping)]

    def register_observations(self) -> Tuple[RegisterObservation, ...]:
        """全部可用的 register 观测。

        register 名不合契约（缺少模块实例前缀）的观测不在此返回，由 tools 层报告为
        不支持：没有模块实例就无法确定它属于哪个外设，不能猜。
        """
        found: List[RegisterObservation] = []
        for item in self.observations:
            if item.get("kind") != "register":
                continue
            register = _text(item, "register")
            if register is None:
                continue
            module, name = _split_register(register)
            if module is None or name is None:
                continue
            fields = item.get("fields")
            found.append(
                RegisterObservation(
                    observation_id=_text(item, "id") or "",
                    module=module,
                    register=register,
                    register_name=name,
                    at=_text(item, "at"),
                    semantics=_text(item, "semantics"),
                    covers_since=_text(item, "covers_since"),
                    capture_id=_text(item, "capture_id"),
                    fields=dict(fields) if isinstance(fields, Mapping) else {},
                    has_raw_value="value" in item,
                )
            )
        return tuple(found)

    def register_observations_for(self, module: str) -> Tuple[RegisterObservation, ...]:
        """指定模块实例的 register 观测，按记录中的顺序返回。"""
        return tuple(
            item for item in self.register_observations() if item.module == module
        )

    def unparsable_register_observations(self) -> Tuple[str, ...]:
        """register 名不含模块实例前缀的观测 id。

        没有模块实例就无法判断它属于哪个外设，不能猜；这类观测由调用方报告为不支持。
        """
        found: List[str] = []
        for item in self.observations:
            if item.get("kind") != "register":
                continue
            register = _text(item, "register")
            if register is None:
                continue
            module, name = _split_register(register)
            if module is None or name is None:
                found.append(_text(item, "id") or register)
        return tuple(found)

    def observations_of_kind(self, kind: str) -> Tuple[Mapping[str, Any], ...]:
        return tuple(item for item in self.observations if item.get("kind") == kind)

    def observation_kinds(self) -> Tuple[str, ...]:
        """记录中出现的观测种类，按首次出现顺序去重。"""
        kinds: List[str] = []
        for item in self.observations:
            kind = _text(item, "kind")
            if kind and kind not in kinds:
                kinds.append(kind)
        return tuple(kinds)

    def targets(self) -> Tuple[str, ...]:
        """记录涉及的模块实例：检测对象在前，其余按观测出现顺序补充。"""
        names: List[str] = []
        target = self.test_target
        if target:
            names.append(target)
        for item in self.register_observations():
            if item.module not in names:
                names.append(item.module)
        return tuple(names)
