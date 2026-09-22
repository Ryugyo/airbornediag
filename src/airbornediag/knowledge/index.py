"""知识全文索引的构建与查询。

索引使用标准库 ``sqlite3`` 的 FTS5，不引入第三方依赖，也不引入向量模型；板端
SQLite 3.37.2 已确认支持 FTS5。

中文处理：FTS5 默认的 ``unicode61`` 分词器把一整串连续汉字当成一个词，``总线关闭``
这样的查询无法命中 ``模块处于总线关闭状态``。本模块在写入索引前把文本切成以空格
分隔的词元——每个汉字一个词元、每段连续的 ASCII 字母数字一个词元——查询时用同样
的方式切分并以短语提交。于是中文按子串匹配、英文寄存器名按词元匹配，两者可以在
同一个查询里混用。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

from airbornediag.knowledge.model import (
    CURATED_VERSION,
    DEFAULT_CURATED_DIR,
    DEFAULT_REGISTRY_PATH,
    KnowledgeEntry,
    KnowledgeError,
    KnowledgeIndexError,
    load_curated,
)

# 索引格式版本。表结构或分词方式变化时递增，查询时据此发现过期索引。
SCHEMA_VERSION = "1"

DEFAULT_INDEX_PATH = Path("knowledge/index/knowledge.sqlite3")
DEFAULT_LIMIT = 5

# 汉字范围：扩展 A、基本区、兼容区、扩展 B 及以上。
_CJK_RANGES = ((0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xF900, 0xFAFF), (0x20000, 0x2FA1F))

_SEARCHED_FIELDS = ("entry_id", "title", "body", "locator", "quote")

_CREATE_ENTRIES_SQL = """
CREATE VIRTUAL TABLE entries USING fts5(
    search_text,
    entry_id UNINDEXED,
    chip UNINDEXED,
    peripheral UNINDEXED,
    kind UNINDEXED,
    title UNINDEXED,
    body UNINDEXED,
    locator UNINDEXED,
    source_id UNINDEXED,
    source_quote UNINDEXED,
    tokenize = 'unicode61'
)
"""

_INSERT_SQL = """
INSERT INTO entries
    (search_text, entry_id, chip, peripheral, kind, title, body, locator, source_id, source_quote)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_SELECT_SQL = """
SELECT entry_id, chip, peripheral, kind, title, body, locator, source_id, source_quote
FROM entries
WHERE entries MATCH ?
"""


def _is_cjk(character: str) -> bool:
    """判断是否为汉字。"""
    code = ord(character)
    return any(start <= code <= end for start, end in _CJK_RANGES)


def segment(text: str) -> str:
    """把文本切成以空格分隔的检索词元。

    汉字逐字成词元，ASCII 字母数字连续段成词元，其余字符（标点、下划线、小数点、
    空白）都作为分隔符丢弃。例如 ``CANx_ESR 的 FLTCONF`` 切成
    ``CANx ESR 的 FLTCONF``。
    """
    tokens: List[str] = []
    buffer: List[str] = []
    for character in text:
        if _is_cjk(character):
            if buffer:
                tokens.append("".join(buffer))
                buffer = []
            tokens.append(character)
        elif character.isascii() and character.isalnum():
            buffer.append(character)
        elif buffer:
            tokens.append("".join(buffer))
            buffer = []
    if buffer:
        tokens.append("".join(buffer))
    return " ".join(tokens)


def build_match_expression(query: str) -> str:
    """把用户查询转成 FTS5 的 MATCH 表达式。

    连续汉字合成一个短语，其余词元各自成为一个短语，短语之间用 AND 连接，表示
    「都要出现」。词元只含汉字或 ASCII 字母数字，加引号后不可能引入 FTS5 的语法
    字符，因此不需要额外转义，也不会因为查询里带标点而报语法错误。
    """
    tokens = segment(query).split()
    if not tokens:
        raise KnowledgeError("查询内容中没有可检索的文字：{!r}".format(query))

    clauses: List[str] = []
    index = 0
    while index < len(tokens):
        if _is_cjk(tokens[index][0]):
            end = index
            while end < len(tokens) and _is_cjk(tokens[end][0]):
                end += 1
            clauses.append('"{}"'.format(" ".join(tokens[index:end])))
            index = end
        else:
            clauses.append('"{}"'.format(tokens[index]))
            index += 1
    return " AND ".join(clauses)


def _connect(db_path: Path) -> sqlite3.Connection:
    try:
        return sqlite3.connect(str(db_path))
    except sqlite3.Error as error:
        raise KnowledgeIndexError("无法打开索引文件 {}：{}".format(db_path, error))


def _search_text(entry: KnowledgeEntry) -> str:
    """拼出用于检索的文本，覆盖条目 id、标题、正文、定位与原文摘录。"""
    parts = [getattr(entry, name) or "" for name in _SEARCHED_FIELDS]
    return segment(" ".join(parts))


def _row(entry: KnowledgeEntry) -> Tuple[str, ...]:
    return (
        _search_text(entry),
        entry.entry_id,
        entry.chip,
        entry.peripheral,
        entry.kind,
        entry.title,
        entry.body,
        entry.locator,
        entry.source_id,
        entry.quote,
    )


