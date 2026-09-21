"""知识库测试共用的最小知识文件构造工具。

真实知识文件放在 knowledge/curated/，测试不依赖它们的具体内容，只用这里构造的
最小样本验证读取、校验与索引行为；另有一组检索效果测试直接使用真实知识文件。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

SOURCES = [
    {
        "id": "MPC5554_RM",
        "title": "测试用参考手册",
        "order_number": "TEST_RM",
        "revision": "Rev. 1",
        "applies_to": ["MPC5553", "MPC5554"],
    }
]

DEFAULT_ENTRY = {
    "id": "T-FlexCAN2-001",
    "kind": "chip_knowledge",
    "title": "总线关闭状态",
    "body": "FLTCONF 为 1X 时模块处于总线关闭状态，BOFFREC 决定是否自动恢复。",
    "locator": "22.3.3.6",
    "quote": "1X Bus off",
}


def write_json(path: Path, data: Any) -> Path:
    """写出一个 UTF-8 的 JSON 文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def registry_data(sources: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    return {
        "registry_version": "0.1.0",
        "sources": SOURCES if sources is None else sources,
    }


def curated_data(
    entries: Optional[List[Dict[str, Any]]] = None, **overrides: Any
) -> Dict[str, Any]:
    data: Dict[str, Any] = {
        "curated_version": "0.1.0",
        "chip": "MPC5554",
        "peripheral": "FlexCAN2",
        "source_id": "MPC5554_RM",
        "entries": [dict(DEFAULT_ENTRY)] if entries is None else entries,
    }
    data.update(overrides)
    return data


def sample(tmp_path: Path, entries: Optional[List[Dict[str, Any]]] = None, **overrides: Any):
    """建立一套最小可用的知识目录与来源登记表，返回 (知识目录, 登记表) 路径。"""
    registry = write_json(tmp_path / "source-registry.json", registry_data())
    curated = tmp_path / "curated"
    write_json(curated / "sample.json", curated_data(entries, **overrides))
    return curated, registry
