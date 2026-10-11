"""代码审查模式的命令行前端。

与 :mod:`codeagent.cli`（完整助手模式）的区别：

- **命令驱动**：``help`` / ``api`` / ``review`` / ``model`` / ``url`` / ``config`` / ``save`` / ``exit``；
- **只读工具集**：只注册 ``list_dir`` / ``read_file`` / ``search_code``；
- **专用 Prompt**：用 :data:`codeagent.review_prompt.REVIEW_SYSTEM_PROMPT` 替换通用模板；
- **绑定即校验**：``api`` 命令会真的发一次极小请求，立刻告诉用户成功还是失败。

Agent 内核（``agent.py`` / ``llm.py`` / ``memory.py`` / ``tools``）一行没改，
只是换了一个 system prompt 模板和一个工具注册表 —— 这正是分层设计要换来的东西。
"""

from __future__ import annotations

import argparse
import platform
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from . import __version__
from .agent import AgentHooks, AgentResult, CodeAgent, ToolInvocation
from .config import Settings
from .errors import AgentError, ConfigError, LLMError
from .llm import LLMClient
from .review_prompt import (
    REVIEW_SYSTEM_PROMPT,
    build_review_instruction,
    build_review_task,
)
from .store import Binding, ConfigStore, app_dir, config_precedence_hint
from .tools import ToolContext, ToolResult, build_review_registry
from .ui import Palette, ask, ask_secret, configure_stdio, pad, supports_color
from .verify import verify_binding

# --------------------------------------------------------------------------- #
# 帮助文本
# --------------------------------------------------------------------------- #
HELP_TEXT = """\
可用指令：

  help  |  ?              显示本帮助
  api                    绑定 API Key（会提示输入，并立刻联网校验，告诉你成功还是失败）
  api <key>              直接绑定指定的 API Key
  api show               查看当前绑定状态（密钥脱敏显示）
  api clear              解除绑定（删除 config.json）
  model <名称>            设置模型，例如 model deepseek-chat
  url <地址>              设置接口端点，例如 url https://api.deepseek.com/v1
  review                 开始审查（会提示输入要审查的文件或目录路径）
  review <路径>           直接审查指定的文件或目录
  save [路径]             把上一条审查报告保存为 Markdown 文件
  config                 显示当前完整配置与配置文件位置
  exit | quit | q        退出

其他用法：
  · 直接输入一个已存在的路径，等价于 review <路径>
  · 直接输入一句自然语言，会按你的要求进行审查
    例如：检查 codeagent 目录下所有 Python 文件的空值处理问题
"""

BANNER = r"""
   ____          _      ____            _
  / ___|___   __| | ___|  _ \ _____   _(_) _____      __
 | |   / _ \ / _` |/ _ \ |_) / _ \ \ / / |/ _ \ \ /\ / /
 | |__| (_) | (_| |  __/  _ <  __/\ V /| |  __/\ V  V /
  \____\___/ \__,_|\___|_| \_\___| \_/ |_|\___| \_/\_/
"""


# --------------------------------------------------------------------------- #
# Markdown 的轻量着色
# --------------------------------------------------------------------------- #
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_HEADING_RE = re.compile(r"^(#{1,4})\s*(.*)$")

#: 严重度标记 → 颜色，让问题清单在终端里一眼能分级。
_SEVERITY_COLORS = (
    ("🔴", "red"),
    ("严重", "red"),
    ("🟠", "yellow"),
    ("中等", "yellow"),
    ("🟡", "grey"),
    ("轻微", "grey"),
)


