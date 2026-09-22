"""知识索引构建与查询测试。

覆盖分词与 MATCH 表达式构造、构建与重建、过滤与条数限制、无匹配，以及索引缺失、
损坏、版本不符三类异常。全部使用临时知识文件，不依赖工程内真实知识内容。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import List

import pytest
from knowledge_helpers import curated_data, registry_data, sample, write_json

from airbornediag.knowledge import (
    SCHEMA_VERSION,
    KnowledgeError,
    KnowledgeIndexError,
    build_index,
    build_match_expression,
    query_index,
    segment,
)

BUS_OFF_ENTRY = {
    "id": "T-FlexCAN2-001",
    "kind": "chip_knowledge",
    "title": "总线关闭状态",
    "body": "FLTCONF 为 1X 时模块处于总线关闭状态。",
    "locator": "22.3.3.6",
    "quote": "1X Bus off",
}

DSPI_ENTRY = {
    "id": "T-DSPI-001",
    "kind": "chip_knowledge",
    "title": "发送 FIFO 下溢标志 TFUF",
    "body": "TFUF 仅对工作在 SPI 从模式的 DSPI 检测。",
    "locator": "20.3.2.4",
}


# --- 分词 -----------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("总线关闭", "总 线 关 闭"),
        ("CANx_ESR", "CANx ESR"),
        ("FLTCONF 为 1X", "FLTCONF 为 1X"),
        ("22.3.3.6", "22 3 3 6"),
        ("错误被动，只听模式", "错 误 被 动 只 听 模 式"),
        ("   ", ""),
        ("", ""),
        ("DSPIx_SR 的 TCF 与 TXRXS", "DSPIx SR 的 TCF 与 TXRXS"),
    ],
)
def test_segment_splits_chinese_and_keeps_ascii_runs(text: str, expected: str) -> None:
    assert segment(text) == expected


def test_segmented_text_has_no_empty_tokens() -> None:
    for token in segment("（测试）：__A__ 1.5 中英混排 mixed").split():
        assert token.strip() == token
        assert token


# --- MATCH 表达式 ---------------------------------------------------------


def test_chinese_run_becomes_one_phrase() -> None:
    assert build_match_expression("总线关闭") == '"总 线 关 闭"'


def test_mixed_query_terms_are_combined_with_and() -> None:
    assert build_match_expression("TFUF 从模式") == '"TFUF" AND "从 模 式"'


def test_ascii_terms_are_quoted_separately() -> None:
    assert build_match_expression("CANx_ESR FLTCONF") == '"CANx" AND "ESR" AND "FLTCONF"'


def test_fts5_operators_in_query_cannot_break_the_expression() -> None:
    """查询里的 FTS5 语法字符被当作分隔符丢弃，不会造成语法错误。"""
    assert build_match_expression('") OR ("') == '"OR"'


def test_query_without_searchable_text_is_rejected() -> None:
    with pytest.raises(KnowledgeError):
        build_match_expression("###")


# --- 构建与查询 -----------------------------------------------------------


def test_build_and_query_roundtrip(tmp_path: Path) -> None:
    curated, registry = sample(tmp_path)
    db = tmp_path / "index.sqlite3"
    assert build_index(db_path=db, curated_dir=curated, registry_path=registry) == 1

    hits = query_index("总线关闭", db_path=db)
    assert [hit.entry_id for hit in hits] == ["T-FlexCAN2-001"]
    assert hits[0].locator == "22.3.3.6"
    assert hits[0].source_id == "MPC5554_RM"
    assert hits[0].quote == "1X Bus off"


def test_chinese_substring_query_matches(tmp_path: Path) -> None:
    """汉字按子串匹配：查询词不必与正文中的词边界对齐。"""
    curated, registry = sample(tmp_path)
    db = tmp_path / "index.sqlite3"
    build_index(db_path=db, curated_dir=curated, registry_path=registry)
    assert query_index("线关", db_path=db)


def test_ascii_query_ignores_case(tmp_path: Path) -> None:
    curated, registry = sample(tmp_path)
    db = tmp_path / "index.sqlite3"
    build_index(db_path=db, curated_dir=curated, registry_path=registry)
    assert query_index("fltconf", db_path=db)
    assert query_index("FLTCONF", db_path=db)


def test_terms_from_different_fields_must_all_match(tmp_path: Path) -> None:
    curated, registry = sample(tmp_path)
    db = tmp_path / "index.sqlite3"
    build_index(db_path=db, curated_dir=curated, registry_path=registry)
    # BOFFREC 只在正文里，1X 只在正文与原文里，两者都在，命中。
    assert query_index("BOFFREC 1X", db_path=db)
    # MSTR 不在该条目的任何字段中，整体不命中。
    assert query_index("BOFFREC MSTR", db_path=db) == []


def test_filters_restrict_results(tmp_path: Path) -> None:
    registry = write_json(tmp_path / "source-registry.json", registry_data())
    curated = tmp_path / "curated"
    write_json(curated / "flexcan2.json", curated_data([BUS_OFF_ENTRY]))
    write_json(curated / "dspi.json", curated_data([DSPI_ENTRY], peripheral="DSPI"))

    db = tmp_path / "index.sqlite3"
    assert build_index(db_path=db, curated_dir=curated, registry_path=registry) == 2

    assert {hit.entry_id for hit in query_index("FIFO", db_path=db)} == {"T-DSPI-001"}
    assert {hit.entry_id for hit in query_index("FIFO", db_path=db, peripheral="dspi")} == {
        "T-DSPI-001"
    }
    assert query_index("FIFO", db_path=db, peripheral="FlexCAN2") == []
    assert query_index("FIFO", db_path=db, chip="mpc5554")
    assert query_index("FIFO", db_path=db, chip="TMS320F28335") == []


def test_limit_caps_the_number_of_results(tmp_path: Path) -> None:
    entries: List[dict] = [
        dict(BUS_OFF_ENTRY, id="T-{:03d}".format(index), body="总线关闭状态说明 {}".format(index))
        for index in range(1, 5)
    ]
    curated, registry = sample(tmp_path, entries=entries)
    db = tmp_path / "index.sqlite3"
    build_index(db_path=db, curated_dir=curated, registry_path=registry)

    assert len(query_index("总线关闭", db_path=db, limit=2)) == 2
    assert len(query_index("总线关闭", db_path=db, limit=10)) == 4


def test_no_match_returns_empty_list(tmp_path: Path) -> None:
    curated, registry = sample(tmp_path)
    db = tmp_path / "index.sqlite3"
    build_index(db_path=db, curated_dir=curated, registry_path=registry)
    assert query_index("涡轮叶片疲劳", db_path=db) == []


def test_non_positive_limit_is_rejected(tmp_path: Path) -> None:
    curated, registry = sample(tmp_path)
    db = tmp_path / "index.sqlite3"
    build_index(db_path=db, curated_dir=curated, registry_path=registry)
    with pytest.raises(KnowledgeError):
        query_index("总线关闭", db_path=db, limit=0)


# --- 重建 -----------------------------------------------------------------


def test_rebuild_does_not_duplicate_entries(tmp_path: Path) -> None:
    curated, registry = sample(tmp_path)
    db = tmp_path / "index.sqlite3"
    for _ in range(3):
        assert build_index(db_path=db, curated_dir=curated, registry_path=registry) == 1

    connection = sqlite3.connect(str(db))
    try:
        total = connection.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
        distinct = connection.execute("SELECT COUNT(DISTINCT entry_id) FROM entries").fetchone()[0]
    finally:
        connection.close()
    assert (total, distinct) == (1, 1)
    assert len(query_index("总线关闭", db_path=db, limit=10)) == 1


def test_rebuild_picks_up_edited_knowledge(tmp_path: Path) -> None:
    curated, registry = sample(tmp_path)
    db = tmp_path / "index.sqlite3"
    build_index(db_path=db, curated_dir=curated, registry_path=registry)
    assert query_index("TFUF", db_path=db) == []

    write_json(curated / "sample.json", curated_data([DSPI_ENTRY], peripheral="DSPI"))
    build_index(db_path=db, curated_dir=curated, registry_path=registry)
    assert query_index("总线关闭", db_path=db) == []
    assert [hit.entry_id for hit in query_index("TFUF", db_path=db)] == ["T-DSPI-001"]


def test_build_creates_missing_directories(tmp_path: Path) -> None:
    curated, registry = sample(tmp_path)
    db = tmp_path / "深层" / "目录" / "index.sqlite3"
    build_index(db_path=db, curated_dir=curated, registry_path=registry)
    assert db.is_file()


def test_build_uses_indexed_search_text_not_raw_body(tmp_path: Path) -> None:
    """索引里存的是切好词的文本，正文按原样保存以便展示。"""
    curated, registry = sample(tmp_path, entries=[BUS_OFF_ENTRY])
    db = tmp_path / "index.sqlite3"
    build_index(db_path=db, curated_dir=curated, registry_path=registry)

    connection = sqlite3.connect(str(db))
    try:
        search_text, body = connection.execute(
            "SELECT search_text, body FROM entries"
        ).fetchone()
    finally:
        connection.close()
    assert "总 线 关 闭" in search_text
    assert body == BUS_OFF_ENTRY["body"]


# --- 索引异常 -------------------------------------------------------------


def test_missing_index_is_reported(tmp_path: Path) -> None:
    with pytest.raises(KnowledgeIndexError) as error:
        query_index("总线关闭", db_path=tmp_path / "没有这个索引.sqlite3")
    assert "请先运行 airbornediag kb build" in str(error.value)


def test_corrupt_index_is_reported(tmp_path: Path) -> None:
    db = tmp_path / "index.sqlite3"
    db.write_bytes(b"this is not a database")
    with pytest.raises(KnowledgeIndexError) as error:
        query_index("总线关闭", db_path=db)
    assert "无法读取" in str(error.value)


def test_corrupt_index_is_reported_on_build(tmp_path: Path) -> None:
    curated, registry = sample(tmp_path)
    db = tmp_path / "index.sqlite3"
    db.write_bytes(b"this is not a database")
    with pytest.raises(KnowledgeIndexError) as error:
        build_index(db_path=db, curated_dir=curated, registry_path=registry)
    assert "写入索引" in str(error.value) and "重新构建" in str(error.value)


def test_schema_version_mismatch_is_reported(tmp_path: Path) -> None:
    curated, registry = sample(tmp_path)
    db = tmp_path / "index.sqlite3"
    build_index(db_path=db, curated_dir=curated, registry_path=registry)

    connection = sqlite3.connect(str(db))
    try:
        connection.execute("UPDATE meta SET value = '0' WHERE key = 'schema_version'")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(KnowledgeIndexError) as error:
        query_index("总线关闭", db_path=db)
    assert "请重新构建索引" in str(error.value)


def test_built_index_records_schema_version(tmp_path: Path) -> None:
    curated, registry = sample(tmp_path)
    db = tmp_path / "index.sqlite3"
    build_index(db_path=db, curated_dir=curated, registry_path=registry)

    connection = sqlite3.connect(str(db))
    try:
        meta = dict(connection.execute("SELECT key, value FROM meta"))
    finally:
        connection.close()
    assert meta["schema_version"] == SCHEMA_VERSION
    assert meta["entry_count"] == "1"
    assert meta["built_at"]


def test_build_propagates_knowledge_data_errors(tmp_path: Path) -> None:
    curated, registry = sample(tmp_path, source_id="NOT_REGISTERED")
    with pytest.raises(KnowledgeError) as error:
        build_index(db_path=tmp_path / "index.sqlite3", curated_dir=curated, registry_path=registry)
    assert not isinstance(error.value, KnowledgeIndexError)


def test_query_does_not_create_the_index_file(tmp_path: Path) -> None:
    """查询以只读方式打开索引，不会顺手建出一个空文件。"""
    db = tmp_path / "index.sqlite3"
    with pytest.raises(KnowledgeIndexError):
        query_index("总线关闭", db_path=db)
    assert not db.exists()
