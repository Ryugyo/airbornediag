"""知识文件读取与出处校验测试。

覆盖三类：真实知识文件可用、文件级与条目级格式错误被拒绝、出处与来源登记表的
一致性检查确实生效。校验失败一律抛 KnowledgeDataError，不静默取默认值。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import pytest
from knowledge_helpers import SOURCES, curated_data, registry_data, sample, write_json

from airbornediag.knowledge import (
    CURATED_VERSION,
    ENTRY_KINDS,
    KnowledgeDataError,
    load_curated,
    load_registry_sources,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
CURATED_DIR = REPO_ROOT / "knowledge" / "curated"
REGISTRY = REPO_ROOT / "knowledge" / "source-registry.json"


# --- 真实知识文件 ---------------------------------------------------------


def test_real_knowledge_files_load() -> None:
    """工程内的知识文件必须能读通，且条目 id 唯一。"""
    entries = load_curated(CURATED_DIR, REGISTRY)
    assert entries
    assert len({entry.entry_id for entry in entries}) == len(entries)


def test_real_entries_carry_chip_peripheral_and_registered_source() -> None:
    entries = load_curated(CURATED_DIR, REGISTRY)
    registry = load_registry_sources(REGISTRY)
    for entry in entries:
        assert entry.chip == "MPC5554"
        assert entry.peripheral in ("FlexCAN2", "DSPI")
        assert entry.source_id in registry
        assert entry.kind in ENTRY_KINDS
        # 出处必须可核对：定位非空，且不会退化成页码。
        assert entry.locator.strip()
        assert entry.origin.startswith(entry.source_id)


def test_real_entries_cover_both_peripherals() -> None:
    peripherals = {entry.peripheral for entry in load_curated(CURATED_DIR, REGISTRY)}
    assert peripherals == {"FlexCAN2", "DSPI"}


def test_knowledge_files_do_not_cite_expected_reports() -> None:
    """模拟案例的预期报告不是知识依据，知识条目里不应出现它们的编号。"""
    text = "".join(
        path.read_text(encoding="utf-8") for path in sorted(CURATED_DIR.glob("*.json"))
    )
    assert "RPT-2026" not in text
    assert "REC-2026" not in text


# --- 知识目录与文件格式 ---------------------------------------------------


def test_missing_directory_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(KnowledgeDataError) as error:
        load_curated(tmp_path / "不存在", None)
    assert "知识目录不存在" in str(error.value)


def test_directory_without_knowledge_files_is_rejected(tmp_path: Path) -> None:
    empty = tmp_path / "curated"
    empty.mkdir()
    with pytest.raises(KnowledgeDataError) as error:
        load_curated(empty, None)
    assert "没有 .json 知识文件" in str(error.value)


def test_invalid_json_is_rejected(tmp_path: Path) -> None:
    curated = tmp_path / "curated"
    curated.mkdir()
    (curated / "broken.json").write_text("{不是 JSON", encoding="utf-8")
    with pytest.raises(KnowledgeDataError) as error:
        load_curated(curated, None)
    assert "不是合法 JSON" in str(error.value)


def test_top_level_must_be_an_object(tmp_path: Path) -> None:
    curated = tmp_path / "curated"
    write_json(curated / "list.json", [1, 2, 3])
    with pytest.raises(KnowledgeDataError) as error:
        load_curated(curated, None)
    assert "顶层必须是对象" in str(error.value)


@pytest.mark.parametrize(
    "missing", ["curated_version", "chip", "peripheral", "source_id", "entries"]
)
def test_missing_file_field_is_rejected(tmp_path: Path, missing: str) -> None:
    data = curated_data()
    del data[missing]
    curated = tmp_path / "curated"
    write_json(curated / "sample.json", data)
    with pytest.raises(KnowledgeDataError) as error:
        load_curated(curated, None)
    assert missing in str(error.value)


def test_unknown_file_field_is_rejected(tmp_path: Path) -> None:
    curated = tmp_path / "curated"
    write_json(curated / "sample.json", curated_data(外设="DSPI"))
    with pytest.raises(KnowledgeDataError) as error:
        load_curated(curated, None)
    assert "未定义字段" in str(error.value)


def test_curated_version_mismatch_is_rejected(tmp_path: Path) -> None:
    curated, _ = sample(tmp_path, curated_version="9.9.9")
    with pytest.raises(KnowledgeDataError) as error:
        load_curated(curated, None)
    assert CURATED_VERSION in str(error.value)


def test_empty_entries_are_rejected(tmp_path: Path) -> None:
    curated, _ = sample(tmp_path, entries=[])
    with pytest.raises(KnowledgeDataError) as error:
        load_curated(curated, None)
    assert "非空数组" in str(error.value)


# --- 条目格式 -------------------------------------------------------------


@pytest.mark.parametrize("missing", ["id", "kind", "title", "body", "locator"])
def test_missing_entry_field_is_rejected(tmp_path: Path, missing: str) -> None:
    entry: Dict[str, Any] = dict(curated_data()["entries"][0])
    del entry[missing]
    curated, _ = sample(tmp_path, entries=[entry])
    with pytest.raises(KnowledgeDataError) as error:
        load_curated(curated, None)
    assert missing in str(error.value)


def test_unknown_entry_field_is_rejected(tmp_path: Path) -> None:
    entry = dict(curated_data()["entries"][0])
    entry["bodyy"] = "拼错的字段"
    curated, _ = sample(tmp_path, entries=[entry])
    with pytest.raises(KnowledgeDataError) as error:
        load_curated(curated, None)
    assert "bodyy" in str(error.value)


def test_unknown_kind_is_rejected(tmp_path: Path) -> None:
    entry = dict(curated_data()["entries"][0])
    entry["kind"] = "fault_rule"
    curated, _ = sample(tmp_path, entries=[entry])
    with pytest.raises(KnowledgeDataError) as error:
        load_curated(curated, None)
    assert "fault_rule" in str(error.value)


@pytest.mark.parametrize("bad_id", ["", "   ", "带空格 的 id", "-以连字符开头"])
def test_invalid_entry_id_is_rejected(tmp_path: Path, bad_id: str) -> None:
    entry = dict(curated_data()["entries"][0])
    entry["id"] = bad_id
    curated, _ = sample(tmp_path, entries=[entry])
    with pytest.raises(KnowledgeDataError):
        load_curated(curated, None)


def test_duplicate_entry_id_across_files_is_rejected(tmp_path: Path) -> None:
    registry = write_json(tmp_path / "source-registry.json", registry_data())
    curated = tmp_path / "curated"
    write_json(curated / "a.json", curated_data())
    write_json(curated / "b.json", curated_data(peripheral="DSPI"))
    with pytest.raises(KnowledgeDataError) as error:
        load_curated(curated, registry)
    assert "重复" in str(error.value)


def test_entry_without_quote_is_allowed(tmp_path: Path) -> None:
    entry = dict(curated_data()["entries"][0])
    del entry["quote"]
    curated, registry = sample(tmp_path, entries=[entry])
    entries = load_curated(curated, registry)
    assert entries[0].quote is None


def test_empty_quote_is_rejected(tmp_path: Path) -> None:
    entry = dict(curated_data()["entries"][0])
    entry["quote"] = "  "
    curated, _ = sample(tmp_path, entries=[entry])
    with pytest.raises(KnowledgeDataError) as error:
        load_curated(curated, None)
    assert "quote" in str(error.value)


# --- 出处校验 -------------------------------------------------------------


def test_unregistered_source_is_rejected(tmp_path: Path) -> None:
    curated, registry = sample(tmp_path, source_id="NOT_REGISTERED")
    with pytest.raises(KnowledgeDataError) as error:
        load_curated(curated, registry)
    assert "NOT_REGISTERED" in str(error.value)
    assert "未在来源登记表中登记" in str(error.value)


def test_chip_outside_source_scope_is_rejected(tmp_path: Path) -> None:
    """不允许把来源未覆盖的芯片写成该来源的知识。"""
    curated, registry = sample(tmp_path, chip="TMS320F28335")
    with pytest.raises(KnowledgeDataError) as error:
        load_curated(curated, registry)
    assert "不在来源" in str(error.value)


def test_source_scope_check_is_skipped_without_registry(tmp_path: Path) -> None:
    """不传登记表时跳过出处校验，只校验格式；正常调用不应这样做。"""
    curated, _ = sample(tmp_path, chip="TMS320F28335")
    assert load_curated(curated, None)[0].chip == "TMS320F28335"


# --- 来源登记表 -----------------------------------------------------------


def test_registry_requires_sources(tmp_path: Path) -> None:
    path = write_json(tmp_path / "registry.json", {"registry_version": "0.1.0"})
    with pytest.raises(KnowledgeDataError) as error:
        load_registry_sources(path)
    assert "sources" in str(error.value)


def test_registry_requires_source_id(tmp_path: Path) -> None:
    path = write_json(tmp_path / "registry.json", registry_data([{"title": "缺少 id"}]))
    with pytest.raises(KnowledgeDataError) as error:
        load_registry_sources(path)
    assert "id" in str(error.value)


def test_registry_rejects_duplicate_ids(tmp_path: Path) -> None:
    duplicated: List[Dict[str, Any]] = [SOURCES[0], dict(SOURCES[0])]
    path = write_json(tmp_path / "registry.json", registry_data(duplicated))
    with pytest.raises(KnowledgeDataError) as error:
        load_registry_sources(path)
    assert "重复" in str(error.value)


def test_missing_registry_is_reported(tmp_path: Path) -> None:
    curated, _ = sample(tmp_path)
    with pytest.raises(KnowledgeDataError) as error:
        load_curated(curated, tmp_path / "没有这个文件.json")
    assert "来源登记表不存在" in str(error.value)
