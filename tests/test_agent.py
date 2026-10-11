"""Agent 主循环测试：用脚本化假模型精确复现各种棘手场景。

这些用例覆盖了真实使用中最容易出问题的分支：工具报错、模型返回非法参数、
模型陷入重复调用、用完步数上限等。
"""

from __future__ import annotations

import unittest

from tests.helpers import TempWorkspaceTestCase

from codeagent.agent import AgentHooks, CodeAgent
from codeagent.llm import LLMResponse, ToolCall, Usage
from codeagent.mock_llm import ScriptedLLM, text_response, tool_response


def parse_error_response(call_id: str = "bad1", name: str = "read_file") -> LLMResponse:
    """模拟"模型给出无法解析的工具参数"这一常见故障。"""
    return LLMResponse(
        content="我来读一下文件",
        tool_calls=[
            ToolCall(
                id=call_id,
                name=name,
                arguments={},
                raw_arguments="{not valid json",
                parse_error="工具参数不是合法 JSON（Expecting property name）",
            )
        ],
        finish_reason="tool_calls",
    )


class TestBasicLoop(TempWorkspaceTestCase):
    def test_direct_answer_without_tools(self) -> None:
        agent, llm = self.make_agent([text_response("你好，我是 CodeAgent。")])
        result = agent.run("你是谁？")

        self.assertEqual(result.answer, "你好，我是 CodeAgent。")
        self.assertEqual(result.tool_calls, 0)
        self.assertEqual(result.stop_reason, "completed")
        self.assertEqual(len(result.steps), 1)
        # 模型不应该在纯聊天时看到 tools
        self.assertEqual(len(llm.calls), 1)

    def test_empty_input_is_ignored(self) -> None:
        agent, _ = self.make_agent([text_response("x")])
        result = agent.run("   ")
        self.assertEqual(result.stop_reason, "completed")
        self.assertEqual(agent.memory.turn_count, 0)

    def test_tool_then_answer(self) -> None:
        self.write("hello.py", "print('hello')\n")
        agent, llm = self.make_agent(
            [
                tool_response("read_file", {"path": "hello.py"}),
                text_response("这个文件只有一行，打印 hello。"),
            ]
        )
        result = agent.run("hello.py 里是什么？")

        self.assertEqual(result.tool_calls, 1)
        self.assertEqual(result.answer, "这个文件只有一行，打印 hello。")
        self.assertEqual(result.stop_reason, "completed")

        # 协议完整性：assistant(tool_calls) 后面必须紧跟对应的 tool 消息
        roles = [m["role"] for m in agent.memory.raw_messages()]
        self.assertEqual(roles, ["user", "assistant", "tool", "assistant"])

        # 第二次调用时模型应该看到了工具结果
        second_messages = llm.calls[1]["messages"]
        tool_messages = [m for m in second_messages if m["role"] == "tool"]
        self.assertEqual(len(tool_messages), 1)
        self.assertIn("print('hello')", tool_messages[0]["content"])
        self.assertIn('ok="true"', tool_messages[0]["content"])

    def test_multiple_tool_calls_in_one_step(self) -> None:
        self.write("a.py", "AAA\n")
        self.write("b.py", "BBB\n")
        multi = LLMResponse(
            content="同时读取两个文件",
            tool_calls=[
                ToolCall(id="c1", name="read_file", arguments={"path": "a.py"}, raw_arguments='{"path": "a.py"}'),
                ToolCall(id="c2", name="read_file", arguments={"path": "b.py"}, raw_arguments='{"path": "b.py"}'),
            ],
            finish_reason="tool_calls",
        )
        agent, llm = self.make_agent([multi, text_response("两个文件分别是 AAA 和 BBB。")])
        result = agent.run("对比 a.py 和 b.py")

        self.assertEqual(result.steps[0].tool_count, 2)
        tool_messages = [m for m in llm.calls[1]["messages"] if m["role"] == "tool"]
        self.assertEqual(len(tool_messages), 2)
        self.assertEqual({m["tool_call_id"] for m in tool_messages}, {"c1", "c2"})

    def test_usage_accumulates(self) -> None:
        usage = Usage(prompt_tokens=100, completion_tokens=20, total_tokens=120)
        agent, _ = self.make_agent(
            [
                LLMResponse(
                    content="",
                    tool_calls=[ToolCall(id="c", name="list_dir", arguments={"path": "."}, raw_arguments="{}")],
                    finish_reason="tool_calls",
                    usage=usage,
                ),
                LLMResponse(content="好了", usage=usage),
            ]
        )
        result = agent.run("看看目录")
        self.assertEqual(result.usage.prompt_tokens, 200)
        self.assertEqual(agent.total_usage.completion_tokens, 40)


