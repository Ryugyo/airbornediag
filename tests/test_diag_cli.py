"""diag 子命令的测试：输出流向、退出码与样例记录的实际结果。

样例记录直接取 examples/ 下的文件，与 docs/scenarios.md 的场景对应；边界情形由
临时构造的记录覆盖。断言关注的是「退出码能否区分失败原因」与「缺失信息有没有
被打印出来」，不锁定完整文本。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from airbornediag.cli import EXIT_CONFIG, EXIT_OK, EXIT_UNSUPPORTED
from mcu_helpers import record, register, write_record

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = REPO_ROOT / "examples"
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


# --- 样例记录 ---------------------------------------------------------------


def test_flexcan_example_runs_and_reports_states_with_basis() -> None:
    result = run_cli("diag", str(EXAMPLES / "REC-2026-0918-002.json"))

    assert result.returncode == EXIT_OK
    assert "记录：REC-2026-0918-002" in result.stdout
    assert "mpc5554.flexcan2" in result.stdout
    assert "FC-BUSOFF-STATE" in result.stdout
    # 每条结论都带出处与对应的知识条目，便于核对判据。
    assert "知识条目 MPC5554-FlexCAN2-" in result.stdout


def test_dspi_example_reports_missing_evidence_instead_of_a_conclusion() -> None:
    """DS-01：检测失败但无观测，输出里必须能看出「未判定」而不是「正常」。"""
    result = run_cli("diag", str(EXAMPLES / "REC-2026-0918-001.json"))

    assert result.returncode == EXIT_OK
    assert "mpc5554.dspi" in result.stdout
    assert "缺失信息：" in result.stdout
    assert "缺少观测表示未知，不等于该外设正常" in result.stdout
    # 没有观测就不该出现任何确认状态。
    assert "确认状态：" not in result.stdout


def test_diagnostics_go_to_stderr_and_payload_to_stdout() -> None:
    result = run_cli("diag", str(EXAMPLES / "REC-2026-0918-002.json"))

    assert "工具结果：" in result.stderr
    assert "未列出的字段表示未观测" in result.stderr
    assert "工具结果：" not in result.stdout


def test_json_output_is_machine_readable() -> None:
    result = run_cli("diag", str(EXAMPLES / "REC-2026-0918-001.json"), "--json")

    assert result.returncode == EXIT_OK
    payload = json.loads(result.stdout)
    assert payload["record_id"] == "REC-2026-0918-001"
    assert payload["chip"] == "MPC5554"
    # supported 必须显式出现：只看结论列表无法区分「没跑过工具」与「没有结论」。
    assert payload["supported"] is True
    assert payload["tool_results"][0]["target"] == "DSPI_C"


# --- 退出码 ----------------------------------------------------------------


def test_unsupported_chip_exits_with_its_own_code(tmp_path: Path) -> None:
    """记录合规但芯片不受支持：单独退出码，脚本不会把它当成成功。"""
    path = write_record(
        tmp_path / "unsupported.json", record(chip="TMS320F28335", target="eCAN_A")
    )

    result = run_cli("diag", str(path))

    assert result.returncode == EXIT_UNSUPPORTED
    assert result.returncode != EXIT_OK
    assert "本版不支持：" in result.stdout
    assert "未运行任何工具" in result.stderr
    payload = json.loads(run_cli("diag", str(path), "--json").stdout)
    assert payload["supported"] is False
    assert payload["tool_results"] == []


def test_record_violating_the_schema_exits_with_config_code(tmp_path: Path) -> None:
    """Schema 校验失败时指出具体路径，且不产生任何工具结果。"""
    data = record(observations=[register("OBS-1", "CAN_A.ESR", {"FLTCONF": "bus_off"})])
    data["observations"][0].pop("fields")  # register 观测必须给出 value 或 fields
    path = write_record(tmp_path / "invalid.json", data)

    result = run_cli("diag", str(path))

    assert result.returncode == EXIT_CONFIG
    assert "未通过 Schema 校验" in result.stderr
    assert "$.observations[0]" in result.stderr
    assert result.stdout == ""


def test_missing_record_file_exits_with_config_code(tmp_path: Path) -> None:
    result = run_cli("diag", str(tmp_path / "absent.json"))

    assert result.returncode == EXIT_CONFIG
    assert "不存在" in result.stderr


def test_malformed_json_exits_with_config_code(tmp_path: Path) -> None:
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")

    result = run_cli("diag", str(path))

    assert result.returncode == EXIT_CONFIG
    assert "不是合法 JSON" in result.stderr


def test_missing_schema_file_exits_with_config_code(tmp_path: Path) -> None:
    """--schema 指向的文件不存在时按配置错误处理，不静默跳过校验。"""
    path = write_record(tmp_path / "ok.json", record())

    result = run_cli("diag", str(path), "--schema", str(tmp_path / "absent.schema.json"))

    assert result.returncode == EXIT_CONFIG
    assert "Schema 文件不存在" in result.stderr


def test_record_without_any_observation_is_not_a_success_claim(tmp_path: Path) -> None:
    """记录合规且对象受支持时退出码为 0，但输出里仍要说明本次没有结论。"""
    path = write_record(tmp_path / "empty.json", record(target="CAN_A", observations=[]))

    text_run = run_cli("diag", str(path))

    assert text_run.returncode == EXIT_OK
    assert "缺失信息：" in text_run.stdout
    assert "确认状态：" not in text_run.stdout

    payload = json.loads(run_cli("diag", str(path), "--json").stdout)
    assert payload["supported"] is True
    missing = payload["tool_results"][0]["insufficient_data"]
    assert len(missing) == 1
    assert "不等于该外设正常" in missing[0]["reason"]
