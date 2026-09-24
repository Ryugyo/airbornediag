"""report 子命令的端到端测试：完整诊断流程与各类失败路径。

模型服务用测试进程内的假服务代替（tests/fake_mindie.py），其余环节都是真实实现：
真实的 Schema 校验、真实的诊断工具、真实的知识检索索引。这样覆盖的是完整流程，
不是模拟接口。

假服务只负责返回一段回答，工程侧对回答的解析与校验都是真的：回答不合规、引用了
不存在的观测时，流程必须失败并且不产出报告。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Sequence

import pytest

from airbornediag import cli
from airbornediag.knowledge import build_index
from fake_mindie import fake_mindie, unused_port
from mcu_helpers import record as make_record, register, write_record

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = REPO_ROOT / "examples"
FIXTURES = REPO_ROOT / "tests" / "fixtures"
CURATED_DIR = REPO_ROOT / "knowledge" / "curated"
REGISTRY = REPO_ROOT / "knowledge" / "source-registry.json"

ENV_PREFIX = "AIRBORNEDIAG_LLM_"
RECORD_002 = "examples/REC-2026-0918-002.json"
RECORD_001 = "examples/REC-2026-0918-001.json"

# REC-002 中工具结论引用的知识条目之一，用于确认检索确实按引用取到了条目。
REFERENCED_ENTRY = "MPC5554-FlexCAN2-ESR-FLTCONF"


@pytest.fixture(scope="module")
def index(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """把真实知识文件建成索引，供整个模块复用。"""
    db = tmp_path_factory.mktemp("report-index") / "knowledge.sqlite3"
    build_index(db_path=db, curated_dir=CURATED_DIR, registry_path=REGISTRY)
    return db


@pytest.fixture(autouse=True)
def repo_cwd(monkeypatch: pytest.MonkeyPatch) -> None:
    """切到工程根目录：命令行的默认路径都是相对工程根目录的。"""
    for name in list(os.environ):
        if name.startswith(ENV_PREFIX):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(REPO_ROOT)


def run_report(args: Sequence[str], base_url: str, db: Path) -> int:
    return cli.main(["report", *args, "--base-url", base_url, "--db", str(db)])


def answer(
    causes: Sequence[Dict[str, Any]] = (),
    checks: Sequence[str] = (),
) -> str:
    """假服务返回的回答文本。"""
    return json.dumps(
        {"candidate_causes": list(causes), "recommended_checks": list(checks)},
        ensure_ascii=False,
    )


def cause(statement: str, supporting: Sequence[str], contradicting: Sequence[str] = ()) -> Dict[str, Any]:
    return {
        "statement": statement,
        "supporting": list(supporting),
        "contradicting": list(contradicting),
    }


def fixture(name: str) -> Dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def state_pairs(analysis: Dict[str, Any]) -> List[Any]:
    """按内容与证据比较状态，不比较映射后的 id 字面值。"""
    return [
        (state["statement"], tuple(state["evidence"]))
        for state in analysis["confirmed_states"]
    ]


# --- 正常流程 ---------------------------------------------------------------


def test_can_record_matches_the_expected_report(
    index: Path, capsys: pytest.CaptureFixture
) -> None:
    """REC-002 走完整流程，程序组装部分与期望报告一致。

    模型只提供候选原因与检查建议；状态、矛盾、缺失信息、不支持项与 fault_state
    由程序组装，因此必须与期望报告逐项相同。
    """
    expected = fixture("RPT-2026-0918-002.json")["analysis"]
    model_causes = [
        cause("CAN 总线物理层异常", ["OBS-2", "OBS-3"]),
        cause("位定时配置不一致", ["OBS-3"]),
    ]
    with fake_mindie(payload={"text": [answer(model_causes, ["测量差分电平"])]}) as service:
        code = run_report([RECORD_002], service.base_url, index)

    captured = capsys.readouterr()
    assert code == cli.EXIT_OK, captured.err
    report = json.loads(captured.out)
    analysis = report["analysis"]

    # 回显三项由程序保留，与输入记录一致。
    assert report["device"] == {"model": "MPC5554"}
    assert report["test"]["id"] == "PBIT-CAN-A-0021"
    assert report["record_ref"] == "REC-2026-0918-002"
    assert report["provenance"] == {"input_origin": "simulated", "report_status": "actual"}

    assert analysis["fault_state"] == expected["fault_state"] == "confirmed"
    assert analysis["root_cause"] == "candidates_only"
    assert analysis["inconsistencies"] == expected["inconsistencies"] == []
    assert analysis["insufficient_data"] == expected["insufficient_data"]
    assert analysis["unsupported"] == expected["unsupported"]
    assert state_pairs(analysis) == state_pairs(expected)

    # 状态 id 唯一、带检测对象前缀，并保留规则来源。
    ids = [state["id"] for state in analysis["confirmed_states"]]
    assert len(set(ids)) == len(ids)
    assert ids == ["CAN_A/" + state["id"].split("/", 1)[1] for state in analysis["confirmed_states"]]
    assert {item.split("/", 1)[1] for item in ids} == {
        item["id"].split("/", 1)[1] for item in expected["confirmed_states"]
    }

    # 候选原因由程序编号，内容来自模型回答。
    assert [item["id"] for item in analysis["candidate_causes"]] == ["CC-1", "CC-2"]
    assert analysis["candidate_causes"][0]["statement"] == "CAN 总线物理层异常"
    assert analysis["candidate_causes"][0]["supporting"] == ["OBS-2", "OBS-3"]
    assert analysis["recommended_checks"] == ["测量差分电平"]

    # 进度与检索情况走标准错误，不混进报告。
    assert "知识检索：" in captured.err
    assert "报告：报告标识 RPT-2026-0918-002，fault_state confirmed" in captured.err


def test_tool_referenced_knowledge_reaches_the_model(
    index: Path, capsys: pytest.CaptureFixture
) -> None:
    """检索优先取工具结论引用的条目：它们必须出现在送出的提示词里。"""
    with fake_mindie(payload={"text": [answer()]}) as service:
        code = run_report([RECORD_002], service.base_url, index)

    assert code == cli.EXIT_OK
    prompt = service.last_request()["json"]["prompt"]
    assert REFERENCED_ENTRY in prompt
    # 结论与依据的整体规模受提示词上限约束。
    from airbornediag.llm import PROMPT_CHAR_LIMIT

    assert len(prompt) <= PROMPT_CHAR_LIMIT
    assert "知识检索：工具引用" in capsys.readouterr().err


def test_dspi_record_reports_no_fault_state(
    index: Path, capsys: pytest.CaptureFixture
) -> None:
    """DSPI 第 20 章不提供故障状态判据：没有观测时不给出任何故障状态。"""
    expected = fixture("RPT-2026-0918-001.json")["analysis"]
    with fake_mindie(payload={"text": [answer([], ["补充采集 DSPI_C.SR"])]}) as service:
        code = run_report([RECORD_001], service.base_url, index)

    assert code == cli.EXIT_OK
    analysis = json.loads(capsys.readouterr().out)["analysis"]
    assert analysis["fault_state"] == "not_determined"
    assert analysis["confirmed_states"] == []
    assert analysis["candidate_causes"] == []
    assert analysis["insufficient_data"] == expected["insufficient_data"]


def test_records_without_evidence_may_have_no_candidate_causes(
    index: Path, capsys: pytest.CaptureFixture
) -> None:
    """证据不足时允许不提出候选原因，root_cause 记为未判定。"""
    with fake_mindie(payload={"text": [answer()]}) as service:
        code = run_report([RECORD_001], service.base_url, index)

    assert code == cli.EXIT_OK
    assert json.loads(capsys.readouterr().out)["analysis"]["root_cause"] == "not_determined"


def test_two_instances_of_the_same_peripheral_get_distinct_state_ids(
    index: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """同一外设的两个实例产生同名规则 id 时，报告中的状态仍能唯一区分。"""
    data = make_record(target="CAN_A")
    data["observations"] = [
        register("OBS-A", "CAN_A.ESR", {"FLTCONF": "bus_off"}, target="CAN_A", semantics="instantaneous"),
        register("OBS-B", "CAN_B.ESR", {"FLTCONF": "bus_off"}, target="CAN_B", semantics="instantaneous"),
    ]
    path = write_record(tmp_path / "multi.json", data)

    with fake_mindie(payload={"text": [answer()]}) as service:
        code = run_report([str(path)], service.base_url, index)

    assert code == cli.EXIT_OK
    analysis = json.loads(capsys.readouterr().out)["analysis"]
    ids = [state["id"] for state in analysis["confirmed_states"]]
    assert sorted(ids) == [
        "CAN_A/FC-BUSOFF-NO-ACK-ONLY",
        "CAN_A/FC-BUSOFF-STATE",
        "CAN_B/FC-BUSOFF-NO-ACK-ONLY",
        "CAN_B/FC-BUSOFF-STATE",
    ]
    assert len(set(ids)) == 4


def test_report_can_be_written_to_a_file(
    index: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    out = tmp_path / "report.json"
    with fake_mindie(payload={"text": [answer()]}) as service:
        code = run_report([RECORD_002, "--out", str(out)], service.base_url, index)

    captured = capsys.readouterr()
    assert code == cli.EXIT_OK
    # 写了文件时标准输出为空，便于脚本使用。
    assert captured.out == ""
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["report_id"] == "RPT-2026-0918-002"


def test_extra_fields_in_the_answer_do_not_reach_the_report(
    index: Path, capsys: pytest.CaptureFixture
) -> None:
    """契约没有对应字段的内容不进入报告，但要提示使用者。"""
    text = json.dumps(
        {
            "candidate_causes": [],
            "recommended_checks": [],
            "confidence": 0.8,
        },
        ensure_ascii=False,
    )
    with fake_mindie(payload={"text": [text]}) as service:
        code = run_report([RECORD_002], service.base_url, index)

    captured = capsys.readouterr()
    assert code == cli.EXIT_OK
    assert "confidence" in captured.err
    assert "confidence" not in captured.out


# --- 关键失败路径 -----------------------------------------------------------


def test_unsupported_chip_ends_before_the_model_call(
    index: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    data = make_record(chip="TMS320F28335", target="eCAN_A")
    path = write_record(tmp_path / "other-chip.json", data)

    with fake_mindie() as service:
        code = run_report([str(path)], service.base_url, index)

    captured = capsys.readouterr()
    assert code == cli.EXIT_UNSUPPORTED
    assert captured.out == ""
    assert "未调用模型" in captured.err
    # 没有工具结论时不给模型凭空分析的机会。
    assert service.requests == []


def test_invalid_record_is_rejected_before_the_model_call(
    index: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    data = make_record()
    del data["test"]
    path = write_record(tmp_path / "broken.json", data)

    with fake_mindie() as service:
        code = run_report([str(path)], service.base_url, index)

    captured = capsys.readouterr()
    assert code == cli.EXIT_CONFIG
    assert captured.out == ""
    assert "未通过 Schema 校验" in captured.err
    assert service.requests == []


def test_missing_knowledge_index_has_its_own_exit_code(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    missing = tmp_path / "not-built.sqlite3"
    with fake_mindie() as service:
        code = cli.main(
            ["report", RECORD_002, "--base-url", service.base_url, "--db", str(missing)]
        )

    captured = capsys.readouterr()
    assert code == cli.EXIT_KB_INDEX
    assert captured.out == ""
    assert "知识库错误" in captured.err
    assert service.requests == []


def test_connection_failure_has_its_own_exit_code(index: Path, capsys: pytest.CaptureFixture) -> None:
    code = cli.main(
        [
            "report",
            RECORD_002,
            "--base-url",
            "http://127.0.0.1:{}".format(unused_port()),
            "--db",
            str(index),
            "--timeout",
            "5",
        ]
    )
    captured = capsys.readouterr()
    assert code == cli.EXIT_CONNECTION
    assert captured.out == ""
    assert "连接失败" in captured.err


def test_timeout_has_its_own_exit_code(index: Path, capsys: pytest.CaptureFixture) -> None:
    with fake_mindie(delay=3.0) as service:
        code = run_report([RECORD_002, "--timeout", "0.5"], service.base_url, index)
    assert code == cli.EXIT_TIMEOUT
    assert capsys.readouterr().out == ""


def test_http_error_has_its_own_exit_code(index: Path, capsys: pytest.CaptureFixture) -> None:
    with fake_mindie(payload={"error": "模型未加载"}, status=503) as service:
        code = run_report([RECORD_002], service.base_url, index)
    assert code == cli.EXIT_HTTP
    assert capsys.readouterr().out == ""


def test_response_format_error_has_its_own_exit_code(
    index: Path, capsys: pytest.CaptureFixture
) -> None:
    with fake_mindie(raw_body="<html>不是 JSON</html>") as service:
        code = run_report([RECORD_002], service.base_url, index)
    assert code == cli.EXIT_RESPONSE
    assert capsys.readouterr().out == ""


def test_answer_that_is_not_json_is_rejected(
    index: Path, capsys: pytest.CaptureFixture
) -> None:
    """回答不合规时报错并附上回答开头，不伪造一份成功报告。"""
    with fake_mindie(payload={"text": ["我认为可能是总线短路。"]}) as service:
        code = run_report([RECORD_002], service.base_url, index)

    captured = capsys.readouterr()
    assert code == cli.EXIT_ANALYSIS
    assert captured.out == ""
    assert "模型回答不合规" in captured.err
    assert "我认为可能是总线短路。" in captured.err
    assert "未产出报告" in captured.err


def test_answer_referencing_an_unknown_observation_is_rejected(
    index: Path, capsys: pytest.CaptureFixture
) -> None:
    """模型编造证据引用时报告无效，不能带着假引用输出。"""
    fabricated = [cause("总线物理层异常", ["OBS-99"])]
    with fake_mindie(payload={"text": [answer(fabricated, ["测量差分电平"])]}) as service:
        code = run_report([RECORD_002], service.base_url, index)

    captured = capsys.readouterr()
    assert code == cli.EXIT_ANALYSIS
    assert captured.out == ""
    assert "不存在的观测 OBS-99" in captured.err


def test_answer_missing_the_checks_key_is_rejected(
    index: Path, capsys: pytest.CaptureFixture
) -> None:
    text = json.dumps({"candidate_causes": []}, ensure_ascii=False)
    with fake_mindie(payload={"text": [text]}) as service:
        code = run_report([RECORD_002], service.base_url, index)

    captured = capsys.readouterr()
    assert code == cli.EXIT_ANALYSIS
    assert "缺少 recommended_checks" in captured.err
    assert captured.out == ""


def test_report_self_check_is_applied_to_the_runtime_report(
    index: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """运行期的报告同样过契约校验：组装出不合规的报告时不输出它。"""
    from airbornediag import pipeline

    # pipeline 在导入时绑定了 check_report，因此要替换的是它引用的那个名字。
    original = pipeline.check_report

    def break_echo(where: str, report: Any, **kwargs: Any) -> None:
        report["device"] = {"model": "改过的型号"}
        original(where, report, **kwargs)

    monkeypatch.setattr(pipeline, "check_report", break_echo)

    with fake_mindie(payload={"text": [answer()]}) as service:
        code = run_report([RECORD_002], service.base_url, index)

    captured = capsys.readouterr()
    assert code == cli.EXIT_ANALYSIS
    assert captured.out == ""
    assert "报告未通过契约校验" in captured.err
    assert "模型不得改写该字段" in captured.err
