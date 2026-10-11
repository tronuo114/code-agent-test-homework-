"""离线 Mock 模型：无需任何 API Key 即可跑通完整的 Agent 循环。

用途有三：

- **演示**：``python main.py --mock`` 能真实地走一遍
  ``list_dir → read_file → 总结`` 的流程，评审者不必先申请密钥。
- **测试**：:class:`ScriptedLLM` 让单元测试可以精确控制模型每一步的输出，
  从而覆盖"模型返回非法参数""模型陷入重复调用"等难以用真实模型复现的分支。
- **对照**：它实现了与 :class:`~codeagent.llm.LLMClient` 完全相同的 ``chat``
  签名，说明 Agent 循环确实只依赖一个很窄的接口（依赖倒置）。

它当然不是真正的语言模型：这里的"推理"是一组确定性启发式规则。
"""

from __future__ import annotations

import re
import time
from typing import Any, Callable, Sequence

from .llm import LLMResponse, ToolCall, Usage

Message = dict[str, Any]

#: 从 list_dir 输出里抠出文件名的正则，例如 ``./codeagent/agent.py  (12.3 KB)``。
_FILE_LINE = re.compile(r"^\s*(\S+\.\w{1,8})\s+\([\d.]+\s*[KMG]?B\)", re.MULTILINE)

#: 从用户输入里识别文件名的正则。
_PATH_HINT = re.compile(r"[\w./\\-]+\.\w{1,8}\b")

#: 优先阅读的文件（演示时挑一个"有内容可讲"的）。
_PREFERRED = (".py", ".md", ".txt", ".json", ".toml", ".yaml", ".yml", ".ts", ".js")


def _suffix_of(path: str) -> str:
    """返回路径的扩展名（不含点）；没有扩展名时返回空串。

    用来判断用户点名的是"一个文件"还是"一个目录" —— 目录名通常不带扩展名。
    """
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    head, dot, tail = name.rpartition(".")
    return tail if dot and head else ""


