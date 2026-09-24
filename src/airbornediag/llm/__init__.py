"""模型服务调用。

当前提供 MindIE /generate 接口的非流式调用、回答文本提取、ChatML 提示词构造，
以及诊断提示词的构造与模型回答的解析，不含知识检索、诊断规则与多轮会话。
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
from airbornediag.llm.diagnosis import (
    PROMPT_CHAR_LIMIT,
    AnalysisOutputError,
    CandidateCause,
    DiagnosisAnalysis,
    DiagnosisPromptError,
    build_diagnosis_prompt,
    parse_analysis,
)
from airbornediag.llm.prompt import build_chat_prompt

__all__ = [
    "PROMPT_CHAR_LIMIT",
    "AnalysisOutputError",
    "CandidateCause",
    "DiagnosisAnalysis",
    "DiagnosisPromptError",
    "LLMConnectionError",
    "LLMError",
    "LLMHTTPError",
    "LLMResponse",
    "LLMResponseError",
    "LLMTimeoutError",
    "MindIEClient",
    "build_chat_prompt",
    "build_diagnosis_prompt",
    "extract_text",
    "parse_analysis",
]
