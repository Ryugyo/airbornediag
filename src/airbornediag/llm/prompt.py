"""Qwen2.5 的 ChatML 提示词构造。

MindIE 的 /generate 接口接收原始文本，不负责套用对话模板，因此由调用方显式
构造 Qwen2.5 使用的 ChatML 格式。本模块的格式与 RDC300I 上已验证可用的历史
脚本逐字一致，不是按文档推测的写法。

单轮、无工具调用。诊断提示词属于后续任务，不在本模块内。
"""

from __future__ import annotations

from typing import List, Optional

IM_START = "<|im_start|>"
IM_END = "<|im_end|>"

# 生成起点：模型从这里续写，回答紧跟在后面。
ASSISTANT_PREFIX = IM_START + "assistant\n"


def build_chat_prompt(user_message: str, system_message: Optional[str] = None) -> str:
    """构造单轮对话的提示文本。

    system_message 为 None 或空串时不输出 system 段，与历史脚本的默认行为一致。
    """
    parts: List[str] = []
    if system_message:
        parts.append("{0}system\n{1}{2}\n".format(IM_START, system_message, IM_END))
    parts.append("{0}user\n{1}{2}\n".format(IM_START, user_message, IM_END))
    parts.append(ASSISTANT_PREFIX)
    return "".join(parts)
