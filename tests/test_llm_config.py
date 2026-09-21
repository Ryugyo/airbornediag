"""模型服务配置测试：默认值、优先级与取值校验。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import pytest

from airbornediag.config import ConfigError, LLMConfig, load_llm_config

# 指向一个不存在的文件，确保测试不受工作目录下真实 .env 的影响。
NO_ENV_FILE = Path("__不存在的配置文件__.env")


def test_defaults_match_historical_script() -> None:
    config = load_llm_config(env_file=NO_ENV_FILE, environ={})
    assert config.base_url == "http://127.0.0.1:1025"
    assert config.generate_path == "/generate"
    assert config.model == "Qwen2.5-1.5B"
    assert config.max_tokens == 1000
    assert config.timeout == 300.0
    assert config.api_key is None
    assert config.endpoint == "http://127.0.0.1:1025/generate"


def test_endpoint_tolerates_trailing_slash() -> None:
    assert LLMConfig(base_url="http://127.0.0.1:1025/").endpoint == "http://127.0.0.1:1025/generate"


def test_environment_variables_override_defaults() -> None:
    config = load_llm_config(
        env_file=NO_ENV_FILE,
        environ={
            "AIRBORNEDIAG_LLM_BASE_URL": "http://192.168.150.1:1025",
            "AIRBORNEDIAG_LLM_GENERATE_PATH": "/generate",
            "AIRBORNEDIAG_LLM_MODEL": "Qwen2.5-1.5B-Instruct",
            "AIRBORNEDIAG_LLM_MAX_TOKENS": "256",
            "AIRBORNEDIAG_LLM_TIMEOUT": "30",
            "AIRBORNEDIAG_LLM_API_KEY": "secret-value",
        },
    )
    assert config.base_url == "http://192.168.150.1:1025"
    assert config.model == "Qwen2.5-1.5B-Instruct"
    assert config.max_tokens == 256
    assert config.timeout == 30.0
    assert config.api_key == "secret-value"


def test_env_file_used_when_environment_is_absent(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# 注释行\n"
        "\n"
        "AIRBORNEDIAG_LLM_BASE_URL=http://10.0.0.2:1025\n"
        "export AIRBORNEDIAG_LLM_MODEL='Qwen2.5-1.5B'\n"
        "与工程无关的项=1\n",
        encoding="utf-8",
    )
    config = load_llm_config(env_file=env_file, environ={})
    assert config.base_url == "http://10.0.0.2:1025"
    assert config.model == "Qwen2.5-1.5B"
    # 未配置的项仍是默认值
    assert config.max_tokens == 1000


def test_environment_wins_over_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("AIRBORNEDIAG_LLM_BASE_URL=http://10.0.0.2:1025\n", encoding="utf-8")
    config = load_llm_config(
        env_file=env_file,
        environ={"AIRBORNEDIAG_LLM_BASE_URL": "http://10.0.0.3:1025"},
    )
    assert config.base_url == "http://10.0.0.3:1025"


def test_explicit_argument_wins_over_environment() -> None:
    config = load_llm_config(
        env_file=NO_ENV_FILE,
        environ={
            "AIRBORNEDIAG_LLM_BASE_URL": "http://10.0.0.3:1025",
            "AIRBORNEDIAG_LLM_MAX_TOKENS": "256",
        },
        base_url="http://127.0.0.1:9999",
        max_tokens=64,
    )
    assert config.base_url == "http://127.0.0.1:9999"
    assert config.max_tokens == 64


@pytest.mark.parametrize(
    "override, expected",
    [
        ({"base_url": "127.0.0.1:1025"}, "服务地址"),
        ({"base_url": "ftp://127.0.0.1:1025"}, "服务地址"),
        ({"generate_path": "generate"}, "接口路径"),
        ({"generate_path": "/generate?x=1"}, "接口路径"),
        ({"model": "   "}, "模型名"),
        ({"max_tokens": 0}, "token"),
        ({"timeout": 0}, "超时"),
    ],
)
def test_invalid_values_are_rejected(override: Dict[str, Any], expected: str) -> None:
    with pytest.raises(ConfigError) as error:
        load_llm_config(env_file=NO_ENV_FILE, environ={}, **override)
    assert expected in str(error.value)


def test_invalid_numeric_environment_variable_is_rejected() -> None:
    with pytest.raises(ConfigError) as error:
        load_llm_config(env_file=NO_ENV_FILE, environ={"AIRBORNEDIAG_LLM_TIMEOUT": "很久"})
    assert "AIRBORNEDIAG_LLM_TIMEOUT" in str(error.value)


def test_empty_api_key_means_no_authentication() -> None:
    config = load_llm_config(env_file=NO_ENV_FILE, environ={"AIRBORNEDIAG_LLM_API_KEY": "  "})
    assert config.api_key is None
