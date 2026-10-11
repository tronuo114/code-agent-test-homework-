"""``run_command`` —— 在受控 shell 中执行命令。

这是能力最强、也最危险的工具，因此加了三层防护：

1. **危险模式黑名单**：命中格式化磁盘、删根目录、fork 炸弹、改注册表等
   模式的命令直接拒绝执行，并告诉模型为什么被拒绝。
2. **只读模式白名单**：``--read-only`` 时只允许 ``ls``/``git status`` 一类
   纯查询命令，其余一律拒绝。
3. **超时 + 进程树清理**：见 :mod:`codeagent.tools.proc`。

需要说明的是：黑名单是"降低误伤"而不是"安全边界"。真正的隔离应当交给容器 /
低权限账户。这一点在 README 的安全章节有明确说明，不做过度承诺。
"""

from __future__ import annotations

import re
from typing import Any

from ..errors import ToolExecutionError, ToolPermissionError
from .base import Tool, ToolResult, schema_properties
from .proc import format_process_result, run_process

#: 危险命令模式（正则，忽略大小写）。命中即拒绝。
DANGEROUS_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\brm\s+(-[a-z]*[rf][a-z]*\s+)+/(?:\s|$|\*)", "递归删除根目录"),
    (r"\brm\s+(-[a-z]*[rf][a-z]*\s+)+~", "递归删除用户主目录"),
    (r"\brm\s+-rf\s+\*", "递归删除当前目录全部内容"),
    (r"Remove-Item\s+.*-Recurse.*-Force\s+[A-Za-z]:\\?\s*$", "递归删除整个盘符"),
    (r"\bdel\s+/[a-z]*[fs][a-z]*\s+.*[A-Za-z]:\\?\s*\*?\s*$", "删除盘符内容"),
    (r"\bformat\s+[A-Za-z]:", "格式化磁盘"),
    (r"\bmkfs(\.\w+)?\b", "创建文件系统（会清空分区）"),
    (r"\bdiskpart\b", "磁盘分区操作"),
    (r":\s*\(\s*\)\s*\{.*\|\s*:.*&\s*\}\s*;?\s*:", "fork 炸弹"),
    (r"\b(shutdown|reboot|halt|poweroff)\b", "关机/重启"),
    (r"\breg\s+(delete|add)\s+HKLM", "修改系统级注册表"),
    (r"\bnet\s+user\s+\S+\s+.*\/add", "创建系统账户"),
    (r"\b(icacls|takeown)\b.*\/grant", "修改文件系统权限"),
    (r"\bchmod\s+-R\s+777\s+/\s*$", "放开根目录权限"),
    (r"\bdd\s+.*of=/dev/[sh]d[a-z]", "直接写裸设备"),
    (r">\s*/dev/[sh]d[a-z]", "重定向覆盖裸设备"),
    (r"\b(curl|wget)\b[^|;&]*\|\s*(sudo\s+)?(ba|z|k)?sh\b", "从网络直接执行脚本"),
    (r"\b(iex|Invoke-Expression)\b.*\b(Invoke-WebRequest|iwr|curl)\b", "远程代码执行"),
    (r"\bsudo\s+rm\b", "sudo 删除文件"),
)

#: 只读模式下允许的命令前缀。
READONLY_ALLOWED_PREFIXES: tuple[str, ...] = (
    "ls", "dir", "cat", "type ", "head", "tail", "less", "more", "wc ",
    "find", "grep", "rg ", "fd ", "tree", "pwd", "cd ", "echo", "where ",
    "which ", "whoami", "hostname", "date", "env", "set ",
    "git status", "git log", "git diff", "git show", "git branch", "git remote",
    "python --version", "python3 --version", "node --version", "npm --version",
    "pip list", "pip show", "pip --version", "go version", "java -version",
    "rustc --version", "cargo --version", "tsc --version",
)


class RunCommandTool(Tool):
    name = "run_command"
    description = (
        "在系统 shell（Windows 为 cmd，Linux/macOS 为 sh）中执行一条命令并返回"
        "退出码、stdout 与 stderr。用于运行测试、查询环境、构建等。"
        "命令会在工作目录下执行，并且有超时限制。"
    )
    parameters = {
        "command": schema_properties(description="要执行的完整命令，例如 'python -m unittest discover tests'"),
        "cwd": schema_properties(description="执行目录（相对于工作目录），默认工作目录"),
        "timeout": schema_properties(
            description="超时秒数，默认 60，最长 600", type="number"
        ),
    }
    required = ("command",)
    mutating = True

    #: 允许的超时上限，防止模型传一个巨大的值把会话卡死。
    MAX_TIMEOUT = 600.0

    def run(self, **kwargs: Any) -> ToolResult:
        command: str = kwargs["command"].strip()
        if not command:
            raise ToolExecutionError("command 不能为空")

        self._reject_dangerous(command)

        workdir = self.context.resolve(kwargs.get("cwd"), purpose="在其中执行命令")
        if not workdir.is_dir():
            raise ToolExecutionError(
                f"执行目录不存在：{self.context.display(workdir)}"
            )

        timeout = float(kwargs.get("timeout") or self.context.command_timeout)
        timeout = max(1.0, min(timeout, self.MAX_TIMEOUT))

        result = run_process(command, cwd=workdir, timeout=timeout, shell=True)
        ok, text = format_process_result(result, self.context.max_output)

        prefix = f"（工作目录：{self.context.display(workdir)}）\n"
        if ok:
            return ToolResult.success(prefix + text, exit_code=result.returncode)
        return ToolResult.failure(
            prefix + text,
            kind="tool.timeout" if result.timed_out else "tool.command_failed",
            exit_code=result.returncode,
        )

    # ------------------------------------------------------------------ #
    def _reject_dangerous(self, command: str) -> None:
        for pattern, reason in DANGEROUS_PATTERNS:
            if re.search(pattern, command, flags=re.IGNORECASE):
                raise ToolPermissionError(
                    f"命令被安全策略拒绝（识别为：{reason}）。\n"
                    f"命令原文：{command}\n"
                    f"请改用更精确、非破坏性的方式完成任务。"
                )

        if not self.context.allow_write and not self._is_readonly_command(command):
            raise ToolPermissionError(
                "当前为只读模式（--read-only），只允许执行查询类命令"
                "（如 ls / cat / grep / git status）。\n"
                f"被拒绝的命令：{command}"
            )

    @staticmethod
    def _is_readonly_command(command: str) -> bool:
        normalized = command.strip().lower()
        return any(
            normalized == prefix.strip() or normalized.startswith(prefix)
            for prefix in READONLY_ALLOWED_PREFIXES
        )


__all__ = ["RunCommandTool", "DANGEROUS_PATTERNS", "READONLY_ALLOWED_PREFIXES"]
