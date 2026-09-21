"""kb 子命令测试：输出分流、过滤、无匹配与退出码。

命中结果走标准输出，索引路径、查询表达式与命中数走标准错误，因此可以分别断言。
全部测试在临时目录中构造知识文件与索引，不读取工程内真实知识内容。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from knowledge_helpers import curated_data, registry_data, write_json

from airbornediag import cli

CLI_MODULE = "airbornediag.cli"

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


class _FakeStdin:
    """替代标准输入，避免测试读取真实输入。"""

    def __init__(self, text: str = "", tty: bool = False) -> None:
        self._text = text
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty

    def read(self) -> str:
        return self._text


@pytest.fixture(autouse=True)
def isolated_working_directory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """切到临时工作目录，避免默认路径落到工程内真实知识目录。"""
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def knowledge(tmp_path: Path):
    """一套临时知识文件，返回 (知识目录, 登记表, 索引路径)。"""
    registry = write_json(tmp_path / "source-registry.json", registry_data())
    curated = tmp_path / "curated"
    write_json(curated / "flexcan2.json", curated_data([BUS_OFF_ENTRY]))
    write_json(curated / "dspi.json", curated_data([DSPI_ENTRY], peripheral="DSPI"))
    return curated, registry, tmp_path / "index.sqlite3"


def build_args(knowledge) -> list:
    curated, registry, db = knowledge
    return [
        "kb",
        "build",
        "--db",
        str(db),
        "--curated-dir",
        str(curated),
        "--registry",
        str(registry),
    ]


def query_args(knowledge, text: str, *extra: str) -> list:
    return ["kb", "query", text, "--db", str(knowledge[2])] + list(extra)


# --- 构建 -----------------------------------------------------------------


def test_build_reports_written_entries(knowledge, capsys: pytest.CaptureFixture) -> None:
    code = cli.main(build_args(knowledge))
    captured = capsys.readouterr()
    assert code == cli.EXIT_OK
    assert "已写入 2 条知识条目" in captured.out
    assert str(knowledge[2]) in captured.out
    assert captured.err == ""


def test_build_is_repeatable(knowledge, capsys: pytest.CaptureFixture) -> None:
    assert cli.main(build_args(knowledge)) == cli.EXIT_OK
    assert cli.main(build_args(knowledge)) == cli.EXIT_OK
    capsys.readouterr()
    assert cli.main(query_args(knowledge, "总线关闭", "--limit", "10")) == cli.EXIT_OK
    assert "命中：1 条" in capsys.readouterr().err


def test_build_with_unregistered_source_uses_data_exit_code(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    registry = write_json(tmp_path / "source-registry.json", registry_data())
    curated = tmp_path / "curated"
    write_json(curated / "bad.json", curated_data(source_id="NOT_REGISTERED"))
    code = cli.main(
        [
            "kb",
            "build",
            "--db",
            str(tmp_path / "index.sqlite3"),
            "--curated-dir",
            str(curated),
            "--registry",
            str(registry),
        ]
    )
    captured = capsys.readouterr()
    assert code == cli.EXIT_KB_DATA
    assert "知识库错误" in captured.err
    assert "NOT_REGISTERED" in captured.err


def test_build_with_missing_directory_uses_data_exit_code(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    code = cli.main(
        ["kb", "build", "--db", str(tmp_path / "index.sqlite3"), "--curated-dir", str(tmp_path / "无")]
    )
    assert code == cli.EXIT_KB_DATA
    assert "知识目录不存在" in capsys.readouterr().err


# --- 查询 -----------------------------------------------------------------


def test_query_prints_hits_to_stdout(knowledge, capsys: pytest.CaptureFixture) -> None:
    cli.main(build_args(knowledge))
    capsys.readouterr()

    code = cli.main(query_args(knowledge, "总线关闭"))
    captured = capsys.readouterr()

    assert code == cli.EXIT_OK
    assert "T-FlexCAN2-001" in captured.out
    assert "总线关闭状态" in captured.out
    assert "依据：MPC5554_RM 22.3.3.6" in captured.out
    assert "原文：1X Bus off" in captured.out
    assert "内容：FLTCONF" in captured.out
    # 结果不进标准错误，诊断信息不进标准输出。
    assert "T-FlexCAN2-001" not in captured.err
    assert "命中：1 条" in captured.err
    assert '查询："总 线 关 闭"' in captured.err


def test_query_reports_filters_and_limit(knowledge, capsys: pytest.CaptureFixture) -> None:
    cli.main(build_args(knowledge))
    capsys.readouterr()

    cli.main(query_args(knowledge, "FIFO", "--chip", "MPC5554", "--peripheral", "DSPI", "--limit", "3"))
    captured = capsys.readouterr()
    assert "过滤：芯片=MPC5554  外设=DSPI" in captured.err
    assert "命中：1 条（上限 3）" in captured.err


def test_query_without_match_returns_ok(knowledge, capsys: pytest.CaptureFixture) -> None:
    cli.main(build_args(knowledge))
    capsys.readouterr()

    code = cli.main(query_args(knowledge, "涡轮叶片疲劳"))
    captured = capsys.readouterr()
    assert code == cli.EXIT_OK
    assert captured.out == ""
    assert "命中：0 条" in captured.err
    assert "没有匹配的知识条目。" in captured.err


def test_query_missing_index_uses_index_exit_code(
    knowledge, capsys: pytest.CaptureFixture
) -> None:
    code = cli.main(query_args(knowledge, "总线关闭"))
    captured = capsys.readouterr()
    assert code == cli.EXIT_KB_INDEX
    assert "kb build" in captured.err
    assert captured.out == ""


def test_query_can_read_text_from_stdin(
    knowledge, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli.main(build_args(knowledge))
    capsys.readouterr()
    monkeypatch.setattr(sys, "stdin", _FakeStdin(text="总线关闭\n"))
    assert cli.main(["kb", "query", "--db", str(knowledge[2])]) == cli.EXIT_OK
    assert "T-FlexCAN2-001" in capsys.readouterr().out


def test_query_without_text_is_rejected(
    knowledge, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "stdin", _FakeStdin(tty=True))
    code = cli.main(["kb", "query", "--db", str(knowledge[2])])
    assert code == cli.EXIT_CONFIG
    assert "没有可查询的文本" in capsys.readouterr().err


def test_query_with_only_punctuation_is_rejected(
    knowledge, capsys: pytest.CaptureFixture
) -> None:
    code = cli.main(query_args(knowledge, "###"))
    assert code == cli.EXIT_CONFIG
    assert "没有可检索的文字" in capsys.readouterr().err


def test_query_limit_must_be_positive(knowledge) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(query_args(knowledge, "总线关闭", "--limit", "0"))
    assert exit_info.value.code == cli.EXIT_CONFIG


# --- 用法 -----------------------------------------------------------------


def test_kb_without_action_prints_usage(capsys: pytest.CaptureFixture) -> None:
    assert cli.main(["kb"]) == cli.EXIT_OK
    captured = capsys.readouterr()
    assert "build" in captured.out
    assert "query" in captured.out


def test_kb_help_lists_both_actions() -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["kb", "--help"])
    assert exit_info.value.code == 0


def test_console_entry_point_end_to_end(knowledge) -> None:
    """经安装的命令行入口真实跑一次构建与查询。"""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    curated, registry, db = knowledge

    build = subprocess.run(
        [
            sys.executable,
            "-m",
            CLI_MODULE,
            "kb",
            "build",
            "--db",
            str(db),
            "--curated-dir",
            str(curated),
            "--registry",
            str(registry),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )
    assert build.returncode == cli.EXIT_OK
    assert "已写入 2 条知识条目" in build.stdout

    query = subprocess.run(
        [sys.executable, "-m", CLI_MODULE, "kb", "query", "TFUF", "--db", str(db)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )
    assert query.returncode == cli.EXIT_OK
    assert "T-DSPI-001" in query.stdout
    assert "命中：1 条" in query.stderr