class MockLLM:
    """一个确定性的假模型，行为模拟"会调工具的代码助手"。"""

    #: 模拟网络延迟（秒），设小一点让演示流畅。
    latency = 0.05

    def __init__(self, settings: Any = None) -> None:
        self.settings = settings
        self.last_usage = Usage()
        self.calls = 0

    # ------------------------------------------------------------------ #
    def chat(
        self,
        messages: Sequence[Message],
        tools: Sequence[dict[str, Any]] | None = None,
        *,
        stream: bool | None = None,
        on_text: Callable[[str], None] | None = None,
        tool_choice: str | None = None,
    ) -> LLMResponse:
        """按启发式规则决定：继续调工具，还是给出最终答案。"""
        self.calls += 1
        time.sleep(self.latency)

        available = {self._tool_name(schema) for schema in (tools or [])}
        user_message = self._last_user_message(messages)
        tool_results = self._tool_results_since_last_user(messages)

        # 没有工具可用（收尾调用）→ 直接总结
        if not available or tool_choice == "none":
            return self._respond(self._summarize(user_message, tool_results), on_text)

        # 第一步：如果提示里点名了某个文件就直接读它，否则先看目录结构
        if not tool_results:
            hinted = _PATH_HINT.search(user_message)
            target = hinted.group(0) if hinted else "."
            looks_like_file = bool(hinted) and bool(_suffix_of(target))
            if looks_like_file and "read_file" in available:
                return self._tool_response(
                    "read_file",
                    {"path": target, "max_lines": 150},
                    text=f"我先读取 {target} 的内容。",
                    on_text=on_text,
                )
            if "list_dir" in available:
                return self._tool_response(
                    "list_dir",
                    {"path": target if hinted else ".", "recursive": False},
                    text="我先看一下目录结构，确认有哪些文件。",
                    on_text=on_text,
                )
            if "read_file" in available:
                return self._tool_response(
                    "read_file",
                    {"path": target if hinted else "README.md"},
                    text="我先读取相关文件。",
                    on_text=on_text,
                )

        # 第二步：读取目录里第一个像样的文件
        if len(tool_results) == 1 and "read_file" in available:
            candidate = self._pick_file(tool_results[0])
            if candidate:
                return self._tool_response(
                    "read_file",
                    {"path": candidate, "max_lines": 60},
                    text=f"目录结构清楚了，我读一下 {candidate} 的内容。",
                    on_text=on_text,
                )

        # 默认：给出最终答案
        return self._respond(self._summarize(user_message, tool_results), on_text)

    # ------------------------------------------------------------------ #
    # 内部：构造响应
    # ------------------------------------------------------------------ #
    def _tool_response(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        text: str,
        on_text: Callable[[str], None] | None,
    ) -> LLMResponse:
        if on_text and text:
            on_text(text)
        import json as _json

        call = ToolCall(
            id=f"mock_call_{self.calls}",
            name=name,
            arguments=arguments,
            raw_arguments=_json.dumps(arguments, ensure_ascii=False),
        )
        return LLMResponse(
            content=text,
            tool_calls=[call],
            finish_reason="tool_calls",
            usage=Usage(prompt_tokens=len(str(arguments)) // 4 + 40, completion_tokens=32),
            model="mock-model",
        )

    def _respond(self, text: str, on_text: Callable[[str], None] | None) -> LLMResponse:
        if on_text:
            # 模拟流式：按标点切块吐出
            for piece in re.findall(r"[^，。；\n]*[，。；\n]?", text):
                if piece:
                    on_text(piece)
        return LLMResponse(
            content=text,
            tool_calls=[],
            finish_reason="stop",
            usage=Usage(prompt_tokens=200, completion_tokens=len(text) // 2 + 10),
            model="mock-model",
        )

    # ------------------------------------------------------------------ #
    # 内部：上下文解析
    # ------------------------------------------------------------------ #
    @staticmethod
    def _tool_name(schema: dict[str, Any]) -> str:
        return ((schema or {}).get("function") or {}).get("name", "")

    @staticmethod
    def _last_user_message(messages: Sequence[Message]) -> str:
        for message in reversed(messages):
            if message.get("role") == "user":
                return str(message.get("content") or "")
        return ""

    @staticmethod
    def _tool_results_since_last_user(messages: Sequence[Message]) -> list[str]:
        collected: list[str] = []
        for message in reversed(messages):
            if message.get("role") == "user":
                break
            if message.get("role") == "tool":
                collected.append(str(message.get("content") or ""))
        return list(reversed(collected))

    @staticmethod
    def _pick_file(tool_output: str) -> str | None:
        """从 list_dir 的输出里挑一个最适合阅读的文件。"""
        candidates = _FILE_LINE.findall(tool_output)
        readable = [c for c in candidates if c.lower().endswith(_PREFERRED)]
        if readable:
            # 优先 README，其次按路径长度（更"核心"的文件通常路径更短）
            readable.sort(key=lambda p: (0 if "readme" in p.lower() else 1, len(p)))
            return readable[0]
        return candidates[0] if candidates else None

    # ------------------------------------------------------------------ #
    @staticmethod
    def _summarize(user_message: str, tool_results: Sequence[str]) -> str:
        """把已获得的观察拼成一段像样的中文答复。"""
        lines: list[str] = []
        lines.append("【离线演示模式】我是一条确定性规则生成的回答，用于在没有 API Key 的情况下")
        lines.append("验证 Agent 循环、工具调用与上下文传递是否正常工作。")
        lines.append("")
        lines.append(f"你的问题：{user_message.strip()[:200] or '(空)'}")
        lines.append("")
        if tool_results:
            lines.append(f"本轮我共获得 {len(tool_results)} 条工具返回，摘要如下：")
            for index, output in enumerate(tool_results, 1):
                head = " / ".join(
                    line.strip()
                    for line in output.splitlines()
                    if line.strip() and not line.strip().startswith("<")
                )[:300]
                lines.append(f"  {index}. {head}")
        else:
            lines.append("本轮没有调用任何工具（没有可用的工具，或任务无需取数）。")
        lines.append("")
        lines.append(
            "要获得真正的回答，请去掉 --mock 并配置 CODEAGENT_API_KEY 后重新运行。"
        )
        return "\n".join(lines)


class ScriptedLLM:
    """按预设脚本依次返回响应，用于单元测试。

    Args:
        responses: :class:`LLMResponse` 列表；用完后重复返回 ``fallback``。
        fallback: 脚本耗尽后的兜底响应（默认一句结束语）。
    """

    def __init__(
        self,
        responses: Sequence[LLMResponse],
        fallback: LLMResponse | None = None,
    ) -> None:
        self._responses = list(responses)
        self._fallback = fallback or LLMResponse(
            content="(脚本已耗尽)", finish_reason="stop"
        )
        self.calls: list[dict[str, Any]] = []
        self.last_usage = Usage()
        self.index = 0

    def chat(
        self,
        messages: Sequence[Message],
        tools: Sequence[dict[str, Any]] | None = None,
        *,
        stream: bool | None = None,
        on_text: Callable[[str], None] | None = None,
        tool_choice: str | None = None,
    ) -> LLMResponse:
        # 深拷贝消息快照，便于测试断言"模型看到了什么"
        self.calls.append(
            {
                "messages": [dict(m) for m in messages],
                "tools": list(tools or []),
                "tool_choice": tool_choice,
            }
        )
        response = (
            self._responses[self.index]
            if self.index < len(self._responses)
            else self._fallback
        )
        self.index += 1
        self.last_usage = self.last_usage + response.usage
        if on_text and response.content:
            on_text(response.content)
        return response


def tool_call(name: str, arguments: dict[str, Any] | None = None, call_id: str = "") -> ToolCall:
    """构造 :class:`ToolCall` 的测试辅助函数。"""
    import json as _json

    return ToolCall(
        id=call_id or f"call_{name}",
        name=name,
        arguments=arguments or {},
        raw_arguments=_json.dumps(arguments or {}, ensure_ascii=False),
    )


def tool_response(name: str, arguments: dict[str, Any] | None = None, text: str = "") -> LLMResponse:
    """构造"要求调用某个工具"的响应。"""
    return LLMResponse(
        content=text,
        tool_calls=[tool_call(name, arguments)],
        finish_reason="tool_calls",
    )


def text_response(text: str) -> LLMResponse:
    """构造"最终答案"的响应。"""
    return LLMResponse(content=text, finish_reason="stop")


__all__ = ["MockLLM", "ScriptedLLM", "tool_call", "tool_response", "text_response"]
