"""测试公共辅助：临时工作目录、假 LLM、Agent 组装。"""

from __future__ import annotations

import os
import secrets
import shutil
import sys
import unittest
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

#: 测试临时目录放在项目内部，而不是系统 %TEMP%（后者可能只读）。
TMP_ROOT = PROJECT_ROOT / ".tmp" / "tests"


def _make_temp_dir(root: Path, attempts: int = 50) -> Path:
    """在 ``root`` 下创建一个随机名字的目录。

    这里**刻意不使用** :func:`tempfile.mkdtemp`。原因是一个很容易踩的 Windows 坑：

    CPython 在 Windows 上调用 ``os.mkdir(path, mode)`` 时，只有当 ``mode == 0o777``
    才会把 ``NULL`` 作为安全描述符传给 ``CreateDirectoryW``（即继承父目录权限）。
    mode 是其它任何值时，它会构造一个**只授权当前用户、并阻断权限继承**的
    安全描述符。而 ``tempfile.mkdtemp`` 固定使用 ``0o700``。

    如果测试运行在受限令牌下（AppContainer / 沙箱进程），这个新建目录对进程本身就是
    不可写的，于是所有测试都会莫名其妙地报 ``PermissionError``。

    因此改为自己用 ``0o777`` 建目录，让新目录正常继承父目录权限。
    """
    for _ in range(attempts):
        candidate = root / ("ws-" + secrets.token_hex(4))
        try:
            os.mkdir(candidate, 0o777)
            return candidate
        except FileExistsError:  # pragma: no cover - 概率极低
            continue
    raise RuntimeError(f"无法在 {root} 下创建临时目录")  # pragma: no cover


from codeagent.agent import CodeAgent  # noqa: E402
from codeagent.config import Settings  # noqa: E402
from codeagent.mock_llm import ScriptedLLM  # noqa: E402
from codeagent.tools import ToolContext, build_default_registry  # noqa: E402


class TempWorkspaceTestCase(unittest.TestCase):
    """为每个测试提供一个独立的临时工作目录。"""

    def setUp(self) -> None:
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        self._tmp = _make_temp_dir(TMP_ROOT)
        self.workspace = self._tmp.resolve()

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)
        try:  # 顺手清理空目录，不留痕迹
            TMP_ROOT.rmdir()
        except OSError:
            pass

    # -- 快捷方式 ------------------------------------------------------ #
    def write(self, relative: str, content: str) -> Path:
        path = self.workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def read(self, relative: str) -> str:
        return (self.workspace / relative).read_text(encoding="utf-8")

    def make_context(self, **overrides: Any) -> ToolContext:
        params = {
            "workspace": self.workspace,
            "allow_write": True,
            "max_output": 8_000,
            "command_timeout": 30.0,
        }
        params.update(overrides)
        return ToolContext(**params)

    def make_settings(self, **overrides: Any) -> Settings:
        params: dict[str, Any] = {
            "api_key": "test-key",
            "model": "test-model",
            "base_url": "https://example.invalid/v1",
            "workspace": self.workspace,
            "max_steps": 6,
            "max_tool_output": 8_000,
            "context_token_budget": 20_000,
            "command_timeout": 30.0,
            "stream": False,
        }
        params.update(overrides)
        return Settings(**params)

    def make_agent(
        self, responses: list[Any], **settings_overrides: Any
    ) -> tuple[CodeAgent, ScriptedLLM]:
        settings = self.make_settings(**settings_overrides)
        context = self.make_context(
            allow_write=settings.allow_write,
            max_output=settings.max_tool_output,
            command_timeout=settings.command_timeout,
        )
        registry = build_default_registry(context)
        llm = ScriptedLLM(responses)
        agent = CodeAgent(llm, registry, settings, tool_context=context)
        return agent, llm


__all__ = ["TempWorkspaceTestCase", "PROJECT_ROOT"]
