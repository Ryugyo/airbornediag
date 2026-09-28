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
from typing import Any, Dict, List, Optional, Sequence, Tuple

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

# 命令行的默认路径都是相对当前目录的。需要离开工程根目录运行的用例，用这组参数
# 把默认值固定到工程内的实际位置。
REPO_PATHS = [
    "--schema",
    str(REPO_ROOT / "schemas" / "fault-record.schema.json"),
    "--schema-dir",
    str(REPO_ROOT / "schemas"),
    "--curated-dir",
    str(CURATED_DIR),
    "--registry",
    str(REGISTRY),
]

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


def run_report_to_file(
    args: Sequence[str], base_url: str, db: Path, tmp_path: Path
) -> Tuple[int, Optional[Dict[str, Any]]]:
    """跑一次 report，报告写到临时目录后读回来。

    默认模式会把报告留在当前目录的 results/ 下，因此需要报告内容的用例统一用
    --out 写到临时目录，既拿到文件也不在工程里留下生成物。
    """
    out = tmp_path / "report.json"
    code = run_report([*args, "--out", str(out)], base_url, db)
    report = json.loads(out.read_text(encoding="utf-8")) if out.is_file() else None
    return code, report


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


def expected_reports() -> Dict[str, Dict[str, Any]]:
    """仓库内全部期望报告，按 record_ref 索引。"""
    reports: Dict[str, Dict[str, Any]] = {}
    for path in sorted(FIXTURES.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        reports[data["record_ref"]] = data
    return reports


# examples/ 下的全部输入记录。新增案例时这里不用逐条登记，参数化会自动带上。
RECORD_PATHS = sorted(EXAMPLES.glob("*.json"))


def state_pairs(analysis: Dict[str, Any]) -> List[Any]:
    """按内容与证据比较状态，不比较映射后的 id 字面值。"""
    return [
        (state["statement"], tuple(state["evidence"]))
        for state in analysis["confirmed_states"]
    ]


# --- 正常流程 ---------------------------------------------------------------


def test_can_record_matches_the_expected_report(
    index: Path, tmp_path: Path, capsys: pytest.CaptureFixture
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
        code, report = run_report_to_file([RECORD_002], service.base_url, index, tmp_path)

    captured = capsys.readouterr()
    assert code == cli.EXIT_OK, captured.err
    assert report is not None
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
    assert "报告已保存：" in captured.err

    # 标准输出是可读简述，程序给出的结论与模型给出的建议分节标注来源。
    assert "诊断报告：RPT-2026-0918-002" in captured.out
    assert "故障状态（程序按手册判据判定）：已确认" in captured.out
    assert "候选原因（模型给出的待验证推测，未经确认，2 项）：" in captured.out
    assert "CAN 总线物理层异常" in captured.out


@pytest.mark.parametrize("record_path", RECORD_PATHS, ids=[path.name for path in RECORD_PATHS])
def test_example_matches_its_expected_report(
    record_path: Path, index: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """每个输入记录走完整流程，程序组装的部分与期望报告逐项一致。

    与上面那条 REC-002 的用例分工不同：那条核对模型给出的文本如何进入报告，这条把
    全部案例参数化起来，核对由程序决定的部分——状态、矛盾、缺失信息、不支持项与
    fault_state。假服务返回空数组，因此候选原因与检查建议为空是预期结果，模型自由
    文本不逐字比对。
    """
    record = json.loads(record_path.read_text(encoding="utf-8"))
    expected = expected_reports()[record["record_id"]]["analysis"]

    with fake_mindie(payload={"text": [answer()]}) as service:
        code, report = run_report_to_file([str(record_path)], service.base_url, index, tmp_path)

    captured = capsys.readouterr()
    assert code == cli.EXIT_OK, captured.err
    assert report is not None

    # 回显三项由程序按输入记录的字段值保留，与期望报告、输入记录都一致。
    assert report["report_id"] == "RPT-" + record["record_id"][len("REC-") :]
    assert report["record_ref"] == record["record_id"]
    assert report["provenance"] == {"input_origin": record["origin"], "report_status": "actual"}
    assert report["device"] == record["device"]
    assert report["test"] == record["test"]
    assert report["observations"] == record.get("observations", [])

    analysis = report["analysis"]
    assert analysis["fault_state"] == expected["fault_state"]
    assert analysis["inconsistencies"] == expected["inconsistencies"]
    assert analysis["insufficient_data"] == expected["insufficient_data"]
    assert analysis["unsupported"] == expected["unsupported"]
    assert state_pairs(analysis) == state_pairs(expected)

    # 假服务给的是空数组：没有候选原因时 root_cause 记为未判定。
    assert analysis["candidate_causes"] == []
    assert analysis["recommended_checks"] == []
    assert analysis["root_cause"] == "not_determined"


def test_tool_referenced_knowledge_reaches_the_model(
    index: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """检索优先取工具结论引用的条目：它们必须出现在送出的提示词里。"""
    with fake_mindie(payload={"text": [answer()]}) as service:
        code, _ = run_report_to_file([RECORD_002], service.base_url, index, tmp_path)

    assert code == cli.EXIT_OK
    prompt = service.last_request()["json"]["prompt"]
    assert REFERENCED_ENTRY in prompt
    # 结论与依据的整体规模受提示词上限约束。
    from airbornediag.llm import PROMPT_CHAR_LIMIT

    assert len(prompt) <= PROMPT_CHAR_LIMIT
    assert "知识检索：工具引用" in capsys.readouterr().err


def test_dspi_record_reports_no_fault_state(
    index: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """DSPI 第 20 章不提供故障状态判据：没有观测时不给出任何故障状态。"""
    expected = fixture("RPT-2026-0918-001.json")["analysis"]
    with fake_mindie(payload={"text": [answer([], ["补充采集 DSPI_C.SR"])]}) as service:
        code, report = run_report_to_file([RECORD_001], service.base_url, index, tmp_path)

    assert code == cli.EXIT_OK
    assert report is not None
    analysis = report["analysis"]
    assert analysis["fault_state"] == "not_determined"
    assert analysis["confirmed_states"] == []
    assert analysis["candidate_causes"] == []
    assert analysis["insufficient_data"] == expected["insufficient_data"]


def test_records_without_evidence_may_have_no_candidate_causes(
    index: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """证据不足时允许不提出候选原因，root_cause 记为未判定。"""
    with fake_mindie(payload={"text": [answer()]}) as service:
        code, report = run_report_to_file([RECORD_001], service.base_url, index, tmp_path)

    assert code == cli.EXIT_OK
    assert report is not None
    assert report["analysis"]["root_cause"] == "not_determined"


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
        code, report = run_report_to_file([str(path)], service.base_url, index, tmp_path)

    assert code == cli.EXIT_OK
    assert report is not None
    analysis = report["analysis"]
    ids = [state["id"] for state in analysis["confirmed_states"]]
    assert sorted(ids) == [
        "CAN_A/FC-BUSOFF-NO-ACK-ONLY",
        "CAN_A/FC-BUSOFF-STATE",
        "CAN_B/FC-BUSOFF-NO-ACK-ONLY",
        "CAN_B/FC-BUSOFF-STATE",
    ]
    assert len(set(ids)) == 4


def test_out_writes_only_the_given_path(
    index: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """指定 --out 时只写该路径：不另建 results/，终端同样显示简述与实际路径。"""
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "report.json"
    record = str(EXAMPLES / "REC-2026-0918-002.json")

    with fake_mindie(payload={"text": [answer()]}) as service:
        code = run_report([record, *REPO_PATHS, "--out", str(out)], service.base_url, index)

    captured = capsys.readouterr()
    assert code == cli.EXIT_OK, captured.err
    assert not (tmp_path / "results").exists()
    assert "诊断报告：RPT-2026-0918-002" in captured.out
    assert "报告已保存：{}".format(out) in captured.err
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["report_id"] == "RPT-2026-0918-002"

    # 显式路径沿用既有策略：已存在时覆盖，不追加序号。
    with fake_mindie(payload={"text": [answer([], ["补充采集"])]}) as service:
        code = run_report([record, *REPO_PATHS, "--out", str(out)], service.base_url, index)

    assert code == cli.EXIT_OK
    assert [path.name for path in tmp_path.iterdir() if path.is_dir()] == []
    assert json.loads(out.read_text(encoding="utf-8"))["analysis"]["recommended_checks"] == [
        "补充采集"
    ]


# --- 默认保存到 results/ ----------------------------------------------------


def test_default_run_saves_the_report_under_results(
    index: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """未指定 --out 时报告落到当前目录的 results/，终端只显示简述，不再打一遍 JSON。"""
    monkeypatch.chdir(tmp_path)
    record = str(EXAMPLES / "REC-2026-0918-002.json")

    with fake_mindie(payload={"text": [answer()]}) as service:
        code = run_report([record, *REPO_PATHS], service.base_url, index)

    captured = capsys.readouterr()
    assert code == cli.EXIT_OK, captured.err

    saved = list((tmp_path / "results").glob("RPT-2026-0918-002_*.json"))
    assert len(saved) == 1
    report = json.loads(saved[0].read_text(encoding="utf-8"))
    assert report["report_id"] == "RPT-2026-0918-002"

    assert "诊断报告：RPT-2026-0918-002" in captured.out
    assert "schema_version" not in captured.out
    assert not captured.out.lstrip().startswith("{")
    assert "报告已保存：{}".format(saved[0].resolve()) in captured.err


def test_repeated_runs_keep_both_reports(
    index: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """同一秒内重复运行也不覆盖：第二份带序号，第一份仍在。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_run_stamp", lambda: "20260928T161234")
    record = str(EXAMPLES / "REC-2026-0918-002.json")

    for _ in range(2):
        with fake_mindie(payload={"text": [answer()]}) as service:
            code = run_report([record, *REPO_PATHS], service.base_url, index)
        assert code == cli.EXIT_OK

    assert sorted(path.name for path in (tmp_path / "results").iterdir()) == [
        "RPT-2026-0918-002_20260928T161234-2.json",
        "RPT-2026-0918-002_20260928T161234.json",
    ]


def test_results_path_keeps_the_report_inside_the_results_directory() -> None:
    """报告标识里的分隔符等字符只影响文件名，不会把报告写到 results/ 之外。"""
    path = cli._results_path("RPT/2026:bad*id")

    assert path.parent == cli.DEFAULT_RESULTS_DIR
    assert path.name.startswith("RPT_2026_bad_id_")
    assert path.suffix == ".json"


def test_save_failure_in_the_default_mode_is_reported(
    index: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """results/ 建不出来时明确报错：不显示简述，也不出现任何成功提示。"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "results").write_text("占位", encoding="utf-8")
    record = str(EXAMPLES / "REC-2026-0918-002.json")

    with fake_mindie(payload={"text": [answer()]}) as service:
        code = run_report([record, *REPO_PATHS], service.base_url, index)

    captured = capsys.readouterr()
    assert code == cli.EXIT_CONFIG
    assert captured.out == ""
    assert "保存报告失败" in captured.err
    assert "报告已保存" not in captured.err


def test_save_failure_with_out_is_reported(
    index: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """--out 指向不存在的目录时同样明确报错，不显示本次报告生成成功。"""
    missing = tmp_path / "not-a-directory" / "report.json"

    with fake_mindie(payload={"text": [answer()]}) as service:
        code = run_report([RECORD_002, "--out", str(missing)], service.base_url, index)

    captured = capsys.readouterr()
    assert code == cli.EXIT_CONFIG
    assert captured.out == ""
    assert "保存报告失败" in captured.err
    assert "报告已保存" not in captured.err


def test_extra_fields_in_the_answer_do_not_reach_the_report(
    index: Path, tmp_path: Path, capsys: pytest.CaptureFixture
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
        code, report = run_report_to_file([RECORD_002], service.base_url, index, tmp_path)

    captured = capsys.readouterr()
    assert code == cli.EXIT_OK
    assert "confidence" in captured.err
    assert report is not None
    assert "confidence" not in json.dumps(report, ensure_ascii=False)
    assert "confidence" not in captured.out


# --- 简述的措辞 -------------------------------------------------------------


def test_summary_separates_tool_conclusions_from_model_suggestions(
    index: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """没有候选原因不等于没有故障：简述要写明来源，不把空项读成设备正常。"""
    with fake_mindie(payload={"text": [answer()]}) as service:
        code, report = run_report_to_file([RECORD_002], service.base_url, index, tmp_path)

    shown = capsys.readouterr().out
    assert code == cli.EXIT_OK
    assert report is not None
    assert report["analysis"]["fault_state"] == "confirmed"
    assert report["analysis"]["candidate_causes"] == []

    assert "故障状态（程序按手册判据判定）：已确认" in shown
    assert "根因判定（程序给出）：未确定" in shown
    assert (
        "候选原因（模型给出的待验证推测，未经确认）："
        "本次未提出候选原因，根因尚未确定；不表示设备没有故障" in shown
    )


# --- 部分不支持与全部不支持 -------------------------------------------------


def test_partially_unsupported_record_is_still_analysed(
    index: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """有对象不支持、但也有对象能分析时照常分析，不支持项记进报告。"""
    with fake_mindie(payload={"text": [answer()]}) as service:
        code, report = run_report_to_file(
            [str(EXAMPLES / "REC-2026-0928-008.json")], service.base_url, index, tmp_path
        )

    captured = capsys.readouterr()
    assert code == cli.EXIT_OK, captured.err
    assert service.requests != []
    assert report is not None

    unsupported = report["analysis"]["unsupported"]
    assert [item["subject"] for item in unsupported] == [
        "CAN_A.ESR.FLTCONF 的取值",
        "检测对象 CAN_D",
        "寄存器原始值（OBS-2）",
    ]

    # 简述里不支持项与未判定都带上「不等于正常」的口径。
    assert "不支持（程序给出，本版没有判据、不等于正常，3 项）：" in captured.out
    assert "检测对象 CAN_D" in captured.out
    assert "故障状态（程序按手册判据判定）：未判定（不等于设备正常）" in captured.out


def test_record_with_only_unsupported_targets_exits_unsupported(
    index: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """全部对象都不支持时提前退出：没有工具结论，就不给模型凭空分析的机会。"""
    path = write_record(tmp_path / "unsupported-target.json", make_record(target="CAN_D"))

    with fake_mindie() as service:
        code = run_report([str(path)], service.base_url, index)

    captured = capsys.readouterr()
    assert code == cli.EXIT_UNSUPPORTED
    assert captured.out == ""
    assert "未调用模型" in captured.err
    assert service.requests == []


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