class TestErrorRecovery(TempWorkspaceTestCase):
    def test_tool_failure_is_fed_back(self) -> None:
        agent, llm = self.make_agent(
            [
                tool_response("read_file", {"path": "ghost.py"}),
                text_response("这个文件不存在，我换个方式。"),
            ]
        )
        result = agent.run("读一下 ghost.py")

        self.assertEqual(result.failed_tools, 1)
        tool_message = [m for m in llm.calls[1]["messages"] if m["role"] == "tool"][0]
        self.assertIn('ok="false"', tool_message["content"])
        self.assertIn("不存在", tool_message["content"])
        # 循环没有被中断，仍然给出了最终答案
        self.assertEqual(result.stop_reason, "completed")

    def test_unknown_tool_is_reported(self) -> None:
        agent, llm = self.make_agent(
            [tool_response("delete_everything", {"path": "/"}), text_response("抱歉，没有这个工具。")]
        )
        result = agent.run("删库")

        self.assertEqual(result.failed_tools, 1)
        tool_message = [m for m in llm.calls[1]["messages"] if m["role"] == "tool"][0]
        self.assertIn("不存在名为", tool_message["content"])
        self.assertEqual(result.answer, "抱歉，没有这个工具。")

    def test_parse_error_still_produces_tool_message(self) -> None:
        """关键协议约束：即使参数解析失败，也必须回一条 tool 消息占位。"""
        agent, llm = self.make_agent([parse_error_response(), text_response("我重新读一次。")])
        result = agent.run("读文件")

        self.assertEqual(result.steps[0].tool_count, 1)
        messages = llm.calls[1]["messages"]
        tool_messages = [m for m in messages if m["role"] == "tool"]
        self.assertEqual(len(tool_messages), 1)
        self.assertEqual(tool_messages[0]["tool_call_id"], "bad1")
        self.assertIn("JSON", tool_messages[0]["content"])

    def test_workspace_violation_fed_back(self) -> None:
        agent, llm = self.make_agent(
            [
                tool_response("write_file", {"path": "../escape.txt", "content": "x"}),
                text_response("我不能写到工作目录外面。"),
            ]
        )
        result = agent.run("把内容写到上级目录")
        self.assertEqual(result.failed_tools, 1)
        tool_message = [m for m in llm.calls[1]["messages"] if m["role"] == "tool"][0]
        self.assertIn("工作目录", tool_message["content"])

    def test_read_only_mode_blocks_writes(self) -> None:
        agent, llm = self.make_agent(
            [
                tool_response("write_file", {"path": "new.py", "content": "x"}),
                text_response("当前是只读模式。"),
            ],
            allow_write=False,
        )
        result = agent.run("新建一个文件")

        self.assertEqual(result.failed_tools, 1)
        tool_message = [m for m in llm.calls[1]["messages"] if m["role"] == "tool"][0]
        self.assertIn("--read-only", tool_message["content"])


class TestGuards(TempWorkspaceTestCase):
    def test_duplicate_call_is_blocked_after_threshold(self) -> None:
        responses = [tool_response("list_dir", {"path": "."}) for _ in range(3)]
        responses.append(text_response("目录里没什么东西。"))
        agent, llm = self.make_agent(responses, max_steps=8)
        result = agent.run("看看目录")

        outcomes = [o for step in result.steps for o in step.outcomes]
        self.assertEqual(len(outcomes), 3)
        self.assertFalse(outcomes[0].blocked)
        self.assertFalse(outcomes[1].blocked)
        self.assertTrue(outcomes[2].blocked, "第 3 次相同调用应被拦截")
        self.assertIn("已阻止", outcomes[2].result.content)

        # 第 2 次之后应注入过纠偏提示
        texts = " ".join(
            str(m.get("content")) for m in agent.memory.raw_messages() if m["role"] == "user"
        )
        self.assertIn("重复调用", texts)

    def test_repeated_failures_trigger_nudge(self) -> None:
        responses = [tool_response("read_file", {"path": f"ghost{i}.py"}) for i in range(3)]
        responses.append(text_response("这几个文件都不存在。"))
        agent, llm = self.make_agent(responses, max_steps=8)
        result = agent.run("读取几个不存在的文件")

        self.assertEqual(result.failed_tools, 3)
        texts = " ".join(
            str(m.get("content")) for m in agent.memory.raw_messages() if m["role"] == "user"
        )
        self.assertIn("连续失败", texts)

    def test_step_limit_forces_final_answer(self) -> None:
        responses = [
            tool_response("list_dir", {"path": "."}),
            text_response("基于已有信息，我给出结论。"),
        ]
        agent, llm = self.make_agent(responses, max_steps=1)
        result = agent.run("看看目录")

        self.assertEqual(result.stop_reason, "step_limit")
        self.assertEqual(result.answer, "基于已有信息，我给出结论。")
        self.assertEqual(len(result.steps), 1)

        # 收尾调用必须不带工具，逼模型输出文本
        self.assertEqual(llm.calls[-1]["tools"], [])
        self.assertEqual(llm.calls[-1]["tool_choice"], "none")
        # 并且注入了步数上限提示
        last_messages = llm.calls[-1]["messages"]
        self.assertIn("步数上限", str(last_messages[-1]["content"]))

    def test_step_limit_survives_final_call_failure(self) -> None:
        class BoomLLM(ScriptedLLM):
            def chat(self, messages, tools=None, **kwargs):  # type: ignore[override]
                if not tools:
                    from codeagent.errors import LLMHTTPError

                    raise LLMHTTPError("HTTP 500", status_code=500, retryable=False)
                return super().chat(messages, tools, **kwargs)

        settings = self.make_settings(max_steps=1)
        context = self.make_context()
        from codeagent.tools import build_default_registry

        agent = CodeAgent(
            BoomLLM([tool_response("list_dir", {"path": "."})]),
            build_default_registry(context),
            settings,
            tool_context=context,
        )
        result = agent.run("看看目录")
        self.assertEqual(result.stop_reason, "step_limit")
        self.assertIn("调用模型失败", result.answer)


