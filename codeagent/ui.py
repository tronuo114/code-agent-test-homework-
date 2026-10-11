"""终端界面基础件：着色、编码设置、输入辅助。

从 ``cli.py`` 中抽出，供"完整助手模式"与"代码审查模式"两个前端共用，
避免两份实现各写一遍 ANSI 转义序列。
"""

from __future__ import annotations

import getpass
import os
import sys
from typing import Any


class Palette:
    """极简 ANSI 着色器（不引入 colorama / rich 这类第三方依赖）。"""

    CODES = {
        "reset": "\033[0m",
        "bold": "\033[1m",
        "dim": "\033[2m",
        "italic": "\033[3m",
        "red": "\033[31m",
        "green": "\033[32m",
        "yellow": "\033[33m",
        "blue": "\033[34m",
        "magenta": "\033[35m",
        "cyan": "\033[36m",
        "grey": "\033[90m",
    }

    def __init__(self, enabled: bool = True) -> None:
        #: NO_COLOR 是业界通用的关闭着色的环境变量约定。
        self.enabled = bool(enabled) and not os.environ.get("NO_COLOR")

    def __call__(self, text: Any, *styles: str) -> str:
        text = str(text)
        if not self.enabled or not styles:
            return text
        prefix = "".join(self.CODES.get(style, "") for style in styles)
        return f"{prefix}{text}{self.CODES['reset']}"

    # -- 语义化快捷方式 ------------------------------------------------ #
    def ok(self, text: str) -> str:
        return self(f"✔ {text}", "green")

    def fail(self, text: str) -> str:
        return self(f"✖ {text}", "red")

    def warn(self, text: str) -> str:
        return self(f"⚠ {text}", "yellow")

    def info(self, text: str) -> str:
        return self(f"ℹ {text}", "cyan")

    def title(self, text: str) -> str:
        return self(text, "cyan", "bold")

    def dim(self, text: str) -> str:
        return self(text, "grey")


def configure_stdio() -> None:
    """把标准输出切到 UTF-8，避免 Windows 控制台里中文与符号乱码。"""
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    # 让子进程（run_command / run_python）也走 UTF-8
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")


def supports_color(stream: Any = None) -> bool:
    """是否应该在当前输出流上着色。"""
    stream = stream or sys.stdout
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    isatty = getattr(stream, "isatty", None)
    try:
        return bool(isatty and isatty())
    except Exception:
        return False


def ask(prompt: str, palette: Palette | None = None) -> str:
    """普通一行输入；EOF / Ctrl+C 返回空串，由调用方决定怎么处理。"""
    text = prompt if palette is None else palette(prompt, "green", "bold")
    try:
        return input(text).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return ""


def ask_secret(prompt: str) -> str:
    """不显示回显地读取密钥。

    终端不支持隐藏输入时（例如被重定向）退化为普通输入，
    但会明确提示"输入内容将可见"，避免用户误以为已隐藏。
    """
    try:
        return getpass.getpass(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return ""
    except Exception:
        print("（当前环境不支持隐藏输入，输入内容将可见）")
        try:
            return input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return ""


def display_width(text: str) -> int:
    """估算字符串在终端里占多少列。

    ``str.ljust`` 按**字符数**补齐，但中文/emoji 在终端里占 2 列，
    混排时用它会得到参差不齐的输出。这里按东亚宽字符区间粗略累加。
    """
    width = 0
    for ch in str(text):
        code = ord(ch)
        if (
            0x1100 <= code <= 0x115F          # 韩文字母
            or 0x2E80 <= code <= 0xA4CF       # CJK 部首 / 汉字 / 假名
            or 0xAC00 <= code <= 0xD7A3       # 韩文音节
            or 0xF900 <= code <= 0xFAFF       # CJK 兼容表意
            or 0xFE30 <= code <= 0xFE6F       # CJK 兼容形式
            or 0xFF00 <= code <= 0xFF60       # 全角标点
            or 0xFFE0 <= code <= 0xFFE6
            or 0x1F300 <= code <= 0x1FAFF     # emoji
            or 0x20000 <= code <= 0x3FFFD     # CJK 扩展
        ):
            width += 2
        else:
            width += 1
    return width


def pad(text: str, width: int) -> str:
    """按**显示宽度**右补空格（而不是字符数）。"""
    return str(text) + " " * max(0, width - display_width(text))


__all__ = [
    "Palette",
    "configure_stdio",
    "supports_color",
    "ask",
    "ask_secret",
    "display_width",
    "pad",
]
