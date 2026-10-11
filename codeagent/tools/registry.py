"""工具注册表：集中管理工具的注册、schema 导出与安全分发。

Agent 循环只依赖这一个类，新增工具时只需 :meth:`ToolRegistry.register`，
无需改动循环代码 —— 这是本项目的核心可扩展点。
"""

from __future__ import annotations

from typing import Any, Iterator, Mapping, Sequence

from ..errors import ToolNotFound
from .base import Tool, ToolContext, ToolResult


class ToolRegistry:
    """一个按名字索引的工具集合。"""

    def __init__(self, tools: Sequence[Tool] | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools or ():
            self.register(tool)

    # ------------------------------------------------------------------ #
    # 注册与查询
    # ------------------------------------------------------------------ #
    def register(self, tool: Tool) -> Tool:
        """注册一个工具；重名会直接报错，避免静默覆盖难以排查。"""
        if not tool.name:
            raise ValueError(f"{type(tool).__name__} 未声明 name")
        if tool.name in self._tools:
            raise ValueError(f"工具名重复：{tool.name!r} 已被注册")
        self._tools[tool.name] = tool
        return tool

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise ToolNotFound(
                f"不存在名为 {name!r} 的工具；可用工具：{self.names()}"
            ) from exc

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __iter__(self) -> Iterator[Tool]:
        return iter(self._tools.values())

    def __len__(self) -> int:
        return len(self._tools)

    def names(self) -> list[str]:
        return list(self._tools)

    def tools(self) -> list[Tool]:
        return list(self._tools.values())

    # ------------------------------------------------------------------ #
    # 导出给 LLM
    # ------------------------------------------------------------------ #
    def schemas(self) -> list[dict[str, Any]]:
        """导出 OpenAI ``tools`` 字段所需的 schema 列表。"""
        return [tool.schema() for tool in self._tools.values()]

    def catalog(self) -> str:
        """渲染成人可读清单，用于拼进 system prompt。"""
        from ..prompts import render_tool_catalog

        return render_tool_catalog(self._tools.values())

    # ------------------------------------------------------------------ #
    # 分发执行
    # ------------------------------------------------------------------ #
    def execute(self, name: str, arguments: Mapping[str, Any] | None) -> ToolResult:
        """执行工具。**本方法不会抛异常**（除 ``KeyboardInterrupt``）。

        找不到工具或参数非法时返回 ``ok=False`` 的结果，由 Agent 循环回灌给模型，
        让模型有机会自我修正 —— 这是 Agent 鲁棒性的关键。
        """
        try:
            tool = self.get(name)
        except ToolNotFound as exc:
            return ToolResult.from_exception(exc)
        return tool.execute(arguments)

    def describe(self) -> str:
        """给 ``/tools`` 命令用的多行说明。"""
        lines: list[str] = []
        for tool in self._tools.values():
            flags = []
            if tool.requires_write:
                flags.append("需写权限")
            if tool.mutating:
                flags.append("会改变状态")
            suffix = f"  [{'/'.join(flags)}]" if flags else ""
            lines.append(f"● {tool.name}{suffix}")
            lines.append(f"    {tool.description}")
            props = tool.parameters or {}
            for key, spec in props.items():
                mark = "*" if key in tool.required else " "
                desc = spec.get("description", "")
                lines.append(f"      {mark} {key}: {desc}")
        return "\n".join(lines) if lines else "（未注册任何工具）"


__all__ = ["ToolRegistry", "ToolContext"]
