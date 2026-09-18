"""命令行入口测试：检查帮助、版本输出及退出状态。"""

from __future__ import annotations

import os
import subprocess
import sys
from importlib import metadata
from typing import List

from airbornediag import __version__

CLI_MODULE = "airbornediag.cli"


def run_cli(*args: str) -> "subprocess.CompletedProcess[str]":
    """以子进程方式运行命令行模块，返回包含输出与退出码的结果。"""
    env = dict(os.environ)
    # 固定子进程输出编码，避免 Windows 上按本地代码页写出导致解码不一致。
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, "-m", CLI_MODULE, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )


def _console_script_entry_points() -> List[metadata.EntryPoint]:
    """兼容 Python 3.9 与 3.10+ 的入口点查询。"""
    entry_points = metadata.entry_points()
    if hasattr(entry_points, "select"):
        selected = entry_points.select(group="console_scripts", name="airbornediag")
    else:
        selected = [
            ep
            for ep in entry_points.get("console_scripts", [])
            if ep.name == "airbornediag"
        ]
    return list(selected)


def test_help_exits_zero_and_prints_usage() -> None:
    result = run_cli("--help")
    assert result.returncode == 0
    assert "usage" in result.stdout
    assert "airbornediag" in result.stdout


def test_version_exits_zero_and_prints_project_version() -> None:
    result = run_cli("--version")
    assert result.returncode == 0
    assert __version__ in result.stdout


def test_version_matches_installed_metadata() -> None:
    assert metadata.version("airbornediag") == __version__


def test_console_script_entry_point_registered() -> None:
    # 可编辑安装会把 src 加入 sys.path，此时 src/airbornediag.egg-info 与
    # site-packages 中的 dist-info 都会被识别为发行版，入口点可能被枚举多次，
    # 因此按集合比较而非列表比较。
    values = {ep.value for ep in _console_script_entry_points()}
    assert values == {"airbornediag.cli:main"}
