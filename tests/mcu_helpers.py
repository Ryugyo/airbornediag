"""诊断工具测试共用的最小故障记录构造工具。

这里只拼装记录字典，不经过 Schema 校验，便于直接构造边界情形（缺字段、取值冲突、
不合契约的寄存器名等）。Schema 校验另由命令行测试覆盖。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

SCHEMA_VERSION = "0.1.0"


def record(
    *,
    chip: str = "MPC5554",
    target: str = "CAN_A",
    result: str = "fail",
    observations: Sequence[Dict[str, Any]] = (),
    record_id: str = "REC-TEST-001",
    test_id: str = "TEST-001",
    origin: str = "simulated",
) -> Dict[str, Any]:
    """拼一条故障记录。observations 缺省表示未采集。"""
    data: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "record_id": record_id,
        "origin": origin,
        "device": {"model": chip},
        "test": {"id": test_id, "target": target, "result": result},
    }
    if observations:
        data["observations"] = list(observations)
    return data


def register(
    observation_id: str,
    register: str,
    fields: Optional[Dict[str, Any]] = None,
    *,
    target: str = "CAN_A",
    value: Optional[int] = None,
    semantics: Optional[str] = None,
    covers_since: Optional[str] = None,
    at: Optional[str] = None,
    capture_id: Optional[str] = None,
) -> Dict[str, Any]:
    """拼一条 register 观测。fields 与 value 互斥，两者都不给时由 Schema 判为不合规。"""
    item: Dict[str, Any] = {
        "id": observation_id,
        "kind": "register",
        "target": target,
        "register": register,
    }
    if fields is not None:
        item["fields"] = dict(fields)
    if value is not None:
        item["value"] = value
    if semantics is not None:
        item["semantics"] = semantics
    if covers_since is not None:
        item["covers_since"] = covers_since
    if at is not None:
        item["at"] = at
    if capture_id is not None:
        item["capture_id"] = capture_id
    return item


def other_observation(
    observation_id: str, kind: str, *, target: str = "CAN_A", **extra: Any
) -> Dict[str, Any]:
    """拼一条非 register 观测，字段由调用方补齐。"""
    item: Dict[str, Any] = {
        "id": observation_id,
        "kind": kind,
        "target": target,
    }
    item.update(extra)
    return item


def write_record(path: Path, data: Dict[str, Any]) -> Path:
    """把记录写成 UTF-8 的 JSON 文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def state_ids(result: Any) -> list:
    return [state.id for state in result.confirmed_states]


def conflict_ids(result: Any) -> list:
    return [item.id for item in result.inconsistencies]


def missing_texts(result: Any) -> str:
    """把所有缺失信息拼成一段文本，便于按措辞断言。"""
    return "\n".join("{}\n{}".format(item.missing, item.reason) for item in result.insufficient_data)


def unsupported_texts(result: Any) -> str:
    return "\n".join(
        "{}\n{}".format(item.subject, item.reason) for item in result.unsupported
    )
