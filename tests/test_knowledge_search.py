"""真实知识文件的检索效果测试。

与其他知识测试不同，这里直接使用 knowledge/curated/ 与 knowledge/source-registry.json，
索引建在临时目录中。目的是验证实际检索效果：中文描述与英文寄存器名混排时，
docs/scenarios.md 列出的字段名都能查到对应条目，且过滤与条数限制确实生效。

断言以「期望条目出现在命中中」为主，不锁定完整排序：排序由 bm25 决定，
调整正文措辞会让名次变动，但不应影响该条目能否被检索到。
"""

from __future__ import annotations

from pathlib import Path
from typing import List

import pytest

from airbornediag.knowledge import build_index, load_curated, query_index

REPO_ROOT = Path(__file__).resolve().parents[1]
CURATED_DIR = REPO_ROOT / "knowledge" / "curated"
REGISTRY = REPO_ROOT / "knowledge" / "source-registry.json"

FLEXCAN = "FlexCAN2"
DSPI = "DSPI"


@pytest.fixture(scope="module")
def index(tmp_path_factory: pytest.TempPathFactory) -> Path:
    db = tmp_path_factory.mktemp("knowledge-index") / "knowledge.sqlite3"
    build_index(db_path=db, curated_dir=CURATED_DIR, registry_path=REGISTRY)
    return db


def ids_of(db: Path, text: str, **kwargs) -> List[str]:
    return [hit.entry_id for hit in query_index(text, db_path=db, **kwargs)]


# --- 索引内容 -------------------------------------------------------------


def test_every_curated_entry_is_indexed(index: Path) -> None:
    """逐条按 id 检索，确认没有条目在重建后丢失。"""
    entries = load_curated(CURATED_DIR, REGISTRY)
    for entry in entries:
        assert entry.entry_id in ids_of(index, entry.entry_id, limit=len(entries) + 5)


def test_hits_carry_chip_peripheral_and_origin(index: Path) -> None:
    hits = query_index("清除", db_path=index, limit=20)
    assert hits
    for hit in hits:
        assert hit.chip == "MPC5554"
        assert hit.peripheral in (FLEXCAN, DSPI)
        assert hit.source_id == "MPC5554_RM"
        assert hit.locator.strip()
        assert hit.body.strip()
        assert hit.origin == "{} {}".format(hit.source_id, hit.locator)


# --- 代表性查询 -----------------------------------------------------------

REPRESENTATIVE_QUERIES = [
    # 中文描述
    ("总线关闭", "MPC5554-FlexCAN2-ESR-FLTCONF"),
    ("总线关闭恢复", "MPC5554-FlexCAN2-CR-BOFFREC"),
    ("错误被动", "MPC5554-FlexCAN2-ESR-FLTCONF"),
    ("只听模式", "MPC5554-FlexCAN2-CR-LOM"),
    ("读清除", "MPC5554-FlexCAN2-ESR-READCLEAR"),
    ("位时间", "MPC5554-FlexCAN2-CR-BITTIMING"),
    ("发送 FIFO 下溢", "MPC5554-DSPI-SR-TFUF"),
    ("写 1 清除", "MPC5554-DSPI-SR-W1C"),
    ("故障状态判据", "MPC5554-DSPI-CHAPTER-SCOPE"),
    # 英文寄存器与字段名
    ("FLTCONF", "MPC5554-FlexCAN2-ESR-FLTCONF"),
    ("FLTCONF LOM", "MPC5554-FlexCAN2-ESR-FLTCONF-LOM"),
    ("BOFFREC", "MPC5554-FlexCAN2-CR-BOFFREC"),
    ("BOFFINT", "MPC5554-FlexCAN2-ESR-BOFFINT-ERRINT"),
    ("TXECTR", "MPC5554-FlexCAN2-ECR-BUSOFF-TXECTR"),
    ("PRESDIV", "MPC5554-FlexCAN2-CR-BITTIMING"),
    ("TFUF", "MPC5554-DSPI-SR-TFUF"),
    ("MSTR", "MPC5554-DSPI-MCR-MSTR"),
    ("TXRXS", "MPC5554-DSPI-SR-TXRXS"),
    ("RFOF", "MPC5554-DSPI-SR-FIFO-FLAGS"),
    ("TXCTR", "MPC5554-DSPI-SR-FIFO-COUNTERS"),
]


