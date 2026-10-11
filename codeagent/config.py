"""配置加载：命令行参数 > 环境变量 > .env 文件 > 默认值。

刻意不引入 ``python-dotenv`` / ``pydantic``，保持"零第三方依赖"，
让评审者 ``git clone`` 后可以直接运行。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Mapping, Sequence

from .errors import ConfigError

#: 允许从环境变量读取 API Key 的名字，按优先级排列（方便直接复用已有变量）。
API_KEY_ENV_NAMES = (
    "CODEAGENT_API_KEY",
    "OPENAI_API_KEY",
    "DEEPSEEK_API_KEY",
    "DASHSCOPE_API_KEY",
    "MOONSHOT_API_KEY",
)

BASE_URL_ENV_NAMES = ("CODEAGENT_BASE_URL", "OPENAI_BASE_URL", "OPENAI_API_BASE")
MODEL_ENV_NAMES = ("CODEAGENT_MODEL", "OPENAI_MODEL")

DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-chat"


def parse_dotenv(path: Path) -> dict[str, str]:
    """解析一个极简的 ``.env`` 文件。

    支持 ``KEY=VALUE``、``#`` 注释、``export`` 前缀、以及单/双引号包裹的值。
    解析失败的行会被静默忽略（配置文件不应该让程序崩溃）。
    """
    values: dict[str, str] = {}
    if not path.is_file():
        return values

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return values

    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def _first_present(
    sources: Sequence[tuple[str, Mapping[str, Any]]], names: tuple[str, ...]
) -> tuple[str | None, str | None]:
    """按 (来源优先级, 变量名优先级) 返回第一个非空值及其来源标签。

    ``sources`` 是 ``(标签, 映射)`` 序列；标签用于回填 ``Settings.sources``，
    方便 ``--show-config`` 告诉用户"这个值到底从哪来的"。
    对 ``env`` 这一来源，回填的是具体变量名（例如 ``OPENAI_API_KEY``）。
    """
    for label, source in sources:
        for name in names:
            value = source.get(name)
            if value not in (None, ""):
                return str(value), (name if label == "env" else label)
    return None, None


@dataclass
class Settings:
    """一次会话的全部可调参数。"""

    # --- 模型接入 ---
    api_key: str = ""
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    temperature: float = 0.2
    max_tokens: int = 0  # 0 表示不发送该字段，交给服务端默认值
    timeout: float = 120.0
    max_retries: int = 3
    stream: bool = True

    # --- Agent 行为 ---
    max_steps: int = 12
    max_tool_output: int = 12_000
    context_token_budget: int = 60_000
    allow_write: bool = True
    command_timeout: float = 60.0

    # --- 运行环境 ---
    workspace: Path = field(default_factory=Path.cwd)
    verbose: bool = False
    #: ``None`` 表示自动判断（stdout 是否 TTY）。
    color: bool | None = None
    mock: bool = False

    #: 记录关键配置的来源，便于 `--show-config` 排查问题。
    sources: dict[str, str] = field(default_factory=dict)

    # ------------------------------------------------------------------ #
    @classmethod
    def load(
        cls,
        cli_args: Mapping[str, Any] | None = None,
        env: Mapping[str, str] | None = None,
        dotenv_path: Path | None = None,
        cwd: Path | None = None,
    ) -> "Settings":
        """从多个来源合并出最终配置。

        Args:
            cli_args: 命令行显式指定的值；值为 ``None`` 的键会被忽略。
            env: 环境变量映射，默认取 ``os.environ``。
            dotenv_path: ``.env`` 文件路径，默认取当前目录下的 ``.env``。
            cwd: 工作目录基准，默认取进程当前目录。
        """
        cwd = (cwd or Path.cwd()).resolve()
        env = dict(os.environ if env is None else env)
        cli = {k: v for k, v in (cli_args or {}).items() if v is not None}
        file_values = parse_dotenv(dotenv_path or (cwd / ".env"))

        settings = cls(workspace=cwd)
        settings.sources["workspace"] = "cwd"

        # 三个「模型接入」字段的来源优先级：命令行 > 环境变量 > .env
        ordered_sources = (("cli", cli), ("env", env), (".env", file_values))

        key, key_src = _first_present(ordered_sources, API_KEY_ENV_NAMES + ("api_key",))
        if key:
            settings.api_key = key
            settings.sources["api_key"] = key_src or "cli"

        base_url, base_src = _first_present(
            ordered_sources, BASE_URL_ENV_NAMES + ("base_url",)
        )
        if base_url:
            settings.base_url = base_url
            settings.sources["base_url"] = base_src or "cli"

        model, model_src = _first_present(ordered_sources, MODEL_ENV_NAMES + ("model",))
        if model:
            settings.model = model
            settings.sources["model"] = model_src or "cli"

        # 其余标量字段：命令行 > .env（约定变量名 CODEAGENT_<大写字段名>）
        for f in fields(cls):
            name = f.name
            if name in {"api_key", "base_url", "model", "sources"}:
                continue
            if name in cli:
                setattr(settings, name, cli[name])
                settings.sources[name] = "cli"
                continue
            env_name = f"CODEAGENT_{name.upper()}"
            if env_name in env and env[env_name] != "":
                setattr(settings, name, _coerce(env[env_name], f.type))
                settings.sources[name] = env_name
            elif env_name in file_values and file_values[env_name] != "":
                setattr(settings, name, _coerce(file_values[env_name], f.type))
                settings.sources[name] = ".env"

        settings.workspace = Path(str(settings.workspace)).expanduser().resolve()
        return settings

    # ------------------------------------------------------------------ #
    def validate(self) -> None:
        """启动前校验，尽早失败并给出可操作的提示。"""
        problems: list[str] = []
        if not self.mock and not self.api_key:
            problems.append(
                "缺少 API Key：请设置环境变量 CODEAGENT_API_KEY（或 OPENAI_API_KEY），"
                "或复制 .env.example 为 .env 并填写。若只想离线体验，请加 --mock。"
            )
        if not self.base_url.startswith(("http://", "https://")):
            problems.append(f"base_url 必须以 http(s):// 开头，当前为 {self.base_url!r}")
        if not self.model:
            problems.append("model 不能为空")
        if self.max_steps < 1:
            problems.append("max_steps 必须 >= 1")
        if not (0.0 <= float(self.temperature) <= 2.0):
            problems.append("temperature 必须位于 [0, 2]")
        if not self.workspace.is_dir():
            problems.append(f"工作目录不存在或不是目录：{self.workspace}")
        if problems:
            raise ConfigError("配置校验失败：\n  - " + "\n  - ".join(problems))

    # ------------------------------------------------------------------ #
    def redacted(self) -> dict[str, Any]:
        """返回可安全打印的配置快照（屏蔽密钥）。

        ``sources`` 是纯诊断用的内部映射，已经以「来源列」的形式单独展示，
        因此这里排除掉，避免在 ``--show-config`` 里刷出一大串字典。
        """
        snapshot: dict[str, Any] = {}
        for f in fields(self):
            if f.name == "sources":
                continue
            value = getattr(self, f.name)
            if f.name == "api_key":
                value = _mask(value)
            elif f.name == "workspace":
                value = str(value)
            snapshot[f.name] = value
        return snapshot

    @property
    def chat_endpoint(self) -> str:
        """拼出 ``/chat/completions`` 端点，兼容用户填写带/不带 ``/v1`` 的写法。"""
        base = self.base_url.rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return f"{base}/chat/completions"


def _coerce(raw: str, type_hint: Any) -> Any:
    """把字符串配置项转换成字段声明的类型。"""
    text = raw.strip()
    hint = type_hint if isinstance(type_hint, str) else getattr(type_hint, "__name__", "")
    try:
        if hint == "bool":
            return text.lower() in {"1", "true", "yes", "y", "on"}
        if hint == "int":
            return int(text)
        if hint == "float":
            return float(text)
        if hint == "Path":
            return Path(text)
    except ValueError as exc:
        raise ConfigError(f"无法把 {raw!r} 解析为 {hint}") from exc
    return text


def _mask(secret: str) -> str:
    if not secret:
        return "<未设置>"
    if len(secret) <= 8:
        return "*" * len(secret)
    return f"{secret[:4]}{'*' * 6}{secret[-4:]}"


def default_dotenv_path(cwd: Path | None = None) -> Path:
    """`.env` 查找顺序：当前目录 → 项目根目录。"""
    cwd = cwd or Path.cwd()
    if (cwd / ".env").is_file():
        return cwd / ".env"
    project_root = Path(__file__).resolve().parent.parent
    return project_root / ".env"


__all__ = [
    "Settings",
    "ConfigError",
    "parse_dotenv",
    "default_dotenv_path",
    "DEFAULT_BASE_URL",
    "DEFAULT_MODEL",
]