class MarkdownLineFormatter:
    """按行把 Markdown 渲染成带颜色的终端输出。

    做成**行缓冲**而不是整段后处理，是为了在流式输出下也能立刻看到内容：
    增量文本先攒进缓冲区，遇到换行才格式化并吐出完整的一行。
    """

    def __init__(self, palette: Palette) -> None:
        self.palette = palette
        self._buffer = ""

    def feed(self, delta: str) -> str:
        """喂入增量文本，返回可以立刻打印的部分（可能为空）。"""
        self._buffer += delta
        if "\n" not in self._buffer:
            return ""
        *lines, self._buffer = self._buffer.split("\n")
        return "".join(self.format(line) + "\n" for line in lines)

    def flush(self) -> str:
        if not self._buffer:
            return ""
        text = self.format(self._buffer)
        self._buffer = ""
        return text

    def format(self, line: str) -> str:
        palette = self.palette
        match = _HEADING_RE.match(line)
        if match:
            level, title = len(match.group(1)), match.group(2).strip()
            if level <= 2:
                color = "cyan"
            else:
                color = _severity_color(title)
            return palette(f"{title}", color, "bold")
        return _BOLD_RE.sub(lambda m: palette(m.group(1), "bold"), line)


def _severity_color(text: str) -> str:
    for marker, color in _SEVERITY_COLORS:
        if marker in text:
            return color
    return "cyan"


# --------------------------------------------------------------------------- #
# 渲染器
# --------------------------------------------------------------------------- #
@dataclass
class ReviewStats:
    """跨多次审查累计的统计。"""

    reviews: int = 0
    steps: int = 0
    tool_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0


class ReviewRenderer:
    """把审查过程画到终端上。"""

    def __init__(self, palette: Palette, verbose: bool = False) -> None:
        self.p = palette
        self.verbose = verbose
        self._formatter: MarkdownLineFormatter | None = None
        self._streamed = False

    # -- 钩子 ---------------------------------------------------------- #
    def hooks(self) -> AgentHooks:
        return AgentHooks(
            on_step_start=self.on_step_start,
            on_assistant_text=self.on_assistant_text,
            on_tool_start=self.on_tool_start,
            on_tool_end=self.on_tool_end,
            on_notice=self.on_notice,
            on_retry=self.on_retry,
        )

    def on_step_start(self, step: int, total: int) -> None:
        self._close_stream()
        print(self.p(f"\n▸ 第 {step}/{total} 步", "cyan", "bold"))

    def on_assistant_text(self, delta: str) -> None:
        if self._formatter is None:
            self._formatter = MarkdownLineFormatter(self.p)
        text = self._formatter.feed(delta)
        if text:
            self._streamed = True
            sys.stdout.write(text)
            sys.stdout.flush()

    def on_tool_start(self, invocation: ToolInvocation) -> None:
        self._close_stream()
        print(
            "  "
            + self.p("▶", "yellow")
            + " "
            + self.p(invocation.name, "yellow", "bold")
            + self.p(invocation.preview(limit=90), "grey")
        )

    def on_tool_end(self, invocation: ToolInvocation, result: ToolResult, duration: float) -> None:
        mark = self.p("✔", "green") if result.ok else self.p("✖", "red")
        summary = _summarize_tool_result(invocation.name, result)
        print(f"    {mark} {summary} {self.p(f'({duration:.2f}s)', 'grey')}")
        if self.verbose and not result.ok:
            for line in result.content.splitlines()[:6]:
                print("      " + self.p(line, "grey"))

    def on_notice(self, text: str) -> None:
        self._close_stream()
        print("  " + self.p(f"ℹ {text}", "magenta"))

    def on_retry(self, event: Any) -> None:
        self._close_stream()
        print(
            "  "
            + self.p(
                f"↻ 第 {event.attempt}/{event.max_attempts} 次失败，"
                f"{event.delay:.1f}s 后重试：{event.reason}",
                "yellow",
            )
        )

    # -- 辅助 ---------------------------------------------------------- #
    def _close_stream(self) -> None:
        if self._formatter is not None:
            tail = self._formatter.flush()
            if tail:
                sys.stdout.write(tail)
            self._formatter = None
        if self._streamed:
            sys.stdout.write("\n")
            sys.stdout.flush()
            self._streamed = False

    def finish(self) -> None:
        self._close_stream()

    def summary(self, result: AgentResult) -> None:
        self.finish()
        usage = result.usage
        bits = [
            f"{result.duration:.1f}s",
            f"{len(result.steps)} 步",
            f"{result.tool_calls} 次读取",
        ]
        if usage.total_tokens:
            bits.append(f"tokens {usage.prompt_tokens}↑/{usage.completion_tokens}↓")
        if result.stop_reason == "step_limit":
            bits.append("已达步数上限")
        print(self.p("\n  ⏱ " + " · ".join(bits), "grey"))