class TestHooks(TempWorkspaceTestCase):
    def test_all_hooks_fire(self) -> None:
        events: dict[str, list] = {
            "step": [], "text": [], "tool_start": [], "tool_end": [], "notice": [], "final": []
        }
        hooks = AgentHooks(
            on_step_start=lambda s, t: events["step"].append((s, t)),
            on_assistant_text=lambda d: events["text"].append(d),
            on_tool_start=lambda inv: events["tool_start"].append(inv.name),
            on_tool_end=lambda inv, res, dur: events["tool_end"].append((inv.name, res.ok)),
            on_notice=lambda n: events["notice"].append(n),
            on_final=lambda f: events["final"].append(f),
        )
        settings = self.make_settings(max_steps=1)
        context = self.make_context()
        from codeagent.tools import build_default_registry

        llm = ScriptedLLM(
            [tool_response("list_dir", {"path": "."}), text_response("最终答案")]
        )
        agent = CodeAgent(
            llm, build_default_registry(context), settings, hooks=hooks, tool_context=context
        )
        agent.run("看看目录")

        self.assertEqual(events["step"], [(1, 1)])
        self.assertEqual(events["tool_start"], ["list_dir"])
        self.assertEqual(events["tool_end"], [("list_dir", True)])
        self.assertEqual(events["final"], ["最终答案"])
        self.assertTrue(any("步数上限" in n for n in events["notice"]))

    def test_broken_hook_does_not_break_loop(self) -> None:
        def explode(*_args: object) -> None:
            raise RuntimeError("钩子炸了")

        hooks = AgentHooks(on_step_start=explode, on_tool_start=explode, on_final=explode)
        settings = self.make_settings()
        context = self.make_context()
        from codeagent.tools import build_default_registry

        agent = CodeAgent(
            ScriptedLLM([text_response("一切正常")]),
            build_default_registry(context),
            settings,
            hooks=hooks,
            tool_context=context,
        )
        result = agent.run("你好")
        self.assertEqual(result.answer, "一切正常")


class TestPromptAndMemory(TempWorkspaceTestCase):
    def test_system_prompt_contains_environment_and_tools(self) -> None:
        agent, _ = self.make_agent([text_response("x")])
        prompt = agent.memory.system_prompt

        self.assertIn(str(self.workspace), prompt)
        self.assertIn("list_dir(", prompt)
        self.assertIn("run_python(", prompt)
        self.assertIn("先看再改", prompt)

    def test_multi_turn_context_is_kept(self) -> None:
        agent, llm = self.make_agent(
            [text_response("第一次回答"), text_response("第二次回答")]
        )
        agent.run("第一个问题")
        agent.run("第二个问题")

        messages = llm.calls[1]["messages"]
        contents = [str(m.get("content")) for m in messages]
        self.assertIn("第一个问题", contents)
        self.assertIn("第一次回答", contents)
        self.assertEqual(agent.memory.turn_count, 2)

    def test_reset_clears_history_but_keeps_stats(self) -> None:
        agent, _ = self.make_agent([text_response("回答")])
        agent.run("问题")
        self.assertEqual(agent.memory.turn_count, 1)
        agent.reset()
        self.assertEqual(agent.memory.turn_count, 0)

    def test_force_final_answer(self) -> None:
        agent, llm = self.make_agent(
            [
                tool_response("list_dir", {"path": "."}),
                text_response("收尾结论"),
            ],
            max_steps=5,
        )
        agent.run("看看目录")
        agent.force_final_answer()
        self.assertEqual(llm.calls[-1]["tool_choice"], "none")
        self.assertEqual(agent.memory.raw_messages()[-1]["content"], "(脚本已耗尽)")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
