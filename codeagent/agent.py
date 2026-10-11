"""Agent 主循环：输入 → 推理 → 工具调用 → 观察 → … → 输出。

这是整个项目的核心。循环本身只有 60 行左右，剩下的都是"让它在真实模型下面
不翻车"的工程细节，也是评审最关心的部分：

======================  ==========================================================
机制                    解决的问题
======================  ==========================================================
工具结果回灌            工具失败不中断任务，而是把错误作为观察结果交回模型自纠错
孤儿 tool_calls 补齐    模型给出无法解析的参数时，仍然回一条 tool 消息，否则下一轮必被服务端拒绝
重复调用抑制            模型陷入"用同样参数反复调用"的循环时，第 3 次起直接拦截并注入纠偏提示
连续失败提醒            同一工具连续失败时提示模型换策略，而不是无限重试
步数上限兜底            达到 max_steps 后强制要求模型给出最终答案，保证一定有输出
钩子（Hooks）           把过程可视化与业务逻辑解耦，CLI 只订阅事件，不侵入循环
======================  ==========================================================
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from .config import Settings
from .errors import AgentError, LLMError
from .llm import LLMClient, LLMResponse, RetryEvent, ToolCall, Usage
from .memory import ConversationMemory
from .prompts import (
    FORCE_ANSWER_INSTRUCTION,
    REPEAT_CALL_NUDGE,
    STEP_LIMIT_NUDGE,
    TOOL_FAILURE_STREAK_NUDGE,
    build_system_prompt,
    wrap_tool_result,
)
from .tools import ToolContext, ToolRegistry, ToolResult

#: 同一工具连续失败多少次后提醒模型换策略。
FAILURE_STREAK_THRESHOLD = 2

#: 同一 (工具, 参数) 组合重复第几次后开始拦截。
REPEAT_BLOCK_THRESHOLD = 3


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
@dataclass
class ToolInvocation:
    """一次工具调用的描述（用于钩子回调与日志）。"""

    call_id: str
    name: str
    arguments: dict[str, Any]

    def preview(self, limit: int = 120) -> str:
        """紧凑的单行参数预览。"""
        if not self.arguments:
            return "()"
        parts = []
        for key, value in self.arguments.items():
            text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            text = " ".join(str(text).split())
            if len(text) > limit:
                text = text[: limit - 1] + "…"
            parts.append(f"{key}={text}")
        return "(" + ", ".join(parts) + ")"


@dataclass
class ToolOutcome:
    """一次工具调用 + 其结果。"""

    invocation: ToolInvocation
    result: ToolResult
    duration: float = 0.0
    blocked: bool = False


@dataclass
class AgentStep:
    """一步推理（可能包含多个并发工具调用）。"""

    index: int
    text: str = ""
    outcomes: list[ToolOutcome] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)

    @property
    def tool_count(self) -> int:
        return len(self.outcomes)


@dataclass
class AgentResult:
    """一次 :meth:`CodeAgent.run` 的完整结果。"""

    answer: str
    steps: list[AgentStep] = field(default_factory=list)
    stop_reason: str = "completed"  # completed | step_limit | error
    usage: Usage = field(default_factory=Usage)
    duration: float = 0.0
    error: str | None = None

    @property
    def tool_calls(self) -> int:
        return sum(step.tool_count for step in self.steps)

    @property
    def failed_tools(self) -> int:
        return sum(1 for step in self.steps for o in step.outcomes if not o.result.ok)


@dataclass
class AgentHooks:
    """过程事件回调。所有字段可选，未提供即静默。"""

    on_step_start: Callable[[int, int], None] | None = None
    on_assistant_text: Callable[[str], None] | None = None
    on_assistant_message: Callable[[str], None] | None = None
    on_tool_start: Callable[[ToolInvocation], None] | None = None
    on_tool_end: Callable[[ToolInvocation, ToolResult, float], None] | None = None
    on_notice: Callable[[str], None] | None = None
    on_retry: Callable[[RetryEvent], None] | None = None
    on_final: Callable[[str], None] | None = None

    # -- 安全触发 ------------------------------------------------------ #
    def step_start(self, step: int, total: int) -> None:
        _safe(self.on_step_start, step, total)

    def text(self, delta: str) -> None:
        _safe(self.on_assistant_text, delta)

    def assistant_message(self, text: str) -> None:
        _safe(self.on_assistant_message, text)

    def tool_start(self, invocation: ToolInvocation) -> None:
        _safe(self.on_tool_start, invocation)

    def tool_end(self, invocation: ToolInvocation, result: ToolResult, duration: float) -> None:
        _safe(self.on_tool_end, invocation, result, duration)

    def notice(self, text: str) -> None:
        _safe(self.on_notice, text)

    def retry(self, event: RetryEvent) -> None:
        _safe(self.on_retry, event)

    def final(self, text: str) -> None:
        _safe(self.on_final, text)


def _safe(callback: Callable[..., Any] | None, *args: Any) -> None:
    """钩子里的异常绝不能影响 Agent 主流程。"""
    if callback is None:
        return
    try:
        callback(*args)
    except Exception:  # pragma: no cover - 防御性
        pass


# --------------------------------------------------------------------------- #
# Agent
# --------------------------------------------------------------------------- #
class CodeAgent:
    """ReAct 风格的代码助手 Agent。"""

    def __init__(
        self,
        llm: LLMClient,
        registry: ToolRegistry,
        settings: Settings,
        *,
        memory: ConversationMemory | None = None,
        hooks: AgentHooks | None = None,
        tool_context: ToolContext | None = None,
        system_prompt_template: str | None = None,
    ) -> None:
        self.llm = llm
        self.registry = registry
        self.settings = settings
        self.hooks = hooks or AgentHooks()
        self.tool_context = tool_context or ToolContext(
            workspace=settings.workspace,
            allow_write=settings.allow_write,
            max_output=settings.max_tool_output,
            command_timeout=settings.command_timeout,
        )

        #: 换一个模板就换一种"人格与作业规程"。代码审查模式传的是
        #: :data:`codeagent.review_prompt.REVIEW_SYSTEM_PROMPT`；循环本身完全不变。
        self.system_prompt_template = system_prompt_template

        system_prompt = build_system_prompt(
            workspace=str(settings.workspace),
            tool_catalog=registry.catalog(),
            allow_write=settings.allow_write,
            template=system_prompt_template,
        )
        self.memory = memory or ConversationMemory(
            system_prompt, token_budget=settings.context_token_budget
        )

        #: 统计信息（跨多轮对话累计）。
        self.total_usage = Usage()
        self.total_tool_calls = 0

    # ------------------------------------------------------------------ #
    # 主循环
    # ------------------------------------------------------------------ #
    def run(self, user_input: str) -> AgentResult:
        """处理一轮用户输入，直到模型给出不含工具调用的最终答复。"""
        if not user_input or not user_input.strip():
            return AgentResult(answer="（空输入，已忽略）", stop_reason="completed")

        started = time.monotonic()
        self.memory.add_user(user_input.strip())

        tool_schemas = self.registry.schemas()
        steps: list[AgentStep] = []
        usage = Usage()
        repeat_counts: dict[str, int] = {}
        failure_streak: dict[str, int] = {}
        warned_streaks: set[str] = set()

        max_steps = max(1, int(self.settings.max_steps))

        for step_index in range(1, max_steps + 1):
            self.hooks.step_start(step_index, max_steps)

            response = self._invoke_llm(tool_schemas)
            usage = usage + response.usage
            self.total_usage = self.total_usage + response.usage

            # --- 没有工具调用 → 这就是最终答案 ------------------------- #
            if not response.has_tool_calls:
                answer = (response.content or "").strip()
                steps.append(AgentStep(index=step_index, text=answer, usage=response.usage))
                self.memory.add_assistant(answer)
                if answer:
                    self.hooks.final(answer)
                return AgentResult(
                    answer=answer or "（模型未返回内容）",
                    steps=steps,
                    stop_reason="completed",
                    usage=usage,
                    duration=time.monotonic() - started,
                )

            # --- 有工具调用 → 记录 assistant 消息并执行 ---------------- #
            self.memory.add_assistant(
                response.content or "",
                [call.to_message() for call in response.tool_calls],
            )
            step = AgentStep(index=step_index, text=response.content or "", usage=response.usage)

            for call in response.tool_calls:
                invocation = ToolInvocation(
                    call_id=call.id, name=call.name or "<unknown>", arguments=call.arguments
                )
                outcome = self._handle_tool_call(call, invocation, repeat_counts)
                step.outcomes.append(outcome)
                self.total_tool_calls += 1

                # 失败连击统计
                if outcome.result.ok:
                    failure_streak.pop(call.name, None)
                else:
                    failure_streak[call.name] = failure_streak.get(call.name, 0) + 1

            steps.append(step)

            # --- 注入运行时纠偏提示 ------------------------------------ #
            self._maybe_nudge(failure_streak, warned_streaks, repeat_counts)

        # ------------------------------------------------------------------ #
        # 走到这里说明用完了步数：强制收尾，保证一定有输出。
        # ------------------------------------------------------------------ #
        self.hooks.notice(f"已达到步数上限（{max_steps}），强制生成最终答案")
        self.memory.add_user(STEP_LIMIT_NUDGE.format(max_steps=max_steps))
        final_text = ""
        try:
            final = self._invoke_llm(tools=None, allow_stream=False)
            usage = usage + final.usage
            self.total_usage = self.total_usage + final.usage
            final_text = (final.content or "").strip()
        except LLMError as exc:
            final_text = (
                f"（在生成最终答案时调用模型失败：{exc.message}）\n"
                f"以下是本轮已经收集到的信息，供你参考。"
            )
            self.hooks.notice(f"收尾调用失败：{exc.message}")

        self.memory.add_assistant(final_text or "（未能在步数上限内得出结论）")
        if final_text:
            self.hooks.final(final_text)
        return AgentResult(
            answer=final_text or "（未能在步数上限内得出结论）",
            steps=steps,
            stop_reason="step_limit",
            usage=usage,
            duration=time.monotonic() - started,
        )

    # ------------------------------------------------------------------ #
    # LLM 调用
    # ------------------------------------------------------------------ #
    def _invoke_llm(
        self,
        tools: Sequence[dict[str, Any]] | None,
        *,
        allow_stream: bool = True,
    ) -> LLMResponse:
        on_text = self.hooks.on_assistant_text if allow_stream else None
        try:
            return self.llm.chat(
                self.memory.messages(),
                tools=tools or None,
                on_text=on_text,
                tool_choice=None if tools else "none",
            )
        except LLMError:
            raise
        except AgentError:
            raise
        except Exception as exc:  # 兜底，避免非预期异常直接冒泡到 CLI 崩溃
            raise LLMError(f"调用模型时发生未预期的错误：{type(exc).__name__}: {exc}") from exc

    # ------------------------------------------------------------------ #
    # 工具执行
    # ------------------------------------------------------------------ #
    def _handle_tool_call(
        self,
        call: ToolCall,
        invocation: ToolInvocation,
        repeat_counts: dict[str, int],
    ) -> ToolOutcome:
        signature = _signature(call)
        repeat_counts[signature] = repeat_counts.get(signature, 0) + 1
        count = repeat_counts[signature]

        # 模型参数无法解析：也要回一条 tool 消息占位，否则服务端会因
        # "tool_calls 没有对应的 tool 响应" 而报 400。
        if call.parse_error:
            result = ToolResult.failure(
                f"工具参数解析失败：{call.parse_error}\n"
                f"你返回的原始 arguments 为：{call.raw_arguments[:400]}\n"
                f"请重新以合法 JSON 对象的形式提供参数。",
                kind="tool.argument",
            )
            return self._record_tool_result(call, invocation, result, duration=0.0)

        # 重复调用抑制
        if count >= REPEAT_BLOCK_THRESHOLD and not call.parse_error:
            result = ToolResult.failure(
                f"已阻止第 {count} 次完全相同的调用：{call.name} {invocation.preview()}。\n"
                f"重复同样的调用不会产生新信息，请换参数、换工具，或直接给出最终答案。",
                kind="tool.duplicate",
            )
            return self._record_tool_result(call, invocation, result, duration=0.0, blocked=True)

        self.hooks.tool_start(invocation)
        started = time.monotonic()
        result = self.registry.execute(call.name, call.arguments)
        duration = time.monotonic() - started
        self.hooks.tool_end(invocation, result, duration)
        return self._record_tool_result(call, invocation, result, duration)

    def _record_tool_result(
        self,
        call: ToolCall,
        invocation: ToolInvocation,
        result: ToolResult,
        duration: float,
        *,
        blocked: bool = False,
    ) -> ToolOutcome:
        body = result.render(self.settings.max_tool_output)
        self.memory.add_tool_result(
            call.id,
            call.name or "unknown",
            wrap_tool_result(call.name or "unknown", result.ok, body),
        )
        return ToolOutcome(
            invocation=invocation, result=result, duration=duration, blocked=blocked
        )

    # ------------------------------------------------------------------ #
    # 纠偏提示
    # ------------------------------------------------------------------ #
    def _maybe_nudge(
        self,
        failure_streak: dict[str, int],
        warned: set[str],
        repeat_counts: dict[str, int],
    ) -> None:
        # 连续失败
        for name, count in list(failure_streak.items()):
            if count >= FAILURE_STREAK_THRESHOLD and name not in warned:
                warned.add(name)
                text = TOOL_FAILURE_STREAK_NUDGE.format(tool_name=name, count=count)
                self.memory.add_user(text)
                self.hooks.notice(f"工具 {name} 连续失败 {count} 次，已注入纠偏提示")

        # 重复调用
        for signature, count in repeat_counts.items():
            if count == 2:
                name = signature.split("|", 1)[0]
                text = REPEAT_CALL_NUDGE.format(tool_name=name)
                self.memory.add_user(text)
                self.hooks.notice(f"检测到重复调用 {name}，已注入纠偏提示")

    # ------------------------------------------------------------------ #
    # 其它
    # ------------------------------------------------------------------ #
    def reset(self) -> None:
        """清空对话历史（保留 system prompt 与累计统计）。"""
        self.memory.clear()

    def force_final_answer(self, instruction: str = FORCE_ANSWER_INSTRUCTION) -> str:
        """在不追加用户问题的情况下，让模型基于现有上下文直接收尾。"""
        self.memory.add_user(instruction)
        response = self._invoke_llm(tools=None, allow_stream=False)
        self.total_usage = self.total_usage + response.usage
        answer = (response.content or "").strip()
        self.memory.add_assistant(answer)
        return answer


def _signature(call: ToolCall) -> str:
    """生成"工具名 + 规范化参数"的指纹，用于识别重复调用。"""
    try:
        args = json.dumps(call.arguments, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        args = call.raw_arguments
    return f"{call.name}|{args}"


__all__ = [
    "CodeAgent",
    "AgentHooks",
    "AgentResult",
    "AgentStep",
    "ToolInvocation",
    "ToolOutcome",
    "FAILURE_STREAK_THRESHOLD",
    "REPEAT_BLOCK_THRESHOLD",
]
