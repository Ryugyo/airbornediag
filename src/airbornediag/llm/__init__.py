"""模型服务调用。

当前只提供 MindIE /generate 接口的非流式调用、回答文本提取与提示词构造，
不含知识检索、诊断规则与多轮会话。
"""

from __future__ import annotations

from airbornediag.llm.client import (
    LLMConnectionError,
    LLMError,
    LLMHTTPError,
    LLMResponse,
    LLMResponseError,
    LLMTimeoutError,
    MindIEClient,
    extract_text,
)
from airbornediag.llm.prompt import build_chat_prompt

__all__ = [
    "LLMConnectionError",
    "LLMError",
    "LLMHTTPError",
    "LLMResponse",
    "LLMResponseError",
    "LLMTimeoutError",
    "MindIEClient",
    "build_chat_prompt",
    "extract_text",
]