@pytest.mark.parametrize("text, expected", REPRESENTATIVE_QUERIES)
def test_representative_query_finds_the_expected_entry(
    index: Path, text: str, expected: str
) -> None:
    assert expected in ids_of(index, text, limit=10)


def test_mixed_chinese_and_register_name_query(index: Path) -> None:
    """同一查询里混用中文与寄存器名，两部分都必须命中同一条目。"""
    assert "MPC5554-DSPI-SR-TFUF" in ids_of(index, "TFUF 从模式")
    assert "MPC5554-FlexCAN2-CR-LOM" in ids_of(index, "LOM 只听")
    # 0x 之类的写法不应把查询打断。
    assert "MPC5554-FlexCAN2-ESR-FLTCONF" in ids_of(index, "FLTCONF=1X")


def test_chinese_matches_by_substring(index: Path) -> None:
    """中文按子串匹配，查询词不必与正文中的词对齐。"""
    assert "MPC5554-FlexCAN2-ESR-FLTCONF" in ids_of(index, "线关")
    assert "MPC5554-FlexCAN2-CR-BOFFREC" in ids_of(index, "自动恢复")


def test_register_name_query_ignores_case(index: Path) -> None:
    assert ids_of(index, "fltconf") == ids_of(index, "FLTCONF")


def test_unambiguous_query_ranks_its_entry_first(index: Path) -> None:
    assert ids_of(index, "TFUF")[0] == "MPC5554-DSPI-SR-TFUF"
    assert ids_of(index, "MSTR")[0] == "MPC5554-DSPI-MCR-MSTR"


# --- 场景字段名的可检索性 -------------------------------------------------

SCENARIO_FIELD_NAMES = [
    # 22.3.3.2，Table 22-8
    "CANx_CR",
    "LOM",
    "BOFFMSK",
    "BOFFREC",
    "PRESDIV",
    "PROPSEG",
    "PSEG1",
    "PSEG2",
    "RJW",
    # 22.3.3.5，Figure 22-7
    "CANx_ECR",
    "TXECTR",
    "RXECTR",
    # 22.3.3.6，Figure 22-8，Table 22-11
    "CANx_ESR",
    "FLTCONF",
    # 20.3.2.4，Figure 20-6，Table 20-6
    "DSPIx_SR",
    "TCF",
    "TXRXS",
    "EOQF",
    "TFUF",
    "TFFF",
    "RFOF",
    "RFDF",
    "MSTR",
]


@pytest.mark.parametrize("field_name", SCENARIO_FIELD_NAMES)
def test_scenario_field_name_is_searchable(index: Path, field_name: str) -> None:
    """docs/scenarios.md 依据核对情况表里的字段名都能查到知识条目。"""
    assert ids_of(index, field_name), "{} 未检索到任何知识条目".format(field_name)


def test_scenario_locator_is_searchable(index: Path) -> None:
    """章节号可检索，便于按出处回查。"""
    assert "MPC5554-FlexCAN2-ESR-FLTCONF" in ids_of(index, "22.3.3.6")


# --- 过滤与条数限制 -------------------------------------------------------


def test_chip_filter_excludes_other_chips(index: Path) -> None:
    assert ids_of(index, "FLTCONF", chip="MPC5554")
    assert ids_of(index, "FLTCONF", chip="mpc5554")
    assert ids_of(index, "FLTCONF", chip="TMS320F28335") == []


def test_peripheral_filter_splits_the_two_peripherals(index: Path) -> None:
    unfiltered = query_index("清除", db_path=index, limit=20)
    assert {hit.peripheral for hit in unfiltered} == {FLEXCAN, DSPI}

    for peripheral in (FLEXCAN, DSPI):
        hits = query_index("清除", db_path=index, peripheral=peripheral, limit=20)
        assert hits
        assert {hit.peripheral for hit in hits} == {peripheral}


def test_peripheral_filter_accepts_either_case(index: Path) -> None:
    assert ids_of(index, "TFUF", peripheral="dspi") == ids_of(index, "TFUF", peripheral="DSPI")


def test_limit_caps_the_results(index: Path) -> None:
    assert len(query_index("清除", db_path=index, limit=1)) == 1
    assert len(query_index("清除", db_path=index, limit=2)) == 2
    assert len(query_index("清除", db_path=index, limit=20)) > 2


def test_no_match_returns_no_hits(index: Path) -> None:
    assert ids_of(index, "涡轮叶片疲劳") == []
    assert ids_of(index, "TMS320F28335 的 eCAN") == []
