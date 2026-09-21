"""Qwen2.5 ChatML 提示词测试。

断言的是历史脚本中逐字使用的格式：改动这里等于改动发给模型的请求格式。
"""

from __future__ import annotations

from airbornediag.llm.prompt import build_chat_prompt


def test_prompt_matches_historical_script_without_system_message() -> None:
    expected = "<|im_start|>user\n你好<|im_end|>\n<|im_start|>assistant\n"
    assert build_chat_prompt("你好") == expected
    # 不传 system 与传空 system 等价，与历史脚本的默认行为一致。
    assert build_chat_prompt("你好", None) == expected
    assert build_chat_prompt("你好", "") == expected


def test_prompt_includes_system_message_when_given() -> None:
    assert build_chat_prompt("你好", "你是诊断助手。") == (
        "<|im_start|>system\n你是诊断助手。<|im_end|>\n"
        "<|im_start|>user\n你好<|im_end|>\n"
        "<|im_start|>assistant\n"
    )