def _relative_to(target: Path, base: Path) -> str:
    """把 ``target`` 表示为相对 ``base`` 的短路径；不在其内则返回绝对路径。"""
    try:
        rel = target.relative_to(Path(base).resolve())
    except (ValueError, OSError):
        return str(target)
    return "." if str(rel) == "." else rel.as_posix()


def _summarize_tool_result(name: str, result: ToolResult) -> str:
    """给工具结果生成一行摘要，避免刷屏。"""
    if not result.ok:
        first = next(
            (line for line in result.content.splitlines() if line.strip()), "失败"
        )
        return first.strip()[:120]
    meta = result.meta or {}
    if name == "list_dir":
        return f"{meta.get('count', '?')} 个条目"
    if name == "read_file":
        total = meta.get("total_lines")
        start, end = meta.get("start"), meta.get("end")
        if total is not None and start is not None:
            return f"{meta.get('path', '')} 第 {start}-{end} 行 / 共 {total} 行"
        return f"{total or '?'} 行"
    if name == "search_code":
        return f"{meta.get('matches', 0)} 处命中 / {meta.get('files', 0)} 个文件"
    return "成功"


# --------------------------------------------------------------------------- #
# 会话
# --------------------------------------------------------------------------- #
@dataclass
class SessionOptions:
    """命令行传入的一次性选项。"""

    mock: bool = False
    color: bool | None = None
    verbose: bool = False
    workspace: Path | None = None
    no_save: bool = False


