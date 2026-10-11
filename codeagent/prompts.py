"""Prompt 设计层。

本模块把"提示词"当成一等公民来管理，而不是散落在业务代码里的字符串拼接。
四个可独立测试的部分：

1. :data:`SYSTEM_PROMPT_TEMPLATE` —— Agent 的**人格 + 作业规程**（ReAct 行为约束）。
2. :func:`render_tool_catalog` —— 把工具注册表渲染成人可读清单塞进 system prompt。
   虽然函数调用协议已经通过 API 的 ``tools`` 字段传入，但再写一份自然语言清单可以
   显著提升**弱函数调用能力模型**（部分开源模型 / 量化模型）的工具选择准确率。
3. :data:`STEP_LIMIT_NUDGE` / :data:`REPEAT_CALL_NUDGE` / :data:`TOOL_ERROR_HINT`
   —— **运行时纠偏提示**，在 Agent 循环检测到异常模式时注入。
4. :data:`TOOL_RESULT_WRAPPER` —— 工具输出的统一信封格式，让模型能区分
   "工具失败" 与 "工具成功但内容里恰好包含 error 字样"。
"""

from __future__ import annotations

import platform
from typing import Iterable, Protocol


class _HasSchema(Protocol):  # pragma: no cover - 仅用于类型标注
    name: str
    description: str


# --------------------------------------------------------------------------- #
# 1. 核心 System Prompt
# --------------------------------------------------------------------------- #
SYSTEM_PROMPT_TEMPLATE = """\
你是 CodeAgent，一个运行在用户本地终端中的代码助手 Agent。

# 你与普通聊天模型的区别
你可以调用工具来**真实地**读取文件、检索代码、修改文件、执行命令与运行 Python。
凡是关于代码库的事实性结论，都必须来自工具的真实返回；**绝不允许凭空推测**文件内容、
函数签名或目录结构。若不确定，就先去查。

# 运行环境
- 操作系统：{os_name}
- 工作目录（沙箱根，所有路径必须位于其内部）：{workspace}
- 路径写法：可以使用相对工作目录的相对路径，也可以使用该目录下的绝对路径。
- 写入权限：{write_mode}
- 可用工具：
{tool_catalog}

# 作业规程
1. **先看再改**：修改任何文件之前，先用 read_file / search_code 确认它的真实内容。
   严禁基于猜测的"原文"去调用 edit_file。
2. **由粗到细**：定位问题优先用 list_dir → search_code → read_file 的收敛顺序，
   不要一上来就大范围读取无关文件。
3. **并行取数**：当多个工具调用之间没有依赖关系时（例如同时读 3 个文件），
   请在**同一条回复里一次性发起多个 tool_calls**，而不是分多轮串行，以减少往返。
4. **命令要克制**：run_command 只执行必要的、幂等的、非破坏性的命令。
   不要执行修改系统全局状态的命令（安装系统软件、改注册表、关机、删除工作目录外的文件）。
5. **失败要读错误**：工具返回错误时，仔细阅读错误信息并调整参数。
   **禁止**用完全相同的参数重复调用同一个工具；那只会浪费步数。
6. **及时收尾**：一旦掌握的信息足以回答用户，立即给出最终答案。
   不要为了"看起来更努力"而做无意义的额外调用。

# 输出规范
- 默认使用中文回答；代码、命令、路径、标识符保持原文。
- 代码块请标注语言（```python / ```bash 等）。
- 涉及代码修改时，说明**改了哪个文件**、**为什么改**。
- 不要向用户粘贴工具的原始 JSON 返回，除非用户明确要求。
- 最终答案要直接、可执行、不啰嗦。
"""


