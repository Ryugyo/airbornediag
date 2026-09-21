"""MindIE 文本生成接口调用。

只实现非流式单轮请求：发送一段文本，取回回答。**不重试，不在失败时返回模拟
或降级回答**——任何失败都通过异常上报，由调用方决定如何处理。因此调用失败时
不会得到一个看似正常的回答。

接口依据 RDC300I 上已验证可用的历史脚本（mindie_chat.py、mindie_load_test.py）：

    POST http://127.0.0.1:1025/generate
    Content-Type: application/json
    {"prompt": "<ChatML 文本>", "max_tokens": 1000, "stream": false, "model": "..."}
    响应 {"text": "<回答>"}，text 也可能是字符串数组

历史脚本未发送认证头。这里实现为可选的 Authorization: Bearer，默认不发送，
即默认行为与已验证脚本一致；该认证分支尚未在板端验证。

/generate 是 MindIE 原生文本生成接口，不是 OpenAI 兼容的 /v1/chat/completions，
两者的请求体与响应结构都不同，改接口路径并不能切换协议。
"""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from airbornediag.config import LLMConfig
from airbornediag.llm.prompt import IM_END

# 错误响应体只保留开头，避免把整页 HTML 或模型输出倒进日志。
BODY_PREVIEW_LIMIT = 500


class LLMError(Exception):
    """模型服务调用失败的基类。"""


class LLMConnectionError(LLMError):
    """无法连接到服务。"""


class LLMTimeoutError(LLMError):
    """请求超时。"""


class LLMHTTPError(LLMError):
    """服务返回了非 2xx 状态码。"""

    def __init__(self, status_code: int, body: str, endpoint: str) -> None:
        self.status_code = status_code
        self.body = body
        self.endpoint = endpoint
        message = "{} 返回 HTTP {} {}".format(endpoint, status_code, _reason(status_code))
        if body:
            message += "，响应体：{}".format(body)
        super().__init__(message)


class LLMResponseError(LLMError):
    """响应不是预期结构，无法取出回答。"""


@dataclass(frozen=True)
class LLMResponse:
    """一次成功的调用结果。"""

    text: str
    model: str
    endpoint: str
    elapsed_seconds: float
    raw: Mapping[str, Any]


def _reason(status_code: int) -> str:
    """把常见的 HTTP 状态码翻成便于排查的中文说明。"""
    hints = {
        400: "请求格式未被接受",
        401: "未通过认证",
        403: "无权访问",
        404: "接口路径不存在",
        405: "请求方法不被支持",
        413: "请求体过大",
        500: "服务内部错误",
        502: "网关错误",
        503: "服务不可用",
        504: "网关超时",
    }
    hint = hints.get(status_code)
    return "（{}）".format(hint) if hint else ""


def _truncate(text: str) -> str:
    if len(text) <= BODY_PREVIEW_LIMIT:
        return text
    return text[:BODY_PREVIEW_LIMIT] + "...（已截断）"


def _read_error_body(error: urllib.error.HTTPError) -> str:
    """读取错误响应体，读不到时返回空串而不是让异常覆盖原始错误。"""
    try:
        return _truncate(error.read().decode("utf-8", errors="replace").strip())
    except Exception:  # noqa: BLE001 - 响应体只是辅助信息，不应影响错误上报
        return ""


def _decode_json(raw: bytes) -> Any:
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise LLMResponseError("响应不是有效的 UTF-8 文本：{}".format(error))
    try:
        return json.loads(decoded)
    except json.JSONDecodeError as error:
        raise LLMResponseError(
            "响应不是有效的 JSON：{}；响应开头 {!r}".format(error, decoded[:200])
        )


def extract_text(payload: Any, prompt: str) -> str:
    """从响应中取出回答文本。

    处理两种历史脚本中实际观察到的现象：接口可能把输入提示一并回显，且可能
    在回答之后继续生成下一轮对话。与历史脚本的差别只有一处：历史脚本把空数组
    当作空回答，这里判为响应格式异常——取不到回答与模型回答为空是两回事，
    不应混为一谈。
    """
    if not isinstance(payload, dict):
        raise LLMResponseError("响应不是 JSON 对象，实际为 {}".format(type(payload).__name__))

    if "text" not in payload:
        fields = sorted(str(key) for key in payload)
        raise LLMResponseError("响应缺少 text 字段，实际字段：{}".format(fields or "无"))

    text = payload["text"]
    if isinstance(text, list):
        if not text:
            raise LLMResponseError("响应中的 text 为空数组，取不到回答")
        text = text[0]
    if not isinstance(text, str):
        raise LLMResponseError("响应中的 text 不是字符串，实际为 {}".format(type(text).__name__))

    if text.startswith(prompt):
        text = text[len(prompt) :]
    # 截断到本轮结束标记，丢掉模型自行续写的下一轮对话。
    text = text.split(IM_END, 1)[0]
    return text.strip()


class MindIEClient:
    """MindIE /generate 接口的轻量客户端。"""

    def __init__(self, config: LLMConfig) -> None:
        self._config = config

    @property
    def config(self) -> LLMConfig:
        return self._config

    def build_payload(self, prompt: str, max_tokens: Optional[int] = None) -> Mapping[str, Any]:
        """构造请求体，字段与历史脚本一致。"""
        return {
            "prompt": prompt,
            "max_tokens": self._config.max_tokens if max_tokens is None else max_tokens,
            "stream": False,
            "model": self._config.model,
        }

    def generate(self, prompt: str, max_tokens: Optional[int] = None) -> LLMResponse:
        """发送一次非流式请求并返回回答。

        失败时抛出 LLMConnectionError、LLMTimeoutError、LLMHTTPError 或
        LLMResponseError，不会返回任何替代内容。
        """
        endpoint = self._config.endpoint
        body = json.dumps(self.build_payload(prompt, max_tokens), ensure_ascii=False).encode("utf-8")
        # Content-Type 与历史脚本保持完全一致，不额外追加 charset：请求体已按
        # UTF-8 编码，而 JSON 的默认编码就是 UTF-8。
        headers = {"Content-Type": "application/json"}
        if self._config.api_key:
            headers["Authorization"] = "Bearer " + self._config.api_key

        request = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")

        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=self._config.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            # HTTPError 是 URLError 的子类，必须先于 URLError 捕获。
            raise LLMHTTPError(error.code, _read_error_body(error), endpoint) from error
        except urllib.error.URLError as error:
            reason = getattr(error, "reason", error)
            if isinstance(reason, (socket.timeout, TimeoutError)):
                raise LLMTimeoutError(
                    "请求 {} 超过 {:.1f} s 未返回".format(endpoint, self._config.timeout)
                ) from error
            raise LLMConnectionError("无法连接 {}：{}".format(endpoint, reason)) from error
        except (socket.timeout, TimeoutError) as error:
            # Python 3.9 的 socket.timeout 与 TimeoutError 是两个类，两者都要接。
            raise LLMTimeoutError(
                "请求 {} 超过 {:.1f} s 未返回".format(endpoint, self._config.timeout)
            ) from error
        except OSError as error:
            raise LLMConnectionError("无法连接 {}：{}".format(endpoint, error)) from error
        elapsed = time.monotonic() - started

        payload = _decode_json(raw)
        return LLMResponse(
            text=extract_text(payload, prompt),
            model=self._config.model,
            endpoint=endpoint,
            elapsed_seconds=elapsed,
            raw=payload,
        )
