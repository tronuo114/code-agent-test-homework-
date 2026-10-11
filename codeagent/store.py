"""绑定信息持久化：把 API Key / 端点 / 模型存到 exe 同目录的 ``config.json``。

设计取舍：

- **绿色便携**：配置跟着程序走，整个文件夹拷到别的机器不用重新绑定。
- **写在程序目录可能失败**（比如被放到 ``Program Files`` 下，普通用户没有写权限），
  因此 :meth:`ConfigStore.save` 在失败时会自动降级到 ``%USERPROFILE%\\.codereview\\``，
  并把实际位置回报给调用方用于提示用户 —— 宁可换个地方存，也不要让用户白输一次密钥。
- **明文存储**：这是有意的取舍。要做加密就得引入密钥管理，反而更不安全也不好审计；
  改为在保存成功时明确提示"密钥以明文保存，不要提交到版本库"，
  并在 ``.gitignore`` 里忽略 ``config.json``。
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-chat"

#: 配置文件在程序目录下的文件名。
CONFIG_FILENAME = "config.json"

#: 降级目录（程序目录不可写时使用）。
FALLBACK_DIRNAME = ".codereview"


def app_dir() -> Path:
    """返回程序所在目录。

    - 打包成 exe（PyInstaller 等）时是 exe 所在目录；
    - 直接跑源码时是 ``main.py`` 所在目录（即 ``codeagent/`` 的上一级）。
    """
    if getattr(sys, "frozen", False):  # pragma: no cover - 仅打包后走到
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def fallback_config_path() -> Path:
    return Path.home() / FALLBACK_DIRNAME / CONFIG_FILENAME


@dataclass
class Binding:
    """一份绑定的接入配置。"""

    api_key: str = ""
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    temperature: float = 0.2
    #: 单次审查最多多少步推理-工具循环。审查比问答更费步数，默认给大一些。
    max_steps: int = 25
    #: 最近一次校验结果，用于 ``config`` 命令展示。
    verified: bool = False
    verified_at: str = ""
    verified_note: str = ""
    saved_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    # ------------------------------------------------------------------ #
    @property
    def bound(self) -> bool:
        return bool(self.api_key.strip())

    def masked_key(self) -> str:
        key = self.api_key.strip()
        if not key:
            return "<未绑定>"
        if len(key) <= 10:
            return "*" * len(key)
        return f"{key[:6]}{'*' * 8}{key[-4:]}"

    def chat_endpoint(self) -> str:
        base = self.base_url.rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return f"{base}/chat/completions"

    # ------------------------------------------------------------------ #
    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["_comment"] = (
            "本文件保存 CodeReview 的绑定信息，含明文 API Key。"
            "请勿提交到版本库或分享给他人。"
        )
        return data

    @classmethod
    def from_json(cls, raw: Any) -> "Binding":
        if not isinstance(raw, dict):
            raise ValueError("配置根节点必须是 JSON 对象")
        known = {f for f in cls.__dataclass_fields__}
        payload = {k: v for k, v in raw.items() if k in known and k != "saved_at"}
        binding = cls(**payload)
        if raw.get("saved_at"):
            binding.saved_at = str(raw["saved_at"])
        return binding


class ConfigStore:
    """读写 ``config.json``，并在必要时降级到用户目录。"""

    def __init__(self, path: Path | None = None) -> None:
        self._explicit_path = path

    # ------------------------------------------------------------------ #
    @property
    def primary_path(self) -> Path:
        return self._explicit_path or (app_dir() / CONFIG_FILENAME)

    def path(self) -> Path:
        """返回实际生效的配置路径。

        优先用程序目录；只有在那里**已经存在**或**可以创建**时才用它，
        否则用用户目录的降级位置。
        """
        if self._explicit_path is not None:
            return self._explicit_path
        primary = self.primary_path
        if primary.exists():
            return primary
        if _dir_writable(primary.parent):
            return primary
        return fallback_config_path()

    # ------------------------------------------------------------------ #
    def load(self) -> Binding:
        """读取绑定信息；文件缺失或损坏时返回空绑定并给出提示。"""
        path = self.path()
        if not path.is_file():
            return Binding()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return Binding.from_json(raw)
        except Exception as exc:
            print(f"⚠ 配置文件无法解析，已按未绑定处理：{path}\n  原因：{exc}")
            return Binding()

    def save(self, binding: Binding) -> tuple[bool, Path, str]:
        """保存绑定信息。

        Returns:
            ``(是否成功, 实际写入的路径, 失败原因)``。失败时原因可直接展示给用户。
        """
        if self._explicit_path is not None:
            candidates = [self._explicit_path]
        else:
            candidates = [app_dir() / CONFIG_FILENAME, fallback_config_path()]

        binding.saved_at = datetime.now().isoformat(timespec="seconds")
        payload = json.dumps(binding.to_json(), ensure_ascii=False, indent=2)

        last_error = ""
        for candidate in candidates:
            try:
                candidate.parent.mkdir(parents=True, exist_ok=True)
                candidate.write_text(payload + "\n", encoding="utf-8")
                return True, candidate, ""
            except OSError as exc:
                last_error = f"{candidate}：{exc}"
                continue
        return False, candidates[-1], last_error or "未知错误"

    def clear(self) -> bool:
        """删除配置文件（解除绑定）。"""
        path = self.path()
        try:
            if path.is_file():
                path.unlink()
            return True
        except OSError:
            return False


def _dir_writable(directory: Path) -> bool:
    """实际尝试在目录里创建并删除一个临时文件，判断是否可写。"""
    probe = directory / ".codereview-write-probe"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe.write_text("x", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def config_precedence_hint() -> str:
    """给 README / config 命令用的一句话说明。"""
    return (
        f"配置查找顺序：{app_dir() / CONFIG_FILENAME}"
        f" → {fallback_config_path()}"
        f"（取第一个可写或已存在的位置）"
    )


__all__ = [
    "Binding",
    "ConfigStore",
    "app_dir",
    "fallback_config_path",
    "config_precedence_hint",
    "DEFAULT_BASE_URL",
    "DEFAULT_MODEL",
    "CONFIG_FILENAME",
]
