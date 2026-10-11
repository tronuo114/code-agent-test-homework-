"""上下文记忆测试：原子分组裁剪是本模块最容易出错、也最值得测的部分。"""

from __future__ import annotations

import unittest

from tests.helpers import TempWorkspaceTestCase

from codeagent.memory import (
    ConversationMemory,
    estimate_messages_tokens,
    estimate_tokens,
)


def assert_well_formed(test: unittest.TestCase, messages: list[dict]) -> None:
    """校验消息序列满足 OpenAI 协议的完整性约束。

    1. 每条 ``tool`` 消息都必须能找到对应的 ``assistant.tool_calls``；
    2. 每个 ``assistant.tool_calls`` 里的调用都必须有对应的 ``tool`` 响应。
    """
    pending: set[str] = set()
    seen_files: list[str] = []
    for message in messages:
        role = message.get("role")
        if role == "assistant" and message.get("tool_calls"):
            for call in message["tool_calls"]:
                pending.add(call["id"])
        elif role == "tool":
            call_id = message.get("tool_call_id")
            test.assertIn(
                call_id,
                pending,
                f"出现孤儿 tool 消息：{call_id}（其 assistant 消息已被裁掉）",
            )
            pending.discard(call_id)
        seen_files.append(str(role))
    test.assertEqual(pending, set(), f"存在没有工具响应的 tool_calls：{pending}\n消息序列：{seen_files}")


class TestEstimate(unittest.TestCase):
    def test_empty(self) -> None:
        self.assertEqual(estimate_tokens(""), 0)

    def test_cjk_costs_more_than_ascii(self) -> None:
        self.assertGreater(estimate_tokens("中文字符"), estimate_tokens("abcdefgh"))

    def test_monotonic(self) -> None:
        self.assertLess(estimate_tokens("abc"), estimate_tokens("abcdefghij"))

    def test_message_overhead(self) -> None:
        plain = estimate_messages_tokens([{"role": "user", "content": "hi"}])
        self.assertGreater(plain, 0)


class TestBasicBehaviour(unittest.TestCase):
    def setUp(self) -> None:
        self.memory = ConversationMemory("你是助手", token_budget=10_000)

    def test_system_prompt_is_first(self) -> None:
        self.memory.add_user("你好")
        messages = self.memory.messages()
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[0]["content"], "你是助手")

    def test_sequence(self) -> None:
        self.memory.add_user("看看 a.py")
        self.memory.add_assistant("好的", [{"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}])
        self.memory.add_tool_result("c1", "read_file", "<tool_result/>")
        self.memory.add_assistant("这是内容")
        roles = [m["role"] for m in self.memory.messages()]
        self.assertEqual(roles, ["system", "user", "assistant", "tool", "assistant"])

    def test_clear_keeps_system(self) -> None:
        self.memory.add_user("x")
        self.memory.clear()
        messages = self.memory.messages()
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["role"], "system")

    def test_turn_count(self) -> None:
        self.memory.add_user("a")
        self.memory.add_user("b")
        self.assertEqual(self.memory.turn_count, 2)

    def test_rollback_incomplete_turn(self) -> None:
        self.memory.add_user("第一轮")
        self.memory.add_assistant("回答")
        self.memory.add_user("第二轮（被打断）")
        self.memory.add_assistant("", [{"id": "c1", "type": "function", "function": {"name": "t", "arguments": "{}"}}])
        self.memory.add_tool_result("c1", "t", "结果")

        removed = self.memory.rollback_incomplete_turn()
        self.assertEqual(removed, 3)
        roles = [m["role"] for m in self.memory.messages()]
        self.assertEqual(roles, ["system", "user", "assistant"])

    def test_rollback_empty_is_safe(self) -> None:
        self.assertEqual(self.memory.rollback_incomplete_turn(), 0)


class TestTrimming(TempWorkspaceTestCase):
    def _fill(self, memory: ConversationMemory, rounds: int) -> None:
        for index in range(rounds):
            memory.add_user(f"第 {index} 轮问题 " + "填充内容" * 20)
            memory.add_assistant(
                "我来看一下",
                [
                    {
                        "id": f"c{index}",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": '{"path": "a.py"}'},
                    }
                ],
            )
            memory.add_tool_result(f"c{index}", "read_file", "文件内容 " * 60)

    def test_keeps_protocol_valid(self) -> None:
        memory = ConversationMemory("SYS", token_budget=2_500, reserve_for_reply=0)
        self._fill(memory, 25)
        messages = memory.messages()
        assert_well_formed(self, messages)

    def test_drops_oldest_first(self) -> None:
        memory = ConversationMemory("SYS", token_budget=2_500, reserve_for_reply=0)
        self._fill(memory, 25)
        messages = memory.messages()
        text = "\n".join(str(m.get("content")) for m in messages)
        self.assertIn("第 24 轮问题", text, "最新的消息必须保留")
        self.assertNotIn("第 0 轮问题", text, "最旧的消息应被裁掉")

    def test_omission_notice_inserted(self) -> None:
        memory = ConversationMemory("SYS", token_budget=1_200, reserve_for_reply=0)
        self._fill(memory, 25)
        messages = memory.messages()
        self.assertEqual(messages[1]["role"], "system")
        self.assertIn("已被省略", messages[1]["content"])

    def test_no_notice_when_nothing_dropped(self) -> None:
        memory = ConversationMemory("SYS", token_budget=100_000, reserve_for_reply=0)
        self._fill(memory, 3)
        messages = memory.messages()
        self.assertEqual(len(messages), 1 + 3 * 3)

    def test_concurrent_tool_calls_are_atomic(self) -> None:
        """一次 assistant 并发调用两个工具，两个结果必须同进同退。"""
        memory = ConversationMemory("SYS", token_budget=400, reserve_for_reply=0)
        memory.add_user("开始")
        memory.add_assistant(
            "",
            [
                {"id": "a1", "type": "function", "function": {"name": "f1", "arguments": "{}"}},
                {"id": "a2", "type": "function", "function": {"name": "f2", "arguments": "{}"}},
            ],
        )
        memory.add_tool_result("a1", "f1", "很长很长的结果一 " * 40)
        memory.add_tool_result("a2", "f2", "很长很长的结果二 " * 40)
        memory.add_user("继续")

        messages = memory.messages()
        assert_well_formed(self, messages)

    def test_stats(self) -> None:
        memory = ConversationMemory("SYS", token_budget=2_000, reserve_for_reply=0)
        self._fill(memory, 20)
        stats = memory.stats()
        self.assertEqual(stats.total_messages, 60)
        self.assertLess(stats.kept_messages, stats.total_messages)
        self.assertGreater(stats.dropped_messages, 0)
        self.assertTrue(0.0 <= stats.usage_ratio <= 1.0)

    def test_transcript(self) -> None:
        memory = ConversationMemory("SYS", token_budget=5_000)
        memory.add_user("你好")
        memory.add_assistant("", [{"id": "c", "type": "function", "function": {"name": "list_dir", "arguments": "{}"}}])
        text = memory.transcript()
        self.assertIn("[user] 你好", text)
        self.assertIn("list_dir", text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
