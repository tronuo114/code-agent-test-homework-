"""工具包入口：对外导出工具基类、注册表与默认工具集。

新增一个工具只需要三步（不必改 Agent 循环）：

1. 继承 :class:`~codeagent.tools.base.Tool`，声明 ``name`` / ``description`` /
   ``parameters`` / ``required``，实现 ``run``；
2. 在 :data:`DEFAULT_TOOL_CLASSES` 中登记；
3. 补一个单元测试。
"""

from __future__ import annotations

from typing import Iterable, Sequence

from .base import Tool, ToolContext, ToolResult, schema_properties, truncate
from .filesystem import (
    EditFileTool,
    ListDirTool,
    ReadFileTool,
    SearchCodeTool,
    WriteFileTool,
)
from .proc import ProcessResult, run_process
from .python_exec import RunPythonTool
from .registry import ToolRegistry
from .shell import RunCommandTool

#: 默认工具集。顺序会影响 system prompt 中的工具清单顺序，
#: 因此把「只读探查类」排在前面，引导模型先看再改。
DEFAULT_TOOL_CLASSES: tuple[type[Tool], ...] = (
    ListDirTool,
    ReadFileTool,
    SearchCodeTool,
    WriteFileTool,
    EditFileTool,
    RunCommandTool,
    RunPythonTool,
)

#: 代码审查模式使用的**只读**工具集。
#:
#: 审查只需要"看"：列目录、读文件、检索。刻意不暴露写文件与执行命令的能力，
#: 理由有三：
#:   1. 审查任务本身不需要它们（需求是"找出可能的错误"，不是"修好它"）；
#:   2. 少一类工具就少一类误操作，用户也不必担心审查过程改动了自己的代码；
#:   3. 工具清单更短，模型选错工具的概率更低，也更省 token。
REVIEW_TOOL_CLASSES: tuple[type[Tool], ...] = (
    ListDirTool,
    ReadFileTool,
    SearchCodeTool,
)


def build_default_registry(context: ToolContext) -> ToolRegistry:
    """构建包含全部内置工具的注册表。"""
    registry = ToolRegistry()
    for tool_class in DEFAULT_TOOL_CLASSES:
        registry.register(tool_class(context))
    return registry


def build_review_registry(context: ToolContext) -> ToolRegistry:
    """构建代码审查模式使用的只读工具集。"""
    registry = ToolRegistry()
    for tool_class in REVIEW_TOOL_CLASSES:
        registry.register(tool_class(context))
    return registry


def build_registry(
    context: ToolContext,
    *,
    only: Sequence[str] | None = None,
    exclude: Iterable[str] = (),
) -> ToolRegistry:
    """按名字筛选工具集。

    Args:
        only: 若提供，则只注册这些工具名。
        exclude: 需要排除的工具名。
    """
    excluded = set(exclude)
    registry = ToolRegistry()
    for tool_class in DEFAULT_TOOL_CLASSES:
        name = tool_class.name
        if only is not None and name not in only:
            continue
        if name in excluded:
            continue
        registry.register(tool_class(context))
    return registry


__all__ = [
    "Tool",
    "ToolContext",
    "ToolResult",
    "ToolRegistry",
    "ProcessResult",
    "run_process",
    "schema_properties",
    "truncate",
    "build_default_registry",
    "build_review_registry",
    "build_registry",
    "DEFAULT_TOOL_CLASSES",
    "REVIEW_TOOL_CLASSES",
    "ListDirTool",
    "ReadFileTool",
    "SearchCodeTool",
    "WriteFileTool",
    "EditFileTool",
    "RunCommandTool",
    "RunPythonTool",
]
