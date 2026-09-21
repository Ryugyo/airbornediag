"""llm 子命令测试：输出、退出码与配置来源。

回答走标准输出，端点等诊断信息走标准错误，因此可以分别断言。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from airbornediag import cli
from fake_mindie import DEFAULT_ANSWER, fake_mindie, unused_port

CLI_MODULE = "airbornediag.cli"
ENV_PREFIX = "AIRBORNEDIAG_LLM_"


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
def isolated_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """清掉外部配置并切换工作目录，避免工程根目录下的 .env 影响断言。"""
    for name in list(os.environ):
        if name.startswith(ENV_PREFIX):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)


def test_success_prints_answer_to_stdout(capsys: pytest.CaptureFixture) -> None:
    with fake_mindie() as service:
        code = cli.main(["llm", "--prompt", "你好", "--base-url", service.base_url])

    captured = capsys.readouterr()
    assert code == cli.EXIT_OK
    assert captured.out.strip() == DEFAULT_ANSWER
    # 诊断信息不进标准输出
    assert DEFAULT_ANSWER not in captured.err
    assert "端点：{}".format(service.base_url) in captured.err
    assert "模型：Qwen2.5-1.5B" in captured.err
    assert "耗时：" in captured.err


def test_system_message_and_max_tokens_reach_the_request(capsys: pytest.CaptureFixture) -> None:
    with fake_mindie() as service:
        code = cli.main(
            [
                "llm",
                "--prompt",
                "你好",
                "--system",
                "你是诊断助手。",
                "--max-tokens",
                "64",
                "--base-url",
                service.base_url,
            ]
        )

    assert code == cli.EXIT_OK
    body = service.last_request()["json"]
    assert body["max_tokens"] == 64
    assert body["prompt"] == (
        "<|im_start|>system\n你是诊断助手。<|im_end|>\n"
        "<|im_start|>user\n你好<|im_end|>\n"
        "<|im_start|>assistant\n"
    )


def test_prompt_can_come_from_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdin", _FakeStdin(text="来自标准输入\n"))
    with fake_mindie() as service:
        code = cli.main(["llm", "--base-url", service.base_url])
    assert code == cli.EXIT_OK
    assert "来自标准输入" in service.last_request()["json"]["prompt"]


def test_missing_prompt_is_rejected(
    capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "stdin", _FakeStdin(tty=True))
    code = cli.main(["llm"])
    captured = capsys.readouterr()
    assert code == cli.EXIT_CONFIG
    assert "没有可发送的文本" in captured.err


def test_invalid_config_is_rejected(capsys: pytest.CaptureFixture) -> None:
    code = cli.main(["llm", "--prompt", "你好", "--base-url", "127.0.0.1:1025"])
    captured = capsys.readouterr()
    assert code == cli.EXIT_CONFIG
    assert "配置错误" in captured.err


def test_configuration_can_come_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with fake_mindie() as service:
        monkeypatch.setenv(ENV_PREFIX + "BASE_URL", service.base_url)
        monkeypatch.setenv(ENV_PREFIX + "MODEL", "Qwen2.5-1.5B-Instruct")
        code = cli.main(["llm", "--prompt", "你好"])
    assert code == cli.EXIT_OK
    assert service.last_request()["json"]["model"] == "Qwen2.5-1.5B-Instruct"


def test_configuration_can_come_from_env_file(tmp_path: Path) -> None:
    """工作目录下的 .env 无需任何参数即可生效。"""
    with fake_mindie() as service:
        (tmp_path / ".env").write_text(
            "{0}BASE_URL={1}\n{0}MAX_TOKENS=32\n".format(ENV_PREFIX, service.base_url),
            encoding="utf-8",
        )
        code = cli.main(["llm", "--prompt", "你好"])
    assert code == cli.EXIT_OK
    assert service.last_request()["json"]["max_tokens"] == 32


def test_connection_failure_has_its_own_exit_code(capsys: pytest.CaptureFixture) -> None:
    code = cli.main(
        [
            "llm",
            "--prompt",
            "你好",
            "--base-url",
            "http://127.0.0.1:{}".format(unused_port()),
            "--timeout",
            "5",
        ]
    )
    captured = capsys.readouterr()
    assert code == cli.EXIT_CONNECTION
    assert "连接失败" in captured.err
    assert captured.out == ""


def test_timeout_has_its_own_exit_code(capsys: pytest.CaptureFixture) -> None:
    with fake_mindie(delay=3.0) as service:
        code = cli.main(
            ["llm", "--prompt", "你好", "--base-url", service.base_url, "--timeout", "0.5"]
        )
    assert code == cli.EXIT_TIMEOUT
    assert "请求超时" in capsys.readouterr().err


def test_http_error_has_its_own_exit_code(capsys: pytest.CaptureFixture) -> None:
    with fake_mindie(payload={"error": "模型未加载"}, status=503) as service:
        code = cli.main(["llm", "--prompt", "你好", "--base-url", service.base_url])
    assert code == cli.EXIT_HTTP
    assert "HTTP 错误" in capsys.readouterr().err


def test_response_format_error_has_its_own_exit_code(capsys: pytest.CaptureFixture) -> None:
    with fake_mindie(raw_body="<html>不是 JSON</html>") as service:
        code = cli.main(["llm", "--prompt", "你好", "--base-url", service.base_url])
    assert code == cli.EXIT_RESPONSE
    assert "响应格式异常" in capsys.readouterr().err


def test_console_entry_point_end_to_end(tmp_path: Path) -> None:
    """经安装的命令行入口真实调用一次，确认打包后的入口也能走通。"""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    for name in list(env):
        if name.startswith(ENV_PREFIX):
            del env[name]

    with fake_mindie() as service:
        env[ENV_PREFIX + "BASE_URL"] = service.base_url
        result = subprocess.run(
            [sys.executable, "-m", CLI_MODULE, "llm", "--prompt", "你好"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            # 换到工程外目录，确保读取的是安装后的包而不是当前目录。
            cwd=str(tmp_path),
        )

    assert result.returncode == cli.EXIT_OK
    assert result.stdout.strip() == DEFAULT_ANSWER
    assert service.base_url in result.stderr
