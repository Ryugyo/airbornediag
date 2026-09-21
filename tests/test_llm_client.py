"""MindIE 客户端测试：请求格式、回答提取与失败处理。

用测试进程内的假服务走真实 HTTP 路径。客户端在失败时不返回任何替代回答，
因此这里逐个断言失败被如实上报为异常。
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from airbornediag.config import LLMConfig
from airbornediag.llm import (
    LLMConnectionError,
    LLMHTTPError,
    LLMResponseError,
    LLMTimeoutError,
    MindIEClient,
    extract_text,
)
from fake_mindie import DEFAULT_ANSWER, fake_mindie, unused_port

PROMPT = "<|im_start|>user\n你好<|im_end|>\n<|im_start|>assistant\n"


def client_for(service: Any, **kwargs: Any) -> MindIEClient:
    return MindIEClient(LLMConfig(base_url=service.base_url, **kwargs))


def test_request_matches_historical_script() -> None:
    with fake_mindie() as service:
        response = client_for(service).generate(PROMPT)

    request = service.last_request()
    assert request["path"] == "/generate"
    assert request["headers"]["content-type"] == "application/json"
    # 默认不发送认证头，与已验证的历史脚本一致。
    assert "authorization" not in request["headers"]
    assert request["json"] == {
        "prompt": PROMPT,
        "max_tokens": 1000,
        "stream": False,
        "model": "Qwen2.5-1.5B",
    }
    assert response.text == DEFAULT_ANSWER
    assert response.endpoint == service.base_url + "/generate"
    assert response.elapsed_seconds >= 0


def test_max_tokens_can_be_overridden_per_request() -> None:
    with fake_mindie() as service:
        client_for(service, max_tokens=64).generate(PROMPT, max_tokens=8)
    assert service.last_request()["json"]["max_tokens"] == 8


def test_api_key_is_sent_only_when_configured() -> None:
    with fake_mindie() as service:
        client_for(service, api_key="secret").generate(PROMPT)
        assert service.last_request()["headers"]["authorization"] == "Bearer secret"

    with fake_mindie() as service:
        client_for(service).generate(PROMPT)
        assert "authorization" not in service.last_request()["headers"]


@pytest.mark.parametrize(
    "payload, expected",
    [
        ({"text": ["回答"]}, "回答"),
        ({"text": "回答"}, "回答"),
        ({"text": ["回答", "被忽略的续写"]}, "回答"),
        ({"text": ["  回答  "]}, "回答"),
    ],
)
def test_answer_is_extracted(payload: Dict[str, Any], expected: str) -> None:
    with fake_mindie(payload=payload) as service:
        response = client_for(service).generate(PROMPT)
    assert response.text == expected


def test_echoed_prompt_and_next_turn_are_removed() -> None:
    # 历史脚本观察到的两种现象同时出现：回显输入提示、继续生成下一轮对话。
    echoed = (
        PROMPT
        + "总线关闭。<|im_end|>\n"
        + "<|im_start|>user\n再来一个<|im_end|>\n"
        + "<|im_start|>assistant\n"
    )
    with fake_mindie(payload={"text": [echoed]}) as service:
        response = client_for(service).generate(PROMPT)
    assert response.text == "总线关闭。"


@pytest.mark.parametrize(
    "payload, expected",
    [
        ([1, 2], "不是 JSON 对象"),
        ({"result": "没有 text"}, "缺少 text"),
        ({"text": []}, "空数组"),
        ({"text": 123}, "不是字符串"),
        ({"text": [{"a": 1}]}, "不是字符串"),
    ],
)
def test_unusable_responses_are_reported(payload: Any, expected: str) -> None:
    with pytest.raises(LLMResponseError) as error:
        extract_text(payload, PROMPT)
    assert expected in str(error.value)


def test_non_json_response_is_reported() -> None:
    with fake_mindie(raw_body="<html>502 Bad Gateway</html>") as service:
        with pytest.raises(LLMResponseError) as error:
            client_for(service).generate(PROMPT)
    assert "JSON" in str(error.value)


def test_http_error_reports_status_and_body() -> None:
    with fake_mindie(payload={"error": "模型未加载"}, status=503) as service:
        with pytest.raises(LLMHTTPError) as error:
            client_for(service).generate(PROMPT)
    assert error.value.status_code == 503
    assert "模型未加载" in error.value.body
    assert "503" in str(error.value)


def test_connection_failure_is_reported() -> None:
    config = LLMConfig(base_url="http://127.0.0.1:{}".format(unused_port()), timeout=5)
    with pytest.raises(LLMConnectionError):
        MindIEClient(config).generate(PROMPT)


def test_timeout_is_reported() -> None:
    with fake_mindie(delay=3.0) as service:
        with pytest.raises(LLMTimeoutError):
            client_for(service, timeout=0.5).generate(PROMPT)
