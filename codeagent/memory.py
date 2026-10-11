"""上下文记忆管理。

LLM 是无状态的，所谓"记忆"就是把历史消息原样带上。但上下文窗口有限，
对话一长就必须裁剪。粗暴地"丢掉最旧的一半"会造成两类严重后果：

1. **孤儿 tool 消息**：``assistant(tool_calls)`` 被丢掉，但它的 ``role=tool``
   结果还在 —— 多数服务端会直接返回 400 错误。
2. **半个工具回合**：一次 assistant 并发调了 3 个工具，只保留了其中 2 个结果。

因此本模块的做法是：先把消息切成**原子块**（一个 assistant 的工具调用 +
它引发的全部 tool 结果算一块，不可拆分），再从最新往旧保留，直到用完预算。
被丢弃的历史会以一条"摘要占位"消息的形式留下痕迹，让模型知道自己"失忆"了。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

Message = dict[str, Any]

#: 裁剪后注入的占位提示。
OMITTED_NOTICE = (
    "【上下文提示】为了控制长度，本次对话中较早的 {count} 条消息已被省略"
    "（包含此前的部分工具调用记录）。如果需要这些信息，请重新用工具查询。"
)


def estimate_tokens(text: str) -> int:
    """粗略估算 token 数。

    经验公式：ASCII 字符约 4 个 1 token；CJK 字符约 1 个 1 token 甚至更多。
    这里对非 ASCII 字符按 1.0、ASCII 按 0.25 计，再乘一个安全系数，
    宁可高估也不要低估（低估会导致真正的超限报错）。
    """
    if not text:
        return 0
    ascii_count = sum(1 for ch in text if ord(ch) < 128)
    wide_count = len(text) - ascii_count
    return int((ascii_count * 0.25 + wide_count * 1.0) * 1.15) + 4


def estimate_message_tokens(message: Message) -> int:
    """估算一条消息的 token 数（含 role 与工具调用开销）。"""
    total = 8  # role / 分隔符等固定开销
    content = message.get("content")
    if isinstance(content, str):
        total += estimate_tokens(content)
    elif isinstance(content, list):
        total += estimate_tokens(str(content))
    for call in message.get("tool_calls") or ():
        function = (call or {}).get("function") or {}
        total += estimate_tokens(str(function.get("name", "")))
        total += estimate_tokens(str(function.get("arguments", "")))
        total += 12
    if message.get("name"):
        total += estimate_tokens(str(message["name"]))
    return total


def estimate_messages_tokens(messages: Sequence[Message]) -> int:
    return sum(estimate_message_tokens(m) for m in messages)


@dataclass
class MemoryStats:
    """供 CLI ``/status`` 展示的统计信息。"""

    total_messages: int = 0
    kept_messages: int = 0
    dropped_messages: int = 0
    estimated_tokens: int = 0
    token_budget: int = 0
    turns: int = 0

    @property
    def usage_ratio(self) -> float:
        if self.token_budget <= 0:
            return 0.0
        return min(1.0, self.estimated_tokens / self.token_budget)


class ConversationMemory:
    """带 token 预算的对话历史。"""

    def __init__(
        self,
        system_prompt: str,
        token_budget: int = 60_000,
        *,
        reserve_for_reply: int = 2_000,
    ) -> None:
        self._system_prompt = system_prompt
        self.token_budget = max(1_000, int(token_budget))
        self.reserve_for_reply = max(0, int(reserve_for_reply))
        self._messages: list[Message] = []
        self._dropped = 0
        self._omitted_notice_count = 0

    # ------------------------------------------------------------------ #
    # 写入
    # ------------------------------------------------------------------ #
    def add_user(self, content: str) -> Message:
        return self._append({"role": "user", "content": content})

    def add_assistant(self, content: str, tool_calls: list[dict[str, Any]] | None = None) -> Message:
        message: Message = {"role": "assistant", "content": content or ""}
        if tool_calls:
            message["tool_calls"] = tool_calls
        return self._append(message)

    def add_tool_result(self, tool_call_id: str, name: str, content: str) -> Message:
        return self._append(
            {
                "role": "tool",
                "tool_call_id": tool_call_id,
                "name": name,
                "content": content,
            }
        )

    def add_system_note(self, content: str) -> Message:
        """插入一条运行时的系统提示（纠偏、步数上限等）。"""
        return self._append({"role": "system", "content": content})

    def _append(self, message: Message) -> Message:
        self._messages.append(message)
        return message

    # ------------------------------------------------------------------ #
    # 读取
    # ------------------------------------------------------------------ #
    @property
    def system_prompt(self) -> str:
        return self._system_prompt

    def set_system_prompt(self, prompt: str) -> None:
        self._system_prompt = prompt

    def raw_messages(self) -> list[Message]:
        """未经裁剪的完整历史（用于持久化或调试）。"""
        return list(self._messages)

    def messages(self) -> list[Message]:
        """返回可直接发给 LLM 的消息列表（system + 裁剪后的历史）。"""
        budget = self.token_budget - self.reserve_for_reply
        system_message: Message = {"role": "system", "content": self._system_prompt}
        system_cost = estimate_message_tokens(system_message)

        blocks = self._atomic_blocks()
        kept: list[list[Message]] = []
        used = system_cost

        for block in reversed(blocks):
            cost = sum(estimate_message_tokens(m) for m in block)
            if kept and used + cost > budget:
                break
            kept.append(block)
            used += cost

        kept.reverse()
        flat = [m for block in kept for m in block]
        dropped = len(self._messages) - len(flat)

        result: list[Message] = [system_message]
        if dropped > 0:
            result.append(
                {
                    "role": "system",
                    "content": OMITTED_NOTICE.format(count=dropped),
                }
            )
        result.extend(flat)
        self._dropped = dropped
        return result

    def _atomic_blocks(self) -> list[list[Message]]:
        """把历史切分成不可拆分的原子块。

        规则：``assistant`` 若带 ``tool_calls``，则它与紧随其后、``tool_call_id``
        属于它的所有 ``tool`` 消息合成一块。这样无论从哪里截断，
        都不会产生"孤儿 tool 消息"或"半个工具回合"。
        """
        blocks: list[list[Message]] = []
        pending_ids: set[str] = set()
        current: list[Message] = []

        for message in self._messages:
            role = message.get("role")
            if role == "assistant" and message.get("tool_calls"):
                if current:
                    blocks.append(current)
                current = [message]
                pending_ids = {
                    str(call.get("id")) for call in message.get("tool_calls") or []
                }
                continue

            if role == "tool":
                call_id = str(message.get("tool_call_id") or "")
                if current and (not pending_ids or call_id in pending_ids):
                    current.append(message)
                    pending_ids.discard(call_id)
                    continue
                # 找不到归属的 tool 消息：单独成块（正常情况下不该出现）
                if current:
                    blocks.append(current)
                    current = []
                blocks.append([message])
                continue

            if current:
                blocks.append(current)
                current = []
            current = [message]

        if current:
            blocks.append(current)
        return blocks

    # ------------------------------------------------------------------ #
    # 维护
    # ------------------------------------------------------------------ #
    def clear(self, *, keep_system: bool = True) -> None:
        self._messages.clear()
        self._dropped = 0

    def drop_last(self, count: int = 1) -> None:
        """移除最后 ``count`` 条消息（用于失败时回滚半截的回合）。"""
        for _ in range(min(count, len(self._messages))):
            self._messages.pop()

    def rollback_incomplete_turn(self) -> int:
        """回滚最后一个"未完成"的用户回合。

        用户按 Ctrl+C 中断时，历史里可能留下"用户提问了、但助手还没回答"的
        半截记录。若不清理，下一轮模型会看到两条连续的 user 消息而困惑。
        本方法会一直删到最近的 ``user`` 消息并把它一并删除。

        Returns:
            被删除的消息条数。
        """
        removed = 0
        while self._messages:
            message = self._messages.pop()
            removed += 1
            if message.get("role") == "user":
                break
        return removed

    def __len__(self) -> int:
        return len(self._messages)

    @property
    def turn_count(self) -> int:
        return sum(1 for m in self._messages if m.get("role") == "user")

    def stats(self) -> MemoryStats:
        final = self.messages()
        return MemoryStats(
            total_messages=len(self._messages),
            kept_messages=len(final),
            dropped_messages=self._dropped,
            estimated_tokens=estimate_messages_tokens(final),
            token_budget=self.token_budget,
            turns=self.turn_count,
        )

    # ------------------------------------------------------------------ #
    def transcript(self, limit: int | None = None) -> str:
        """生成人类可读的对话记录（``/history`` 命令用）。"""
        lines: list[str] = []
        for message in self._messages[-limit:] if limit else self._messages:
            role = message.get("role", "?")
            content = message.get("content")
            text = content if isinstance(content, str) else str(content)
            if message.get("tool_calls"):
                names = [
                    (call.get("function") or {}).get("name", "?")
                    for call in message["tool_calls"]
                ]
                text = (text + f"  [调用工具: {', '.join(names)}]").strip()
            if len(text) > 400:
                text = text[:400] + " …"
            lines.append(f"[{role}] {text}")
        return "\n".join(lines) if lines else "(暂无历史)"


__all__ = [
    "ConversationMemory",
    "MemoryStats",
    "estimate_tokens",
    "estimate_message_tokens",
    "estimate_messages_tokens",
    "OMITTED_NOTICE",
]
