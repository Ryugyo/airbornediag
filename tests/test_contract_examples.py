"""契约文件测试：Schema 校验、回显一致性与引用关系。

本测试只覆盖格式与引用关系，不涉及芯片诊断规则。测试分三类：

- 正向：示例与期望报告通过校验；
- 负例：分别触发 Schema 约束与脚本自行负责的跨文档检查，确认校验确实会失败；
- 限制：锁定“Schema 未执行的约束”这一已知缺口，防止文档与行为脱节。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "validate_contracts.py"

RECORD = "examples/REC-2026-0918-002.json"
REPORT = "tests/fixtures/RPT-2026-0918-002.json"

COPIED_DIRS = ("schemas", "examples")
COPIED_FILES = ("knowledge/source-registry.json",)
FIXTURES_DIR = "tests/fixtures"


def run_validator(root: Path) -> "subprocess.CompletedProcess[str]":
    """以子进程运行校验脚本，返回输出与退出码。"""
    env = dict(os.environ)
    # 固定子进程输出编码，避免 Windows 上按本地代码页写出导致解码不一致。
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )


def build_tree(tmp_path: Path) -> Path:
    """复制契约文件到临时目录，供负例使用。"""
    for name in COPIED_DIRS:
        shutil.copytree(REPO_ROOT / name, tmp_path / name)
    for name in COPIED_FILES:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO_ROOT / name, target)
    shutil.copytree(REPO_ROOT / FIXTURES_DIR, tmp_path / FIXTURES_DIR)
    return tmp_path


def load(relative: str) -> Any:
    with (REPO_ROOT / relative).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def edit(root: Path, relative: str, mutate) -> None:
    """读取临时目录中的 JSON，就地修改后写回。"""
    path = root / relative
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    mutate(data)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)


def test_examples_and_reports_pass_validation() -> None:
    result = run_validator(REPO_ROOT)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "契约校验通过" in result.stdout


def test_record_without_observations_echoes_empty_array() -> None:
    # 输入记录缺省 observations 时，期望报告应使用解析出的空数组。
    record = load("examples/REC-2026-0918-001.json")
    report = load("tests/fixtures/RPT-2026-0918-001.json")
    assert "observations" not in record
    assert report["observations"] == []
    assert report["analysis"]["fault_state"] == "not_determined"
    assert report["analysis"]["candidate_causes"] == []


def test_validator_rejects_unknown_evidence_reference(tmp_path: Path) -> None:
    root = build_tree(tmp_path)

    def mutate(report: dict) -> None:
        report["analysis"]["confirmed_states"][0]["evidence"] = ["OBS-99"]

    edit(root, REPORT, mutate)
    result = run_validator(root)
    assert result.returncode == 1
    assert "不存在的观测 OBS-99" in result.stdout


def test_validator_rejects_unregistered_source(tmp_path: Path) -> None:
    root = build_tree(tmp_path)

    def mutate(report: dict) -> None:
        report["analysis"]["confirmed_states"][0]["basis"]["source"] = "UNKNOWN_DOC"

    edit(root, REPORT, mutate)
    result = run_validator(root)
    assert result.returncode == 1
    assert "UNKNOWN_DOC 未登记在来源登记表中" in result.stdout


def test_validator_rejects_rewritten_echoed_field(tmp_path: Path) -> None:
    # 改成 pass 仍然合法，Schema 不会报错，只有回显一致性检查能发现，
    # 因此这条测试单独验证脚本负责的那部分。
    root = build_tree(tmp_path)

    def mutate(report: dict) -> None:
        report["test"]["result"] = "pass"

    edit(root, REPORT, mutate)
    result = run_validator(root)
    assert result.returncode == 1
    assert "模型不得改写该字段" in result.stdout


def test_validator_rejects_duplicate_observation_id(tmp_path: Path) -> None:
    # 观测 id 的唯一性按字段取值判定，Schema 表达不了，由脚本负责。
    root = build_tree(tmp_path)

    def duplicate(data: dict) -> None:
        data["observations"].append(dict(data["observations"][0]))

    edit(root, RECORD, duplicate)
    edit(root, REPORT, duplicate)
    result = run_validator(root)
    assert result.returncode == 1
    assert "观测 id OBS-1 重复" in result.stdout


def test_schema_rejects_register_observation_without_value_or_fields(tmp_path: Path) -> None:
    # register 观测必须提供 value 或非空 fields，且不同时提供两者。
    root = build_tree(tmp_path)

    def drop_fields(data: dict) -> None:
        del data["observations"][0]["fields"]

    # 记录与报告需同时修改，否则先命中的是回显一致性检查。
    edit(root, RECORD, drop_fields)
    edit(root, REPORT, drop_fields)
    result = run_validator(root)
    assert result.returncode == 1
    assert "Schema 校验失败" in result.stdout
    assert "'value' is a required property" in result.stdout


def test_schema_rejects_unknown_observation_kind(tmp_path: Path) -> None:
    root = build_tree(tmp_path)

    def mutate(data: dict) -> None:
        data["observations"][4]["kind"] = "scope_trace"

    edit(root, RECORD, mutate)
    edit(root, REPORT, mutate)
    result = run_validator(root)
    assert result.returncode == 1
    assert "Schema 校验失败" in result.stdout
    assert "'scope_trace' is not one of ['register', 'can_frame', 'spi_transfer', 'timeout']" in result.stdout


def test_schema_enforces_cross_file_reference(tmp_path: Path) -> None:
    """报告 Schema 对 fault-record.schema.json 的相对 $ref 必须真正生效。

    这条测试是跨文件 $ref 从本地解析的证据：若引用未解析或改从网络获取，
    device 的约束不会生效，改动就会被放过。
    """
    root = build_tree(tmp_path)

    def mutate(data: dict) -> None:
        data["device"]["model"] = 123

    edit(root, RECORD, mutate)
    edit(root, REPORT, mutate)
    result = run_validator(root)
    assert result.returncode == 1
    assert "$.device.model" in result.stdout
    assert "is not of type 'string'" in result.stdout


def test_validator_states_the_datetime_format_gap() -> None:
    """校验通过时必须如实说明哪些约束没有执行。

    Schema 中的 format 只在安装了对应格式校验器时才执行。未执行时必须显式声明，
    不能让使用者以为日期时间格式已被检查。
    """
    result = run_validator(REPO_ROOT)
    assert result.returncode == 0
    if "date-time" in Draft202012Validator.FORMAT_CHECKER.checkers:
        assert "未被执行" not in result.stdout
    else:
        assert "format: date-time 未被执行" in result.stdout
