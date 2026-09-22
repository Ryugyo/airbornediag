"""MCU 知识库：知识文件读取、全文索引构建与查询。

知识内容存放在工程根目录的 ``knowledge/curated/``，本包只负责读取、建索引和查询，
不把知识的判断逻辑写进代码。索引由 ``airbornediag kb build`` 离线构建，运行期只查询。
"""

from airbornediag.knowledge.index import (
    DEFAULT_INDEX_PATH,
    DEFAULT_LIMIT,
    SCHEMA_VERSION,
    build_index,
    build_match_expression,
    query_index,
    segment,
)
from airbornediag.knowledge.model import (
    CURATED_VERSION,
    DEFAULT_CURATED_DIR,
    DEFAULT_REGISTRY_PATH,
    ENTRY_KINDS,
    KnowledgeDataError,
    KnowledgeEntry,
    KnowledgeError,
    KnowledgeIndexError,
    load_curated,
    load_registry_sources,
)

__all__ = [
    "CURATED_VERSION",
    "DEFAULT_CURATED_DIR",
    "DEFAULT_INDEX_PATH",
    "DEFAULT_LIMIT",
    "DEFAULT_REGISTRY_PATH",
    "ENTRY_KINDS",
    "KnowledgeDataError",
    "KnowledgeEntry",
    "KnowledgeError",
    "KnowledgeIndexError",
    "SCHEMA_VERSION",
    "build_index",
    "build_match_expression",
    "load_curated",
    "load_registry_sources",
    "query_index",
    "segment",
]