def _create_entries_table(connection: sqlite3.Connection) -> None:
    connection.execute("DROP TABLE IF EXISTS entries")
    try:
        connection.execute(_CREATE_ENTRIES_SQL)
    except sqlite3.OperationalError as error:
        if "fts5" in str(error).lower():
            raise KnowledgeIndexError(
                "当前 SQLite（{}）未启用 FTS5 模块，无法建立全文索引。".format(
                    sqlite3.sqlite_version
                )
            )
        raise


def build_index(
    *,
    db_path: Path = DEFAULT_INDEX_PATH,
    curated_dir: Path = DEFAULT_CURATED_DIR,
    registry_path: Optional[Path] = DEFAULT_REGISTRY_PATH,
) -> int:
    """按当前知识文件全量重建索引，返回写入的条目数。

    每次都删除并重建表，因此重复构建不会产生重复条目，修改知识文件后重新构建即可
    生效。整个过程在一个事务内完成，中途失败会回滚，原有索引保持可用。
    """
    entries = load_curated(curated_dir, registry_path)
    db_path = Path(db_path)
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise KnowledgeIndexError("无法创建索引目录 {}：{}".format(db_path.parent, error))

    connection = _connect(db_path)
    try:
        with connection:
            _create_entries_table(connection)
            connection.execute("DROP TABLE IF EXISTS meta")
            connection.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            connection.executemany(_INSERT_SQL, [_row(entry) for entry in entries])
            connection.executemany(
                "INSERT INTO meta (key, value) VALUES (?, ?)",
                [
                    ("schema_version", SCHEMA_VERSION),
                    ("curated_version", CURATED_VERSION),
                    ("entry_count", str(len(entries))),
                    ("built_at", datetime.now().astimezone().isoformat(timespec="seconds")),
                ],
            )
    except sqlite3.DatabaseError as error:
        raise KnowledgeIndexError(
            "写入索引 {} 失败：{}。若文件已损坏，删除该文件后重新构建。".format(db_path, error)
        )
    finally:
        connection.close()
    return len(entries)


def _open_for_query(db_path: Path) -> sqlite3.Connection:
    """以只读方式打开索引，避免查询过程意外改动文件。"""
    if not db_path.is_file():
        raise KnowledgeIndexError(
            "索引不存在：{}。请先运行 airbornediag kb build 构建索引。".format(db_path)
        )
    uri = "{}?mode=ro".format(db_path.resolve().as_uri())
    try:
        return sqlite3.connect(uri, uri=True)
    except sqlite3.Error as error:
        raise KnowledgeIndexError("无法打开索引 {}：{}".format(db_path, error))


def _check_schema(connection: sqlite3.Connection, db_path: Path) -> None:
    """确认索引可读且格式版本与当前程序一致。"""
    try:
        row = connection.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    except sqlite3.DatabaseError as error:
        raise KnowledgeIndexError(
            "索引 {} 无法读取：{}。请重新构建索引。".format(db_path, error)
        )
    if row is None or row[0] != SCHEMA_VERSION:
        raise KnowledgeIndexError(
            "索引 {} 的格式版本为 {!r}，当前程序要求 {!r}，请重新构建索引。".format(
                db_path, None if row is None else row[0], SCHEMA_VERSION
            )
        )


def query_index(
    query: str,
    *,
    db_path: Path = DEFAULT_INDEX_PATH,
    chip: Optional[str] = None,
    peripheral: Optional[str] = None,
    limit: int = DEFAULT_LIMIT,
) -> List[KnowledgeEntry]:
    """按关键词查询知识条目，按相关度排序返回。

    ``chip`` 与 ``peripheral`` 为精确匹配（忽略大小写），留空表示不限制。
    ``limit`` 必须为正整数。没有匹配时返回空列表，这属于正常结果，不是错误。
    """
    if limit <= 0:
        raise KnowledgeError("返回条数上限必须是正整数，当前为 {!r}".format(limit))

    match_expression = build_match_expression(query)
    db_path = Path(db_path)
    connection = _open_for_query(db_path)
    try:
        _check_schema(connection, db_path)
        sql = _SELECT_SQL
        parameters: List[object] = [match_expression]
        if chip is not None and chip.strip():
            sql += " AND chip = ? COLLATE NOCASE"
            parameters.append(chip.strip())
        if peripheral is not None and peripheral.strip():
            sql += " AND peripheral = ? COLLATE NOCASE"
            parameters.append(peripheral.strip())
        sql += " ORDER BY bm25(entries), entry_id LIMIT ?"
        parameters.append(limit)

        try:
            rows = connection.execute(sql, parameters).fetchall()
        except sqlite3.OperationalError as error:
            raise KnowledgeIndexError(
                "索引 {} 查询失败：{}。索引可能已损坏，请重新构建。".format(db_path, error)
            )
    finally:
        connection.close()

    return [
        KnowledgeEntry(
            entry_id=row[0],
            chip=row[1],
            peripheral=row[2],
            kind=row[3],
            title=row[4],
            body=row[5],
            locator=row[6],
            source_id=row[7],
            quote=row[8],
        )
        for row in rows
    ]