# --------------------------------------------------------------------------- #
# 2. 工具目录渲染
# --------------------------------------------------------------------------- #
def render_tool_catalog(tools: Iterable[_HasSchema], width: int = 100) -> str:
    """把工具渲染成 ``- name(args): description`` 形式的清单。"""
    lines: list[str] = []
    for tool in tools:
        params = getattr(tool, "parameters", None) or {}
        required = set(params.get("required") or ())
        props: dict = params.get("properties") or {}
        arg_bits: list[str] = []
        for pname in props:
            arg_bits.append(pname if pname in required else f"{pname}?")
        signature = ", ".join(arg_bits)
        desc = " ".join((tool.description or "").split())
        if len(desc) > width:
            desc = desc[: width - 1] + "…"
        lines.append(f"  - {tool.name}({signature}): {desc}")
    return "\n".join(lines) if lines else "  （无可用工具）"


def build_system_prompt(
    workspace: str,
    tool_catalog: str,
    allow_write: bool,
    template: str | None = None,
) -> str:
    """组装最终的 system prompt。

    Args:
        template: 自定义模板；必须与 :data:`SYSTEM_PROMPT_TEMPLATE` 使用同一组
            占位符（``os_name`` / ``workspace`` / ``write_mode`` / ``tool_catalog``）。
            代码审查模式就是靠这个参数换成
            :data:`codeagent.review_prompt.REVIEW_SYSTEM_PROMPT` 的。
    """
    return (template or SYSTEM_PROMPT_TEMPLATE).format(
        os_name=f"{platform.system()} {platform.release()}",
        workspace=workspace,
        write_mode=(
            "允许（仅限工作目录内部，越界会被沙箱拒绝）"
            if allow_write
            else "只读（禁止写文件与执行修改类命令）"
        ),
        tool_catalog=tool_catalog,
    )


# --------------------------------------------------------------------------- #
# 3. 运行时纠偏提示
# --------------------------------------------------------------------------- #
STEP_LIMIT_NUDGE = (
    "【系统提示】你已达到工具调用步数上限（{max_steps} 步）。\n"
    "请立即停止调用工具，仅基于上文已经获得的信息给出最终答案。\n"
    "如果仍有未确认的部分，请在答案中明确说明「未验证」以及你原本打算如何验证，"
    "而不是继续猜测。"
)

REPEAT_CALL_NUDGE = (
    "【系统提示】检测到你刚刚以**完全相同的参数**重复调用了工具 `{tool_name}`。\n"
    "重复调用不会有新结果。请改为：\n"
    "  1) 如果上一步的返回已经足够，直接给出最终答案；\n"
    "  2) 如果是参数不对，换一个明显不同的参数或换用别的工具；\n"
    "  3) 如果工具持续失败，向用户说明失败原因并给出替代方案。"
)

TOOL_FAILURE_STREAK_NUDGE = (
    "【系统提示】工具 `{tool_name}` 已经连续失败 {count} 次。\n"
    "请停止重试该工具，改为向用户解释失败原因（附上关键错误信息），"
    "并给出可行的替代方案。"
)

FORCE_ANSWER_INSTRUCTION = (
    "现在请输出面向用户的最终答案。不要再调用任何工具。"
)

# --------------------------------------------------------------------------- #
# 4. 工具结果信封
# --------------------------------------------------------------------------- #
TOOL_RESULT_WRAPPER = """\
<tool_result name="{tool_name}" ok="{ok}">
{body}
</tool_result>"""


def wrap_tool_result(tool_name: str, ok: bool, body: str) -> str:
    """把工具输出包进统一信封，避免模型混淆"失败"与"内容里含 error"。"""
    return TOOL_RESULT_WRAPPER.format(
        tool_name=tool_name, ok="true" if ok else "false", body=body
    )


__all__ = [
    "SYSTEM_PROMPT_TEMPLATE",
    "build_system_prompt",
    "render_tool_catalog",
    "STEP_LIMIT_NUDGE",
    "REPEAT_CALL_NUDGE",
    "TOOL_FAILURE_STREAK_NUDGE",
    "FORCE_ANSWER_INSTRUCTION",
    "TOOL_RESULT_WRAPPER",
    "wrap_tool_result",
]
