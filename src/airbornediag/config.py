"""运行期配置加载。

当前只包含模型服务（MindIE）的配置。取值优先级从高到低：

1. 调用方显式传入的参数（命令行开关）；
2. 进程环境变量；
3. 工作目录下的 .env 文件（本机开发用，已被 .gitignore 排除）；
4. 本模块的默认值。

真实凭据只通过环境变量或 .env 提供，不写入源码，也不提交 Git。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Mapping, Optional
from urllib.parse import urlsplit

# 环境变量前缀，避免与系统或其他工具的变量冲突。
ENV_PREFIX = "AIRBORNEDIAG_LLM_"

# 默认值取自 RDC300I 上已验证可用的历史调用脚本，不是估计值。
# 服务地址与模型名可在部署变化时覆盖；接口路径见 llm/client.py 的说明，
# 当前只实现了 /generate 的请求与响应格式。
DEFAULT_BASE_URL = "http://127.0.0.1:1025"
DEFAULT_GENERATE_PATH = "/generate"
DEFAULT_MODEL = "Qwen2.5-1.5B"
DEFAULT_MAX_TOKENS = 1000
DEFAULT_TIMEOUT = 300.0

ENV_FILE_NAME = ".env"


class ConfigError(Exception):
    """配置缺失或取值非法。"""


@dataclass(frozen=True)
class LLMConfig:
    """模型服务调用配置。"""

    base_url: str = DEFAULT_BASE_URL
    generate_path: str = DEFAULT_GENERATE_PATH
    model: str = DEFAULT_MODEL
    max_tokens: int = DEFAULT_MAX_TOKENS
    timeout: float = DEFAULT_TIMEOUT
    api_key: Optional[str] = None

    @property
    def endpoint(self) -> str:
        """完整的请求地址。"""
        return self.base_url.rstrip("/") + self.generate_path


def parse_env_file(path: Path) -> Dict[str, str]:
    """解析 .env 文件，返回键值对。

    只支持 KEY=VALUE 一种形式，不做变量插值。无法识别的行按注释或空行处理，
    不报错也不猜测，避免把无关内容当成配置。文件不存在时返回空字典。
    """
    values: Dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return values
    except (OSError, UnicodeDecodeError) as error:
        raise ConfigError("读取配置文件 {} 失败：{}".format(path, error))

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export ") :].strip()
        key, separator, value = stripped.partition("=")
        key = key.strip()
        if not separator or not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        values[key] = value
    return values


def _pick_int(name: str, raw: Optional[str], default: int) -> int:
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        raise ConfigError("{} 必须是整数，当前为 {!r}".format(name, raw))


def _pick_float(name: str, raw: Optional[str], default: float) -> float:
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip())
    except ValueError:
        raise ConfigError("{} 必须是数字，当前为 {!r}".format(name, raw))


def _validate(config: LLMConfig) -> LLMConfig:
    """校验配置取值，避免把错误留给请求阶段。"""
    parts = urlsplit(config.base_url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ConfigError(
            "服务地址必须是 http:// 或 https:// 开头的完整地址，当前为 {!r}".format(config.base_url)
        )

    path = config.generate_path
    if not path.startswith("/"):
        raise ConfigError("接口路径必须以 / 开头，当前为 {!r}".format(path))
    if "://" in path or "?" in path or "#" in path:
        raise ConfigError("接口路径只能是路径部分，不能包含协议、查询串或片段，当前为 {!r}".format(path))

    if not config.model.strip():
        raise ConfigError("模型名不能为空")
    if config.max_tokens <= 0:
        raise ConfigError("最大生成 token 数必须是正整数，当前为 {!r}".format(config.max_tokens))
    if config.timeout <= 0:
        raise ConfigError("超时必须是正数秒，当前为 {!r}".format(config.timeout))
    return config


def load_llm_config(
    *,
    env_file: Optional[Path] = None,
    environ: Optional[Mapping[str, str]] = None,
    base_url: Optional[str] = None,
    generate_path: Optional[str] = None,
    model: Optional[str] = None,
    max_tokens: Optional[int] = None,
    timeout: Optional[float] = None,
    api_key: Optional[str] = None,
) -> LLMConfig:
    """按优先级组装模型服务配置。

    传入 None 表示该层未提供取值，继续向下取；空字符串对 api_key 表示未配置。
    """
    if environ is None:
        environ = os.environ
    if env_file is None:
        env_file = Path(ENV_FILE_NAME)

    merged: Dict[str, str] = parse_env_file(env_file)
    # 环境变量优先于 .env：只覆盖同名键，其余键不受影响。
    merged.update(environ)

    def pick(name: str) -> Optional[str]:
        return merged.get(ENV_PREFIX + name)

    def text(value: Optional[str], env_name: str, default: str) -> str:
        if value is not None:
            return value
        raw = pick(env_name)
        return default if raw is None else raw.strip()

    raw_key = api_key if api_key is not None else pick("API_KEY")
    key = raw_key.strip() if raw_key else ""

    config = LLMConfig(
        base_url=text(base_url, "BASE_URL", DEFAULT_BASE_URL),
        generate_path=text(generate_path, "GENERATE_PATH", DEFAULT_GENERATE_PATH),
        model=text(model, "MODEL", DEFAULT_MODEL),
        max_tokens=(
            max_tokens
            if max_tokens is not None
            else _pick_int(ENV_PREFIX + "MAX_TOKENS", pick("MAX_TOKENS"), DEFAULT_MAX_TOKENS)
        ),
        timeout=(
            timeout
            if timeout is not None
            else _pick_float(ENV_PREFIX + "TIMEOUT", pick("TIMEOUT"), DEFAULT_TIMEOUT)
        ),
        api_key=key or None,
    )
    return _validate(config)
