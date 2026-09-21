"""知识条目模型与知识文件读取。

知识分两层存放：

- ``knowledge/sources/``：原始芯片手册，不提交 Git，只用于人工核对；
- ``knowledge/curated/``：按场景核对后整理的知识条目，提交 Git，是检索的唯一输入。

本模块只把 curated 文件读成条目并校验出处，不做索引与查询，后者见 ``index.py``。

出处校验复用 ``knowledge/source-registry.json``：条目的 ``source_id`` 必须已登记，
且条目的芯片必须落在该来源声明的 ``applies_to`` 之内。这条检查用于避免把不同 MCU
的同名外设、寄存器或状态定义当成同一件事。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

# 默认路径相对当前工作目录，与 .env 的查找方式一致，便于在工程根目录直接运行。
DEFAULT_CURATED_DIR = Path("knowledge/curated")
DEFAULT_REGISTRY_PATH = Path("knowledge/source-registry.json")

# 知识文件格式版本。字段含义变化时递增。
CURATED_VERSION = "0.1.0"

# 与 docs/architecture.md 的知识三分类一致。本批条目全部是 chip_knowledge；
# detection_definition 与 diagnosis_basis 待后续批次，此处先固定取值集合。
ENTRY_KINDS = ("chip_knowledge", "detection_definition", "diagnosis_basis")

_FILE_FIELDS = ("curated_version", "chip", "peripheral", "source_id", "entries")
_ENTRY_FIELDS = ("id", "kind", "title", "body", "locator", "quote")

_ENTRY_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


class KnowledgeError(Exception):
    """知识库操作失败。"""


class KnowledgeDataError(KnowledgeError):
    """知识文件或来源登记表缺失、格式错误，或两者互相矛盾。"""


class KnowledgeIndexError(KnowledgeError):
    """索引缺失、损坏、版本不符，或当前 SQLite 不支持全文检索。"""


@dataclass(frozen=True)
class KnowledgeEntry:
    """一条经核对的知识条目。"""

    entry_id: str
    chip: str
    peripheral: str
    kind: str
    title: str
    body: str
    locator: str
    source_id: str
    quote: Optional[str] = None

    @property
    def origin(self) -> str:
        """出处的可读形式：来源 id 加手册内的定位。"""
        return "{} {}".format(self.source_id, self.locator)


def _read_json(path: Path, description: str) -> Any:
    """读取 JSON 文件，失败时给出带文件名的说明。"""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise KnowledgeDataError("{}不存在：{}".format(description, path))
    except (OSError, UnicodeDecodeError) as error:
        raise KnowledgeDataError("读取{} {} 失败：{}".format(description, path, error))
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        raise KnowledgeDataError("{} {} 不是合法 JSON：{}".format(description, path, error))


def _require_text(container: Mapping[str, Any], key: str, where: str) -> str:
    """取出必须为非空字符串的字段。"""
    value = container.get(key)
    if not isinstance(value, str) or not value.strip():
        raise KnowledgeDataError("{} 缺少非空的 {} 字段。".format(where, key))
    return value.strip()


def load_registry_sources(path: Path) -> Dict[str, Mapping[str, Any]]:
    """读取来源登记表，返回 id 到登记项的映射。"""
    data = _read_json(Path(path), "来源登记表")
    if not isinstance(data, dict):
        raise KnowledgeDataError("来源登记表 {} 的顶层必须是对象。".format(path))

    sources = data.get("sources")
    if not isinstance(sources, list) or not sources:
        raise KnowledgeDataError("来源登记表 {} 缺少非空的 sources 数组。".format(path))

    registry: Dict[str, Mapping[str, Any]] = {}
    for item in sources:
        if not isinstance(item, dict):
            raise KnowledgeDataError("来源登记表 {} 的 sources 中含有非对象项。".format(path))
        source_id = _require_text(item, "id", "来源登记表 {}".format(path))
        if source_id in registry:
            raise KnowledgeDataError("来源登记表 {} 中 id {} 重复。".format(path, source_id))
        registry[source_id] = item
    return registry


def _check_entry(raw: Any, where: str) -> Dict[str, str]:
    """校验单条知识条目，返回规整后的字段字典。"""
    if not isinstance(raw, dict):
        raise KnowledgeDataError("{} 中有非对象的知识条目。".format(where))

    unknown = sorted(set(raw) - set(_ENTRY_FIELDS))
    if unknown:
        raise KnowledgeDataError(
            "{} 的知识条目出现未定义字段 {}。".format(where, "、".join(unknown))
        )

    entry_id = _require_text(raw, "id", where)
    if not _ENTRY_ID_PATTERN.match(entry_id):
        raise KnowledgeDataError(
            "{} 的条目 id {!r} 只能由字母、数字、下划线和连字符组成。".format(where, entry_id)
        )

    kind = _require_text(raw, "kind", "{} 的条目 {}".format(where, entry_id))
    if kind not in ENTRY_KINDS:
        raise KnowledgeDataError(
            "{} 的条目 {} 的 kind {!r} 不在 {} 之内。".format(
                where, entry_id, kind, "、".join(ENTRY_KINDS)
            )
        )

    fields = {"id": entry_id, "kind": kind}
    for name in ("title", "body", "locator"):
        fields[name] = _require_text(raw, name, "{} 的条目 {}".format(where, entry_id))

    quote = raw.get("quote")
    if quote is not None:
        if not isinstance(quote, str) or not quote.strip():
            raise KnowledgeDataError(
                "{} 的条目 {} 的 quote 若存在必须是非空字符串。".format(where, entry_id)
            )
        fields["quote"] = quote.strip()
    return fields


def _load_file(
    path: Path,
    registry: Mapping[str, Mapping[str, Any]],
    seen: Dict[str, str],
) -> List[KnowledgeEntry]:
    """读取单个知识文件，校验文件头、出处与条目，返回条目列表。"""
    where = "知识文件 {}".format(path)
    data = _read_json(path, "知识文件")
    if not isinstance(data, dict):
        raise KnowledgeDataError("{} 的顶层必须是对象。".format(where))

    unknown = sorted(set(data) - set(_FILE_FIELDS) - {"note"})
    if unknown:
        raise KnowledgeDataError("{} 出现未定义字段 {}。".format(where, "、".join(unknown)))

    for name in _FILE_FIELDS:
        if name not in data:
            raise KnowledgeDataError("{} 缺少 {} 字段。".format(where, name))

    version = _require_text(data, "curated_version", where)
    if version != CURATED_VERSION:
        raise KnowledgeDataError(
            "{} 的 curated_version 为 {!r}，当前程序要求 {!r}。".format(
                where, version, CURATED_VERSION
            )
        )

    chip = _require_text(data, "chip", where)
    peripheral = _require_text(data, "peripheral", where)
    source_id = _require_text(data, "source_id", where)

    if registry:
        if source_id not in registry:
            raise KnowledgeDataError(
                "{} 引用的来源 {} 未在来源登记表中登记。".format(where, source_id)
            )
        applies_to = registry[source_id].get("applies_to")
        if isinstance(applies_to, list) and applies_to and chip not in applies_to:
            raise KnowledgeDataError(
                "{} 的芯片 {} 不在来源 {} 声明的适用范围内（{}）。".format(
                    where, chip, source_id, "、".join(str(item) for item in applies_to)
                )
            )

    raw_entries = data["entries"]
    if not isinstance(raw_entries, list) or not raw_entries:
        raise KnowledgeDataError("{} 的 entries 必须是非空数组。".format(where))

    entries: List[KnowledgeEntry] = []
    for raw in raw_entries:
        fields = _check_entry(raw, where)
        entry_id = fields["id"]
        if entry_id in seen:
            raise KnowledgeDataError(
                "条目 id {} 重复：{} 与 {}。".format(entry_id, seen[entry_id], path)
            )
        seen[entry_id] = str(path)
        entries.append(
            KnowledgeEntry(
                entry_id=entry_id,
                chip=chip,
                peripheral=peripheral,
                kind=fields["kind"],
                title=fields["title"],
                body=fields["body"],
                locator=fields["locator"],
                source_id=source_id,
                quote=fields.get("quote"),
            )
        )
    return entries


def load_curated(
    curated_dir: Path = DEFAULT_CURATED_DIR,
    registry_path: Optional[Path] = DEFAULT_REGISTRY_PATH,
) -> List[KnowledgeEntry]:
    """读取目录下全部知识文件，按条目 id 排序返回。

    ``registry_path`` 为 None 时跳过出处校验，仅供格式测试使用；正常调用应传入
    来源登记表，以便发现引用了未登记来源的知识条目。
    """
    curated_dir = Path(curated_dir)
    if not curated_dir.is_dir():
        raise KnowledgeDataError("知识目录不存在：{}".format(curated_dir))

    files = sorted(item for item in curated_dir.glob("*.json") if item.is_file())
    if not files:
        raise KnowledgeDataError("知识目录 {} 下没有 .json 知识文件。".format(curated_dir))

    registry: Mapping[str, Mapping[str, Any]] = {}
    if registry_path is not None:
        registry = load_registry_sources(Path(registry_path))

    seen: Dict[str, str] = {}
    entries: List[KnowledgeEntry] = []
    for path in files:
        entries.extend(_load_file(path, registry, seen))

    entries.sort(key=lambda entry: entry.entry_id)
    return entries