class ReviewSession:
    """审查会话：命令解析 + Agent 装配 + 报告保存。"""

    def __init__(
        self,
        store: ConfigStore,
        binding: Binding,
        options: SessionOptions,
    ) -> None:
        self.store = store
        self.binding = binding
        self.options = options

        color = supports_color() if options.color is None else options.color
        if options.mock:
            color = options.color if options.color is not None else color
        self.p = Palette(color)
        self.renderer = ReviewRenderer(self.p, verbose=options.verbose)

        self.stats = ReviewStats()
        self.agent: CodeAgent | None = None
        self.agent_workspace: Path | None = None
        self.last_report: str = ""
        self.last_report_target: str = ""

    # ------------------------------------------------------------------ #
    # Agent 装配
    # ------------------------------------------------------------------ #
    def _effective_workspace(self, target: Path | None) -> Path:
        """决定工具沙箱的根目录。

        给定目标时以它（文件则取父目录）为根，这样 Agent 能读到该文件的
        兄弟文件、被调函数的定义等 —— 审查需要这种"邻里上下文"。
        """
        if self.options.workspace:
            return self.options.workspace.resolve()
        if target is None:
            return Path.cwd().resolve()
        target = target.resolve()
        return target if target.is_dir() else target.parent

    def _ensure_agent(self, workspace: Path) -> CodeAgent:
        """构建（或复用）Agent。

        工作目录变了就重建 —— 否则旧的沙箱根会把新目标挡在外面，
        报出令人困惑的"拒绝访问工作目录之外的路径"。
        """
        if self.agent is not None and self.agent_workspace == workspace:
            return self.agent

        context = ToolContext(
            workspace=workspace,
            allow_write=False,  # 审查模式强制只读（双保险：工具集本身也只有读的）
            max_output=16_000,
            command_timeout=30.0,
        )
        registry = build_review_registry(context)

        settings = Settings(
            api_key=self.binding.api_key,
            base_url=self.binding.base_url,
            model=self.binding.model,
            temperature=self.binding.temperature,
            max_steps=self.binding.max_steps,
            max_tool_output=16_000,
            context_token_budget=80_000,
            allow_write=False,
            workspace=workspace,
            stream=not self.options.mock,
            mock=self.options.mock,
        )

        if self.options.mock:
            from .mock_llm import MockLLM

            llm: Any = MockLLM(settings)
        else:
            llm = LLMClient(settings, on_retry=self.renderer.on_retry)

        agent = CodeAgent(
            llm=llm,
            registry=registry,
            settings=settings,
            hooks=self.renderer.hooks(),
            tool_context=context,
            system_prompt_template=REVIEW_SYSTEM_PROMPT,
        )

        if self.agent is not None and self.agent_workspace != workspace:
            print(self.p(f"ℹ 审查目录已切换到 {workspace}，上下文已重置", "grey"))
        self.agent = agent
        self.agent_workspace = workspace
        return agent

    # ------------------------------------------------------------------ #
    # 主循环
    # ------------------------------------------------------------------ #
    def run(self, initial: str | None = None) -> int:
        self.print_banner()
        if initial:
            try:
                if self.handle(initial):
                    return 0
            except KeyboardInterrupt:
                print()

        while True:
            try:
                line = input(self.p("\n审查 ▸ ", "green", "bold")).strip()
            except (EOFError, KeyboardInterrupt):
                print(self.p("\n再见。", "grey"))
                return 0
            if not line:
                continue
            try:
                if self.handle(line):
                    return 0
            except KeyboardInterrupt:
                self.renderer.finish()
                print(self.p("\n⏹ 已中断本次审查", "yellow"))
            except AgentError as exc:
                self.renderer.finish()
                print(self.p(f"✖ {exc}", "red"))

    # ------------------------------------------------------------------ #
    # 命令分发
    # ------------------------------------------------------------------ #
    def handle(self, line: str) -> bool:
        """处理一行输入；返回 True 表示退出。"""
        command, _, argument = line.partition(" ")
        command = command.strip().lower()
        argument = argument.strip()

        if command in {"exit", "quit", "q", "退出"}:
            print(self.p("再见。", "grey"))
            return True
        if command in {"help", "?", "h", "帮助"}:
            print(self.p(HELP_TEXT, "grey"))
            return False
        if command == "api":
            self.cmd_api(argument)
            return False
        if command == "model":
            self.cmd_set("model", argument)
            return False
        if command == "url":
            self.cmd_set("base_url", argument)
            return False
        if command == "review":
            self.cmd_review(argument)
            return False
        if command == "save":
            self.cmd_save(argument)
            return False
        if command == "config":
            self.cmd_config()
            return False

        # 非命令输入：是路径就审查，否则当作自然语言的审查要求
        candidate = Path(argument or line).expanduser()
        if candidate.exists():
            self.cmd_review(argument or line)
        else:
            self.cmd_instruction(line)
        return False

    # -- api ----------------------------------------------------------- #
    def cmd_api(self, argument: str) -> None:
        if argument.lower() == "show":
            self._print_binding()
            return
        if argument.lower() == "clear":
            if self.store.clear():
                self.binding = Binding()
                print(self.p("✔ 已解除绑定", "green"))
            else:
                print(self.p("✖ 删除配置文件失败", "red"))
            return

        key = argument
        if not key:
            key = ask_secret("请输入 API Key（输入时不显示，直接回车取消）: ")
            if not key:
                print(self.p("已取消。", "grey"))
                return

        self.binding.api_key = key
        self._verify_and_save()

    def _verify_and_save(self) -> None:
        print(self.p(f"\n正在校验… （{self.binding.base_url} / {self.binding.model}）", "grey"))
        result = verify_binding(
            self.binding.api_key, self.binding.base_url, self.binding.model
        )

        self.binding.verified = result.verified
        self.binding.verified_at = datetime.now().isoformat(timespec="seconds")
        self.binding.verified_note = result.message

        if result.level == "ok":
            print(self.p(f"✔ 绑定成功：{result.message}", "green", "bold"))
        elif result.level == "warn":
            print(self.p(f"⚠ {result.message}", "yellow", "bold"))
        else:
            print(self.p(f"✖ 绑定失败：{result.message}", "red", "bold"))
        if result.detail:
            print(self.p(f"  服务端返回：{result.detail}", "grey"))
        if result.latency:
            print(self.p(f"  耗时 {result.latency:.2f}s", "grey"))

        ok, path, error = self.store.save(self.binding)
        if ok:
            print(self.p(f"  已保存到 {path}", "grey"))
            print(
                self.p(
                    "  注意：密钥以明文保存在该文件中，请勿提交到版本库或分享。",
                    "grey",
                )
            )
        else:
            print(self.p(f"✖ 保存失败：{error}", "red"))

        # Agent 里的密钥是构造时快照的，绑定变了必须重建
        self.agent = None

        if result.level == "fail":
            print(
                self.p(
                    "  提示：可先用 url / model 指令确认端点和模型名，再重新执行 api。",
                    "grey",
                )
            )

    # -- model / url ---------------------------------------------------- #
    def cmd_set(self, field: str, argument: str) -> None:
        label = "模型" if field == "model" else "接口端点"
        if not argument:
            print(f"当前{label}：{getattr(self.binding, field)}")
            return

        setattr(self.binding, field, argument)
        # 端点或模型一变，之前的校验结论就作废了
        self.binding.verified = False
        self.binding.verified_note = "参数已修改，尚未重新校验"

        ok, path, error = self.store.save(self.binding)
        print(self.p(f"✔ {label}已设为 {argument}", "green"))
        if ok:
            print(self.p(f"  已保存到 {path}", "grey"))
        else:
            print(self.p(f"  保存失败：{error}", "red"))
        self.agent = None
        if self.binding.bound:
            print(self.p("  提示：参数变了，建议重新执行 api 校验一次。", "grey"))

    # -- review --------------------------------------------------------- #
    def cmd_review(self, argument: str) -> None:
        target_text = argument
        if not target_text:
            target_text = ask("请输入要审查的文件或目录路径: ", self.p)
            if not target_text:
                print(self.p("已取消。", "grey"))
                return

        target = Path(target_text).expanduser()
        if not target.is_absolute():
            target = (Path.cwd() / target).resolve()
        else:
            target = target.resolve()

        if not target.exists():
            print(self.p(f"✖ 路径不存在：{target}", "red"))
            return

        workspace = self._effective_workspace(target)
        if self.options.workspace:
            try:
                target.relative_to(workspace)
            except ValueError:
                print(
                    self.p(
                        f"✖ 目标不在指定的工作目录内：{target}\n  工作目录：{workspace}",
                        "red",
                    )
                )
                return

        # 关键：交给模型的路径必须是**相对工具沙箱根**的，否则模型按字面
        # 传给 read_file 时会解析到错误位置（沙箱根不是 cwd）。
        task_display = _relative_to(target, workspace)
        human_display = _relative_to(target, Path.cwd())
        task = build_review_task(task_display)
        self._run_review(task, display=human_display, workspace=workspace)

    def cmd_instruction(self, instruction: str) -> None:
        workspace = self._effective_workspace(None)
        task = build_review_instruction(instruction, str(workspace))
        self._run_review(task, display=instruction, workspace=workspace)

    def _run_review(self, task: str, *, display: str, workspace: Path) -> None:
        if not self.binding.bound and not self.options.mock:
            print(self.p("✖ 还没有绑定 API Key，请先输入 api 命令。", "red"))
            return

        if not self.binding.verified and self.binding.bound and self.binding.verified_note:
            print(self.p(f"ℹ 上次校验：{self.binding.verified_note}", "grey"))

        agent = self._ensure_agent(workspace)
        print(self.p(f"\n审查目标：{display}", "cyan"))
        print(self.p(f"工作目录：{workspace}", "grey"))

        try:
            result = agent.run(task)
        except LLMError as exc:
            self.renderer.finish()
            print(self.p(f"\n✖ 调用模型失败：{exc.message}", "red"))
            self._print_llm_hint(exc)
            return

        self.renderer.summary(result)
        self.stats.reviews += 1
        self.stats.steps += len(result.steps)
        self.stats.tool_calls += result.tool_calls
        self.stats.prompt_tokens += result.usage.prompt_tokens
        self.stats.completion_tokens += result.usage.completion_tokens

        if result.answer:
            self.last_report = result.answer
            self.last_report_target = display
            print(
                self.p(
                    "\n  （输入 save 可把这份报告保存为 Markdown 文件）", "grey"
                )
            )

    def _print_llm_hint(self, exc: LLMError) -> None:
        status = getattr(exc, "status_code", None)
        hints = {
            401: "密钥无效，请重新执行 api 绑定。",
            402: "账户余额不足。",
            403: "该密钥没有访问当前模型的权限，可用 model 指令换一个模型。",
            404: "端点或模型不存在，可用 url / model 指令检查。",
            429: "触发限流，稍后重试。",
        }
        if status in hints:
            print(self.p(f"  ↳ {hints[status]}", "yellow"))

    # -- save ----------------------------------------------------------- #
    def cmd_save(self, argument: str) -> None:
        if not self.last_report:
            print(self.p("还没有可保存的审查报告。", "red"))
            return

        if argument:
            path = Path(argument).expanduser()
            if not path.is_absolute():
                path = (Path.cwd() / path).resolve()
        else:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            base = Path.cwd()
            path = base / f"codereview-{stamp}.md"

        header = (
            f"# 代码审查报告\n\n"
            f"- 审查目标：`{self.last_report_target}`\n"
            f"- 模型：`{self.binding.model}`\n"
            f"- 时间：{datetime.now().isoformat(timespec='seconds')}\n"
            f"- 工具：CodeReview v{__version__}\n\n"
            f"---\n\n"
        )
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(header + self.last_report + "\n", encoding="utf-8")
        except OSError as exc:
            print(self.p(f"✖ 保存失败：{exc}", "red"))
            return
        print(self.p(f"✔ 报告已保存到 {path}", "green"))

    # -- config --------------------------------------------------------- #
    def cmd_config(self) -> None:
        print(self.p("\n当前配置：", "cyan", "bold"))
        self._print_binding()
        print(f"  {pad('max_steps', 12)} = {self.binding.max_steps}")
        print(f"  {pad('temperature', 12)} = {self.binding.temperature}")
        print(f"  {pad('运行模式', 12)} = {'离线 Mock（--mock）' if self.options.mock else '真实模型'}")
        if self.agent_workspace:
            print(f"  {pad('当前工作目录', 12)} = {self.agent_workspace}")
        print(self.p(f"\n  {config_precedence_hint()}", "grey"))
        print(self.p(f"  程序目录：{app_dir()}", "grey"))
        print(self.p(f"  实际使用：{self.store.path()}", "grey"))
        if self.stats.reviews:
            print(
                self.p(
                    f"\n  本次运行累计：{self.stats.reviews} 次审查 · "
                    f"{self.stats.steps} 步 · {self.stats.tool_calls} 次读取 · "
                    f"tokens {self.stats.prompt_tokens}↑/{self.stats.completion_tokens}↓",
                    "grey",
                )
            )

    def _print_binding(self) -> None:
        b = self.binding
        status = (
            self.p("已校验通过", "green")
            if b.verified
            else self.p(b.verified_note or "未校验", "yellow")
        )
        print(f"  {pad('API Key', 12)} = {b.masked_key()}")
        print(f"  {pad('接口端点', 12)} = {b.base_url}")
        print(f"  {pad('模型', 12)} = {b.model}")
        print(f"  {pad('校验状态', 12)} = {status}")
        if b.verified_at:
            print(self.p(f"  {pad('校验时间', 12)} = {b.verified_at}", "grey"))

    # ------------------------------------------------------------------ #
    def print_banner(self) -> None:
        print(self.p(BANNER, "cyan"))
        mode = "离线 Mock 模型" if self.options.mock else self.binding.model
        print(f"  CodeReview v{__version__} — 代码错误审查 Agent")
        print(f"  模型    : {self.p(mode, 'bold')}")
        if not self.options.mock:
            print(f"  端点    : {self.binding.base_url}")
            print(f"  密钥    : {self.binding.masked_key()}")
        print(f"  Python  : {platform.python_version()}")
        print(f"  程序目录: {app_dir()}")
        if not self.binding.bound and not self.options.mock:
            print(self.p("  尚未绑定 API Key —— 请输入 api 开始绑定。", "yellow"))
        print(self.p("  输入 help 查看全部指令，exit 退出。", "grey"))


