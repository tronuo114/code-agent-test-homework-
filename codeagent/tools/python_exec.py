"""``run_python`` —— 用独立子进程执行一段 Python 代码。

为什么不让模型直接 ``run_command`` 里写 ``python -c``？因为把代码作为
结构化参数传入有两个好处：

1. 免去跨平台引号转义问题（Windows ``cmd`` 与 POSIX ``sh`` 的引号规则不同，
   模型生成的 ``python -c "..."`` 极易被 shell 吃掉引号）。
2. 可以自动注入一段**可选的探查前言**（例如打印工作目录、Python 版本），
   并且在超时/报错时给出更贴近代码语义的提示。

执行隔离程度：子进程 + 工作目录约束 + 超时 + 无 stdin。
这不是安全沙箱（代码仍拥有当前用户权限），README 中已明确说明。
"""

from __future__ import annotations

from typing import Any

from ..errors import ToolExecutionError
from .base import Tool, ToolResult, schema_properties
from .proc import format_process_result, python_executable, run_process


class RunPythonTool(Tool):
    name = "run_python"
    description = (
        "在独立 Python 子进程中执行一段代码并返回输出（stdout/stderr 与异常堆栈）。"
        "适合做数值验证、调用标准库、快速验证一段逻辑；代码在工作目录下执行。"
        "注意这是独立进程，不会保留上次运行的变量。"
    )
    parameters = {
        "code": schema_properties(description="要执行的 Python 源码（多行字符串）"),
        "timeout": schema_properties(description="超时秒数，默认 60，最长 600", type="number"),
        "cwd": schema_properties(description="执行目录（相对于工作目录），默认工作目录"),
    }
    required = ("code",)
    mutating = True
    MAX_TIMEOUT = 600.0

    #: 注入到用户代码之前的前言：尽量让输出可读、可复现。
    PREAMBLE = (
        "import sys, os\n"
        "sys.setrecursionlimit(10000)\n"
        "os.environ.setdefault('PYTHONIOENCODING', 'utf-8')\n"
    )

    def run(self, **kwargs: Any) -> ToolResult:
        code: str = kwargs["code"]
        if not code.strip():
            raise ToolExecutionError("code 不能为空")

        workdir = self.context.resolve(kwargs.get("cwd"), purpose="在其中执行代码")
        if not workdir.is_dir():
            raise ToolExecutionError(f"执行目录不存在：{self.context.display(workdir)}")

        timeout = float(kwargs.get("timeout") or self.context.command_timeout)
        timeout = max(1.0, min(timeout, self.MAX_TIMEOUT))

        command = [python_executable(), "-X", "utf8", "-c", self.PREAMBLE + "\n" + code]
        result = run_process(command, cwd=workdir, timeout=timeout, shell=False)
        ok, text = format_process_result(result, self.context.max_output)

        header = f"（Python {_py_version()}，工作目录：{self.context.display(workdir)}）\n"
        if ok:
            return ToolResult.success(header + text, exit_code=result.returncode)

        hint = ""
        if result.timed_out:
            hint = "\n提示：代码超时，很可能存在死循环或阻塞式 IO，请缩小验证范围。"
        elif "Traceback (most recent call last)" in result.stderr:
            hint = "\n提示：代码抛出异常，请根据堆栈最后一行定位问题后修正代码重试。"
        return ToolResult.failure(
            header + text + hint,
            kind="tool.timeout" if result.timed_out else "tool.python_failed",
            exit_code=result.returncode,
        )


def _py_version() -> str:
    import platform

    return platform.python_version()


__all__ = ["RunPythonTool"]
