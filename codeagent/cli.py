"""命令行交互层：完整助手模式的终端渲染、REPL、参数解析。

这一层只做两件事：**把用户的话交给 Agent**，以及**把 Agent 的过程画出来**。
所有业务逻辑都在 :mod:`codeagent.agent`，因此 CLI 可以整体换成 Web 界面而不动内核
（这也是本项目把 ``hooks`` 设计成回调的原因）。

代码审查模式是默认入口，见 :mod:`codeagent.review_cli`；
本模块对应的是保留的"完整助手模式"（可写文件、执行命令）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Sequence

from .agent import AgentHooks, AgentResult, CodeAgent, ToolInvocation
from .config import Settings, default_dotenv_path
from .errors import AgentError, ConfigError, LLMError
from .llm import LLMClient, RetryEvent
from .tools import ToolContext, ToolResult, build_default_registry
from .ui import Palette as _Palette
from .ui import configure_stdio

BANNER = r"""
   ____          _         _                    _
  / ___|___   __| | ___   / \   __ _  ___ _ __ | |_
 | |   / _ \ / _` |/ _ \ / _ \ / _` |/ _ \ '_ \| __|
 | |__| (_) | (_| |  __// ___ \ (_| |  __/ | | | |_
  \____\___/ \__,_|\___/_/   \_\__, |\___|_| |_|\__|
                               |___/   代码助手 Agent v{version}
"""


# --------------------------------------------------------------------------- #
# 终端着色
# --------------------------------------------------------------------------- #
class Palette(_Palette):
    """极简 ANSI 着色器。

    实现已抽到 :mod:`codeagent.ui`，与代码审查模式共用一份；
    这里保留子类只是为了让 ``cli.Palette`` 这个历史名字继续可用。
    """


# --------------------------------------------------------------------------- #
# 终端渲染器
# --------------------------------------------------------------------------- #
class TerminalRenderer:
    """把 Agent 的事件流渲染成可读的终端输出。"""

    def __init__(self, color: bool = True, verbose: bool = False) -> None:
        self.c = Palette(color)
        self.verbose = verbose
        self._streamed_any = False
        self._in_step = False

    # -- 构造钩子 ------------------------------------------------------ #
    def hooks(self) -> AgentHooks:
        return AgentHooks(
            on_step_start=self.on_step_start,
            on_assistant_text=self.on_assistant_text,
            on_assistant_message=self.on_assistant_message,
            on_tool_start=self.on_tool_start,
            on_tool_end=self.on_tool_end,
            on_notice=self.on_notice,
            on_retry=self.on_retry,
            on_final=self.on_final,
        )

    # -- 事件 ---------------------------------------------------------- #
    def on_step_start(self, step: int, total: int) -> None:
        self._close_stream()
        print(self.c(f"\n▸ 第 {step}/{total} 步", "cyan", "bold"))
        self._in_step = True

    def on_assistant_text(self, delta: str) -> None:
        if not self._streamed_any:
            self._streamed_any = True
        sys.stdout.write(delta)
        sys.stdout.flush()

    def on_assistant_message(self, text: str) -> None:
        if not self._streamed_any and text.strip():
            print(self.c(text, "grey"))

    def on_tool_start(self, invocation: ToolInvocation) -> None:
        self._close_stream()
        print(
            "  "
            + self.c("▶", "yellow")
            + " "
            + self.c(invocation.name, "yellow", "bold")
            + self.c(invocation.preview(), "grey")
        )

    def on_tool_end(self, invocation: ToolInvocation, result: ToolResult, duration: float) -> None:
        mark = self.c("✔", "green") if result.ok else self.c("✖", "red")
        status = self.c("成功", "green") if result.ok else self.c(
            result.error_kind or "失败", "red"
        )
        print(f"    {mark} {status} {self.c(f'({duration:.2f}s)', 'grey')}")
        for line in _preview(result.content):
            print("      " + self.c(line, "grey"))

    def on_notice(self, text: str) -> None:
        self._close_stream()
        print("  " + self.c(f"ℹ {text}", "magenta"))

    def on_retry(self, event: RetryEvent) -> None:
        self._close_stream()
        print(
            "  "
            + self.c(
                f"↻ 第 {event.attempt}/{event.max_attempts} 次尝试失败，"
                f"{event.delay:.1f}s 后重试：{event.reason}",
                "yellow",
            )
        )

    def on_final(self, text: str) -> None:
        self._close_stream()

    # -- 辅助 ---------------------------------------------------------- #
    def _close_stream(self) -> None:
        """流式输出结束后补一个换行，避免后续提示粘连。"""
        if self._streamed_any:
            sys.stdout.write("\n")
            sys.stdout.flush()
            self._streamed_any = False

    def finish(self) -> None:
        self._close_stream()

    def summary(self, result: AgentResult) -> None:
        self.finish()
        u = result.usage
        parts = [
            f"{result.duration:.1f}s",
            f"{len(result.steps)} 步",
            f"{result.tool_calls} 次工具调用",
        ]
        if result.failed_tools:
            parts.append(self.c(f"{result.failed_tools} 次失败", "red"))
        if u.total_tokens:
            parts.append(f"tokens {u.prompt_tokens}↑/{u.completion_tokens}↓")
        if result.stop_reason == "step_limit":
            parts.append(self.c("已达步数上限", "yellow"))
        print(self.c("  ⏱ " + " · ".join(parts), "grey"))


def _preview(content: str, lines: int = 3, width: int = 140) -> list[str]:
    """截取工具输出的前几行用于展示。"""
    cleaned = [
        line.rstrip()
        for line in (content or "").splitlines()
        if line.strip() and not line.strip().startswith("<tool_result")
    ]
    shown = cleaned[:lines]
    out: list[str] = []
    for line in shown:
        out.append(line if len(line) <= width else line[: width - 1] + "…")
    if len(cleaned) > lines:
        out.append(f"... 还有 {len(cleaned) - lines} 行")
    return out or ["(无输出)"]


# --------------------------------------------------------------------------- #
# 参数解析
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="code-agent",
        description="CodeAgent —— 一个可调用工具（读写文件、检索代码、执行命令）的代码助手 Agent。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python main.py                              # 进入交互式会话\n"
            "  python main.py --mock                       # 无需 API Key 的离线演示\n"
            "  python main.py -p \"解释一下 codeagent/agent.py 的主循环\"\n"
            "  python main.py --workspace D:\\proj --read-only\n"
            "  echo \"这个项目有哪些 Python 文件?\" | python main.py\n"
        ),
    )
    parser.add_argument("-p", "--prompt", help="单次提问后退出（不进入交互模式）")
    parser.add_argument("-w", "--workspace", help="工作目录（沙箱根），默认当前目录")
    parser.add_argument("--base-url", help="OpenAI 兼容端点，例如 https://api.deepseek.com/v1")
    parser.add_argument("--api-key", help="API Key（更推荐用环境变量或 .env）")
    parser.add_argument("-m", "--model", help="模型名，例如 deepseek-chat / qwen-plus")
    parser.add_argument("--temperature", type=float, help="采样温度，默认 0.2")
    parser.add_argument("--max-steps", type=int, help="单轮最多多少次推理-工具循环，默认 12")
    parser.add_argument("--max-tool-output", type=int, help="单个工具返回给模型的字符上限")
    parser.add_argument("--context-token-budget", type=int, help="上下文 token 预算，默认 60000")
    parser.add_argument("--max-retries", type=int, help="API 失败重试次数，默认 3")
    parser.add_argument("--timeout", type=float, help="单次 API 请求超时秒数，默认 120")
    parser.add_argument("--command-timeout", type=float, help="工具执行命令的默认超时秒数")
    parser.add_argument("--stream", action=argparse.BooleanOptionalAction, default=None,
                        help="是否流式输出（默认开启）")
    parser.add_argument("--read-only", action="store_true", default=None,
                        help="只读模式：禁止写文件与执行修改类命令")
    parser.add_argument("--mock", action="store_true", default=None,
                        help="使用离线 Mock 模型，无需 API Key")
    parser.add_argument("--color", action=argparse.BooleanOptionalAction, default=None,
                        help="是否彩色输出（默认自动判断 TTY）")
    parser.add_argument("-v", "--verbose", action="store_true", default=None,
                        help="输出更多诊断信息")
    parser.add_argument("--show-config", action="store_true", help="打印生效配置后退出（密钥已脱敏）")
    parser.add_argument("--list-tools", action="store_true", help="列出所有工具后退出")
    return parser


def _cli_overrides(args: argparse.Namespace) -> dict[str, Any]:
    """把 argparse 的结果映射成 :class:`Settings` 的字段名。"""
    overrides: dict[str, Any] = {
        "api_key": args.api_key,
        "base_url": args.base_url,
        "model": args.model,
        "temperature": args.temperature,
        "max_steps": args.max_steps,
        "max_tool_output": args.max_tool_output,
        "context_token_budget": args.context_token_budget,
        "max_retries": args.max_retries,
        "timeout": args.timeout,
        "command_timeout": args.command_timeout,
        "stream": args.stream,
        "mock": args.mock,
        "verbose": args.verbose,
        "color": args.color,
    }
    if args.workspace:
        overrides["workspace"] = args.workspace
    if args.read_only is not None:
        overrides["allow_write"] = not args.read_only
    return overrides


def build_settings(args: argparse.Namespace) -> Settings:
    """创建并校验配置。"""
    cwd = Path.cwd()
    settings = Settings.load(
        cli_args=_cli_overrides(args),
        dotenv_path=default_dotenv_path(cwd),
        cwd=cwd,
    )
    if settings.color is None:
        settings.color = sys.stdout.isatty()
    settings.validate()
    return settings


def build_agent(settings: Settings, renderer: TerminalRenderer) -> CodeAgent:
    """组装 Agent：工具上下文 → 注册表 → LLM 客户端 → 主循环。"""
    context = ToolContext(
        workspace=settings.workspace,
        allow_write=settings.allow_write,
        max_output=settings.max_tool_output,
        command_timeout=settings.command_timeout,
    )
    registry = build_default_registry(context)

    if settings.mock:
        from .mock_llm import MockLLM

        llm: Any = MockLLM(settings)
    else:
        llm = LLMClient(settings, on_retry=renderer.on_retry)

    memory = None  # 交给 CodeAgent 构造（system prompt 需要工具目录）
    agent = CodeAgent(
        llm=llm,
        registry=registry,
        settings=settings,
        memory=memory,
        hooks=renderer.hooks(),
        tool_context=context,
    )
    return agent


# --------------------------------------------------------------------------- #
# 交互会话
# --------------------------------------------------------------------------- #
HELP_TEXT = """\
可用命令：
  /help            显示本帮助
  /tools           列出已注册的工具及其参数
  /status          显示上下文用量、累计 token 与工具调用统计
  /history [n]     打印最近 n 条消息（默认 20）
  /clear           清空对话历史（保留系统提示与统计）
  /model <名称>     切换模型（对后续请求生效）
  /config          显示当前生效配置（密钥脱敏）
  /exit, /quit     退出

直接输入自然语言即可提问。Ctrl+C 可中断当前这一轮，再按一次退出。
"""


class Session:
    """一次交互式会话。"""

    def __init__(
        self,
        agent: CodeAgent,
        settings: Settings,
        renderer: TerminalRenderer,
    ) -> None:
        self.agent = agent
        self.settings = settings
        self.renderer = renderer
        self.c = renderer.c

    # ------------------------------------------------------------------ #
    def run_once(self, prompt: str) -> AgentResult:
        """执行一轮提问，并把异常转成友好的终端提示。"""
        try:
            result = self.agent.run(prompt)
        except LLMError as exc:
            self.renderer.finish()
            self._print_llm_error(exc)
            self.agent.memory.rollback_incomplete_turn()
            return AgentResult(answer="", stop_reason="error", error=exc.message)
        except AgentError as exc:
            self.renderer.finish()
            print(self.c(f"✖ {exc.message}", "red"))
            self.agent.memory.rollback_incomplete_turn()
            return AgentResult(answer="", stop_reason="error", error=exc.message)

        self.renderer.summary(result)
        return result

    def _print_llm_error(self, exc: LLMError) -> None:
        print(self.c(f"\n✖ 调用模型失败：{exc.message}", "red"))
        hint = _error_hint(exc)
        if hint:
            print(self.c(f"  ↳ {hint}", "yellow"))

    # ------------------------------------------------------------------ #
    def repl(self) -> int:
        self.print_banner()
        while True:
            try:
                line = input(self.c("\n你 ▸ ", "green", "bold"))
            except (EOFError, KeyboardInterrupt):
                print(self.c("\n再见！", "grey"))
                return 0

            text = line.strip()
            if not text:
                continue
            if text.startswith("/"):
                if self.handle_command(text):
                    return 0
                continue

            try:
                self.run_once(text)
            except KeyboardInterrupt:
                self.renderer.finish()
                removed = self.agent.memory.rollback_incomplete_turn()
                print(self.c(f"\n⏹ 已中断本轮（回滚 {removed} 条消息）", "yellow"))
        return 0  # pragma: no cover

    # ------------------------------------------------------------------ #
    def print_banner(self) -> None:
        from . import __version__

        print(self.c(BANNER.format(version=__version__), "cyan"))
        mode = "离线 Mock 模型" if self.settings.mock else self.settings.model
        print(f"  模型    : {self.c(mode, 'bold')}")
        if not self.settings.mock:
            print(f"  端点    : {self.settings.base_url}")
        print(f"  工作目录: {self.settings.workspace}")
        print(
            f"  写权限  : "
            + (self.c("开启（沙箱内）", "yellow") if self.settings.allow_write
               else self.c("只读", "green"))
            + f"   步数上限: {self.settings.max_steps}"
        )
        print(self.c("  输入 /help 查看命令，Ctrl+C 中断当前轮。", "grey"))

    # ------------------------------------------------------------------ #
    def handle_command(self, text: str) -> bool:
        """处理斜杠命令；返回 True 表示要退出。"""
        parts = text.split(maxsplit=1)
        command = parts[0].lower()
        argument = parts[1].strip() if len(parts) > 1 else ""

        if command in {"/exit", "/quit", "/q"}:
            print(self.c("再见！", "grey"))
            return True

        if command == "/help":
            print(self.c(HELP_TEXT, "grey"))

        elif command == "/tools":
            print(self.c("\n已注册工具：", "cyan", "bold"))
            print(self.agent.registry.describe())

        elif command == "/status":
            stats = self.agent.memory.stats()
            usage = self.agent.total_usage
            bar = _progress_bar(stats.usage_ratio)
            print(self.c("\n上下文状态：", "cyan", "bold"))
            print(f"  历史消息  : {stats.total_messages} 条（本轮发送 {stats.kept_messages} 条，"
                  f"省略 {stats.dropped_messages} 条）")
            print(f"  估算 token: {stats.estimated_tokens} / {stats.token_budget}  {bar}")
            print(f"  对话轮次  : {stats.turns}")
            print(f"  累计用量  : prompt {usage.prompt_tokens}, completion {usage.completion_tokens}, "
                  f"合计 {usage.total_tokens}")
            print(f"  工具调用  : 累计 {self.agent.total_tool_calls} 次")

        elif command == "/history":
            limit = int(argument) if argument.isdigit() else 20
            print(self.c(f"\n最近 {limit} 条消息：", "cyan", "bold"))
            print(self.c(self.agent.memory.transcript(limit=limit), "grey"))

        elif command == "/clear":
            self.agent.reset()
            print(self.c("✔ 对话历史已清空", "green"))

        elif command == "/model":
            if not argument:
                print(f"当前模型：{self.settings.model}")
            else:
                self.settings.model = argument
                print(self.c(f"✔ 已切换模型为 {argument}", "green"))

        elif command == "/config":
            _print_config(self.settings)

        else:
            print(self.c(f"未知命令 {command}，输入 /help 查看帮助。", "yellow"))
        return False


def _progress_bar(ratio: float, width: int = 24) -> str:
    filled = int(round(ratio * width))
    return "[" + "█" * filled + "·" * (width - filled) + f"] {ratio * 100:.0f}%"


def _error_hint(exc: LLMError) -> str:
    """针对常见错误给出可操作的排查建议。"""
    status = getattr(exc, "status_code", None)
    hints = {
        401: "检查 CODEAGENT_API_KEY 是否正确、是否带了多余空格。",
        403: "该 Key 可能没有此模型的权限，或需要先开通对应服务。",
        404: "检查 base_url 是否包含 /v1，以及模型名是否拼写正确。",
        429: "请求过于频繁，稍后重试或降低并发；也可调大 --max-retries。",
    }
    if status in hints:
        return hints[status]
    if getattr(exc, "retryable", False):
        return "该错误可重试，已自动重试若干次仍失败，请检查网络或服务端状态。"
    return ""


# --------------------------------------------------------------------------- #
# 展示辅助
# --------------------------------------------------------------------------- #
def _print_config(settings: Settings) -> None:
    print(Palette(True)("\n当前生效配置：", "cyan", "bold"))
    for key, value in settings.redacted().items():
        source = settings.sources.get(key, "default")
        print(f"  {key:<22} = {str(value):<48} [{source}]")


def _configure_stdio() -> None:
    """兼容旧名字；实际实现在 :func:`codeagent.ui.configure_stdio`。"""
    configure_stdio()


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def main(argv: Sequence[str] | None = None) -> int:
    _configure_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)

    # --show-config / --list-tools 不需要 API Key，先处理掉。
    if args.list_tools:
        settings = Settings.load(cli_args=_cli_overrides(args), cwd=Path.cwd())
        settings.workspace = settings.workspace.resolve()
        context = ToolContext(
            workspace=settings.workspace,
            allow_write=settings.allow_write,
            max_output=settings.max_tool_output,
            command_timeout=settings.command_timeout,
        )
        registry = build_default_registry(context)
        print(f"共 {len(registry)} 个工具（* 表示必填参数）：\n")
        print(registry.describe())
        return 0

    try:
        settings = build_settings(args)
    except ConfigError as exc:
        renderer = TerminalRenderer(color=sys.stdout.isatty())
        print(renderer.c("✖ 配置错误", "red", "bold"))
        print(renderer.c(str(exc), "yellow"))
        return 2

    renderer = TerminalRenderer(color=bool(settings.color), verbose=bool(settings.verbose))

    if args.show_config:
        _print_config(settings)
        return 0

    agent = build_agent(settings, renderer)

    # --- 单次提问模式 ------------------------------------------------ #
    prompt = args.prompt
    if prompt is None and not sys.stdin.isatty():
        piped = sys.stdin.read().strip()
        if piped:
            prompt = piped

    if prompt:
        session = Session(agent, settings, renderer)
        if not settings.mock:
            mode = f"{settings.model} @ {settings.base_url}"
        else:
            mode = "离线 Mock 模型"
        print(renderer.c(f"# CodeAgent 单次模式 · {mode} · {settings.workspace}", "grey"))
        result = session.run_once(prompt)
        if result.stop_reason == "error":
            return 1
        if result.answer:
            print()  # 答案已由流式或 on_final 输出，这里只补一个空行
        return 0

    # --- 交互模式 ---------------------------------------------------- #
    session = Session(agent, settings, renderer)
    try:
        return session.repl()
    except KeyboardInterrupt:
        print(renderer.c("\n已退出。", "grey"))
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