# --------------------------------------------------------------------------- #
# 命令行入口
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="CodeReview",
        description="CodeReview —— 审查代码中可能存在的错误的 Agent。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  CodeReview.exe                        进入交互式审查会话\n"
            "  CodeReview.exe help                   打印指令说明后退出\n"
            "  CodeReview.exe api sk-xxxx            绑定并校验 API Key\n"
            "  CodeReview.exe review src             审查 src 目录\n"
            "  CodeReview.exe review src\\app.py      审查单个文件\n"
            "  CodeReview.exe --workspace D:\\proj    指定工具可访问的根目录\n"
            "  CodeReview.exe --mock                 离线演示（无需密钥）\n"
        ),
    )
    parser.add_argument("command", nargs="?", help="要执行的指令：help / api / review / config / exit")
    parser.add_argument("argument", nargs="?", help="指令参数，例如路径或 API Key")
    parser.add_argument("-w", "--workspace", help="工具可访问的根目录（默认由审查目标推导）")
    parser.add_argument("--mock", action="store_true", help="离线 Mock 模型，无需 API Key")
    parser.add_argument("--no-color", action="store_true", help="关闭彩色输出")
    parser.add_argument("-v", "--verbose", action="store_true", help="显示更多细节")
    parser.add_argument("-V", "--version", action="version", version=f"CodeReview {__version__}")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    configure_stdio()
    args = build_parser().parse_args(argv)

    store = ConfigStore()
    binding = store.load()

    options = SessionOptions(
        mock=bool(args.mock),
        color=False if args.no_color else None,
        verbose=bool(args.verbose),
        workspace=Path(args.workspace).expanduser().resolve() if args.workspace else None,
    )
    session = ReviewSession(store, binding, options)

    command = (args.command or "").strip().lower()
    argument = (args.argument or "").strip()

    # 一次性指令：跑完就退出，适合脚本与批处理
    if command in {"help", "?", "h"}:
        print(session.p(HELP_TEXT))
        return 0
    if command in {"config"}:
        session.cmd_config()
        return 0
    if command in {"exit", "quit", "q"}:
        return 0

    # 单次执行模式：api / review / 或直接给路径
    if command:
        seed = f"{command} {argument}".strip()
        if command == "api" and not binding.bound and not argument and not options.mock:
            # 让交互式的"请输入 API Key"提示照常生效
            pass
        try:
            if command == "api":
                session.cmd_api(argument)
            elif command == "review":
                session.cmd_review(argument)
            elif command in {"model", "url"}:
                session.cmd_set("model" if command == "model" else "base_url", argument)
            elif Path(f"{command} {argument}".strip()).exists():
                session.cmd_review(seed)
            else:
                print(session.p(f"✖ 未知指令：{command}（可用 help 查看全部指令）", "red"))
                return 2
        except KeyboardInterrupt:
            print()
            return 130
        return 0

    return session.run()


__all__ = ["main", "ReviewSession", "SessionOptions", "HELP_TEXT", "build_parser"]
