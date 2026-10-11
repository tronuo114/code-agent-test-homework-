"""代码审查模式测试：绑定存储、密钥校验、专用 Prompt、命令循环。

覆盖的是这次改造新加的全部逻辑。原有的内核测试在
``test_llm`` / ``test_memory`` / ``test_tools`` / ``test_agent`` 里，继续保留。
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import TempWorkspaceTestCase
from tests.test_llm import FakeTransport, completion

from codeagent.errors import LLMConnectionError, LLMHTTPError, LLMTimeoutError
from codeagent.review_cli import (
    HELP_TEXT,
    MarkdownLineFormatter,
    ReviewSession,
    SessionOptions,
    _relative_to,
    _summarize_tool_result,
    main as review_main,
)
from codeagent.review_prompt import (
    REVIEW_SYSTEM_PROMPT,
    build_review_instruction,
    build_review_task,
)
from codeagent.store import Binding, ConfigStore, app_dir
from codeagent.tools import ToolContext, build_review_registry
from codeagent.tools.base import ToolResult
from codeagent.ui import Palette, display_width, pad
from codeagent.verify import verify_binding


def quiet(callable_obj, *args, **kwargs):
    """执行并吞掉标准输出，返回 (返回值, 捕获到的文本)。"""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        result = callable_obj(*args, **kwargs)
    return result, buffer.getvalue()


@contextlib.contextmanager
def color_allowed():
    """临时移除 NO_COLOR。

    很多执行环境（CI、被捕获输出的终端）会预设 ``NO_COLOR=1``，
    而 :class:`Palette` 是**故意**尊重它的。想测"着色确实生效"，
    就得先把这个变量摘掉；顺手也能测出"尊重 NO_COLOR"本身是否有效。
    """
    saved = os.environ.pop("NO_COLOR", None)
    try:
        yield
    finally:
        if saved is not None:
            os.environ["NO_COLOR"] = saved


# --------------------------------------------------------------------------- #
# 绑定结构与持久化
# --------------------------------------------------------------------------- #
class TestBinding(unittest.TestCase):
    def test_masks_key(self) -> None:
        binding = Binding(api_key="sk-1234567890abcdef")
        masked = binding.masked_key()
        self.assertNotIn("1234567890", masked)
        self.assertTrue(masked.startswith("sk-123"))
        self.assertIn("*", masked)

    def test_empty_key(self) -> None:
        self.assertEqual(Binding().masked_key(), "<未绑定>")
        self.assertFalse(Binding().bound)

    def test_short_key_fully_masked(self) -> None:
        self.assertEqual(Binding(api_key="abc").masked_key(), "***")

    def test_endpoint_join(self) -> None:
        self.assertEqual(
            Binding(base_url="https://api.deepseek.com/v1").chat_endpoint(),
            "https://api.deepseek.com/v1/chat/completions",
        )
        self.assertEqual(
            Binding(base_url="https://api.deepseek.com/v1/").chat_endpoint(),
            "https://api.deepseek.com/v1/chat/completions",
        )
        self.assertEqual(
            Binding(base_url="https://x.com/v1/chat/completions").chat_endpoint(),
            "https://x.com/v1/chat/completions",
        )

    def test_json_roundtrip(self) -> None:
        original = Binding(api_key="sk-x", model="qwen-plus", max_steps=7, verified=True)
        restored = Binding.from_json(json.loads(json.dumps(original.to_json())))
        self.assertEqual(restored.api_key, "sk-x")
        self.assertEqual(restored.model, "qwen-plus")
        self.assertEqual(restored.max_steps, 7)
        self.assertTrue(restored.verified)

    def test_from_json_ignores_unknown_keys(self) -> None:
        binding = Binding.from_json({"api_key": "k", "_comment": "x", "bogus": 1})
        self.assertEqual(binding.api_key, "k")

    def test_from_json_rejects_non_object(self) -> None:
        with self.assertRaises(ValueError):
            Binding.from_json(["not", "an", "object"])


class TestConfigStore(TempWorkspaceTestCase):
    def test_save_then_load(self) -> None:
        path = self.workspace / "config.json"
        store = ConfigStore(path)
        ok, written, error = store.save(Binding(api_key="sk-test", model="m1"))
        self.assertTrue(ok, error)
        self.assertEqual(written, path)

        restored = store.load()
        self.assertEqual(restored.api_key, "sk-test")
        self.assertEqual(restored.model, "m1")

    def test_missing_file_returns_empty_binding(self) -> None:
        store = ConfigStore(self.workspace / "nope.json")
        self.assertFalse(store.load().bound)

    def test_corrupt_file_is_survivable(self) -> None:
        path = self.workspace / "config.json"
        path.write_text("{ this is not json", encoding="utf-8")
        store = ConfigStore(path)
        binding, output = quiet(store.load)
        self.assertFalse(binding.bound)
        self.assertIn("无法解析", output)

    def test_clear_removes_file(self) -> None:
        path = self.workspace / "config.json"
        store = ConfigStore(path)
        store.save(Binding(api_key="sk-x"))
        self.assertTrue(path.is_file())
        self.assertTrue(store.clear())
        self.assertFalse(path.exists())
        self.assertTrue(store.clear())  # 幂等

    def test_save_reports_failure_when_no_candidate_writable(self) -> None:
        """两个候选位置都写不进去时应返回失败，而不是抛异常。"""
        # 用一个"父路径是文件"的路径，保证 mkdir 必然失败
        blocker = self.workspace / "blocker"
        blocker.write_text("x", encoding="utf-8")
        store = ConfigStore(blocker / "config.json")
        ok, path, error = store.save(Binding(api_key="sk-x"))
        self.assertFalse(ok)
        self.assertTrue(error)

    def test_written_file_is_valid_json_and_warns_about_plaintext(self) -> None:
        path = self.workspace / "config.json"
        ConfigStore(path).save(Binding(api_key="sk-secret"))
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["api_key"], "sk-secret")
        self.assertIn("明文", data["_comment"])


# --------------------------------------------------------------------------- #
# 密钥校验
# --------------------------------------------------------------------------- #
class TestVerifyInputValidation(unittest.TestCase):
    def test_empty_key(self) -> None:
        self.assertEqual(verify_binding("", "https://x/v1", "m").level, "fail")
        self.assertEqual(verify_binding("   ", "https://x/v1", "m").level, "fail")

    def test_bad_base_url(self) -> None:
        result = verify_binding("sk-x", "api.deepseek.com", "m")
        self.assertEqual(result.level, "fail")
        self.assertIn("端点格式", result.message)

    def test_empty_model(self) -> None:
        self.assertEqual(verify_binding("sk-x", "https://x/v1", "  ").level, "fail")


class TestVerifyClassification(unittest.TestCase):
    """用假传输层离线覆盖每一种 HTTP 结果该怎么归类。"""

    def _run(self, script_item) -> object:
        transport = FakeTransport([script_item])
        return verify_binding(
            "sk-test", "https://example.invalid/v1", "test-model", transport=transport
        )

    def test_success_is_ok(self) -> None:
        result = self._run(completion("ok"))
        self.assertEqual(result.level, "ok")
        self.assertTrue(result.ok)
        self.assertTrue(result.verified)
        self.assertIn("校验通过", result.message)

    def test_401_is_fail(self) -> None:
        result = self._run(LLMHTTPError("HTTP 401", status_code=401, body="bad key"))
        self.assertEqual(result.level, "fail")
        self.assertFalse(result.ok)
        self.assertIn("无效", result.message)

    def test_402_is_fail(self) -> None:
        result = self._run(LLMHTTPError("HTTP 402", status_code=402))
        self.assertIn("余额", result.message)

    def test_403_is_fail(self) -> None:
        result = self._run(LLMHTTPError("HTTP 403", status_code=403))
        self.assertIn("权限", result.message)

    def test_404_is_fail(self) -> None:
        result = self._run(LLMHTTPError("HTTP 404", status_code=404))
        self.assertIn("/v1", result.message)

    def test_400_is_fail(self) -> None:
        self.assertEqual(
            self._run(LLMHTTPError("HTTP 400", status_code=400)).level, "fail"
        )

    def test_429_is_warn_not_fail(self) -> None:
        """限流不代表密钥有错，不能判用户输错了。"""
        result = self._run(LLMHTTPError("HTTP 429", status_code=429))
        self.assertEqual(result.level, "warn")
        self.assertTrue(result.ok)
        self.assertFalse(result.verified)

    def test_500_is_warn(self) -> None:
        result = self._run(LLMHTTPError("HTTP 500", status_code=500))
        self.assertEqual(result.level, "warn")

    def test_network_error_is_warn_not_fail(self) -> None:
        result = self._run(LLMConnectionError("连不上"))
        self.assertEqual(result.level, "warn")
        self.assertIn("无法完成校验", result.message)

    def test_timeout_is_warn(self) -> None:
        result = self._run(LLMTimeoutError("超时"))
        self.assertEqual(result.level, "warn")

    def test_unexpected_exception_is_absorbed(self) -> None:
        """校验本身出 bug 也不能把程序打崩，退化成 warn。"""
        result = self._run(RuntimeError("boom"))
        self.assertEqual(result.level, "warn")
        self.assertIn("未预期", result.message)


# --------------------------------------------------------------------------- #
# 审查 Prompt
# --------------------------------------------------------------------------- #
class TestReviewPrompt(unittest.TestCase):
    def test_system_prompt_placeholders(self) -> None:
        rendered = REVIEW_SYSTEM_PROMPT.format(
            os_name="Windows 11",
            workspace=r"D:\proj",
            write_mode="只读",
            tool_catalog="  - read_file(path)",
        )
        self.assertIn("D:\\proj", rendered)
        self.assertIn("read_file(path)", rendered)
        self.assertIn("只读", rendered)

    def test_prompt_demands_evidence_and_line_numbers(self) -> None:
        self.assertIn("证据来自工具", REVIEW_SYSTEM_PROMPT)
        self.assertIn("文件:行号", REVIEW_SYSTEM_PROMPT)
        # 必须明确禁止编造问题
        self.assertIn("编造", REVIEW_SYSTEM_PROMPT)

    def test_task_template_mentions_target(self) -> None:
        task = build_review_task("src/app.py")
        self.assertIn("src/app.py", task)
        self.assertIn("文件:行号", task)

    def test_instruction_template_mentions_both(self) -> None:
        task = build_review_instruction("检查空值处理", "/work")
        self.assertIn("检查空值处理", task)
        self.assertIn("/work", task)


# --------------------------------------------------------------------------- #
# 只读工具集
# --------------------------------------------------------------------------- #
class TestReviewRegistry(TempWorkspaceTestCase):
    def test_only_read_only_tools_are_registered(self) -> None:
        registry = build_review_registry(self.make_context())
        self.assertEqual(
            sorted(registry.names()), ["list_dir", "read_file", "search_code"]
        )
        # 写类与执行类工具必须完全不存在
        for forbidden in ("write_file", "edit_file", "run_command", "run_python"):
            self.assertNotIn(forbidden, registry)

    def test_write_tool_is_not_reachable_at_all(self) -> None:
        """写类工具在审查模式里应当**完全不可达**，而不是"调用了才被拒绝"。"""
        registry = build_review_registry(self.make_context(allow_write=False))
        result = registry.execute("write_file", {"path": "x.py", "content": "x"})
        self.assertFalse(result.ok)
        self.assertEqual(result.error_kind, "tool.not_found")

    def test_read_file_works(self) -> None:
        self.write("a.py", "def f():\n    return 1\n")
        registry = build_review_registry(self.make_context())
        result = registry.execute("read_file", {"path": "a.py"})
        self.assertTrue(result.ok)
        self.assertIn("def f", result.content)


# --------------------------------------------------------------------------- #
# 终端渲染辅助
# --------------------------------------------------------------------------- #
class TestDisplayWidth(unittest.TestCase):
    def test_ascii_is_one_column(self) -> None:
        self.assertEqual(display_width("abc"), 3)

    def test_cjk_is_two_columns(self) -> None:
        self.assertEqual(display_width("端点"), 4)

    def test_pad_aligns_mixed_width(self) -> None:
        self.assertEqual(display_width(pad("端点", 12)), 12)
        self.assertEqual(display_width(pad("API Key", 12)), 12)


class TestMarkdownLineFormatter(unittest.TestCase):
    def setUp(self) -> None:
        self.formatter = MarkdownLineFormatter(Palette(enabled=False))

    def test_holds_partial_line(self) -> None:
        self.assertEqual(self.formatter.feed("hello"), "")
        self.assertEqual(self.formatter.flush(), "hello")

    def test_emits_complete_lines(self) -> None:
        self.assertEqual(self.formatter.feed("a\nb\n"), "a\nb\n")
        self.assertEqual(self.formatter.flush(), "")

    def test_strips_heading_markers(self) -> None:
        out = self.formatter.feed("## 审查结论\n")
        self.assertEqual(out, "审查结论\n")
        self.assertNotIn("#", out)

    def test_strips_bold_markers(self) -> None:
        out = self.formatter.feed("**1. 标题**\n")
        self.assertEqual(out, "1. 标题\n")

    def test_colored_output_contains_ansi(self) -> None:
        with color_allowed():
            formatter = MarkdownLineFormatter(Palette(enabled=True))
            out = formatter.feed("## 审查结论\n")
        self.assertIn("\033[", out)

    def test_no_color_env_is_respected(self) -> None:
        """NO_COLOR 是业界通用约定，必须遵守。"""
        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}):
            self.assertFalse(Palette(enabled=True).enabled)
            self.assertEqual(Palette(enabled=True)("hello", "red"), "hello")

    def test_disabled_palette_is_plain_text(self) -> None:
        self.assertEqual(Palette(enabled=False)("hello", "red", "bold"), "hello")

    def test_severity_colour_mapping(self) -> None:
        with color_allowed():
            formatter = MarkdownLineFormatter(Palette(enabled=True))
            self.assertIn("\033[31m", formatter.format("### 🔴 严重"))
            self.assertIn("\033[33m", formatter.format("### 🟠 中等"))
            self.assertIn("\033[90m", formatter.format("### 🟡 轻微"))


class TestSummarize(unittest.TestCase):
    def test_read_file_summary(self) -> None:
        result = ToolResult.success(
            "x", path="a.py", total_lines=100, start=1, end=50
        )
        text = _summarize_tool_result("read_file", result)
        self.assertIn("a.py", text)
        self.assertIn("1-50", text)

    def test_search_summary(self) -> None:
        result = ToolResult.success("x", matches=3, files=2)
        self.assertIn("3 处命中", _summarize_tool_result("search_code", result))

    def test_failure_summary_uses_first_line(self) -> None:
        result = ToolResult.failure("文件不存在：ghost.py\n更多细节")
        self.assertEqual(_summarize_tool_result("read_file", result), "文件不存在：ghost.py")


class TestRelativeTo(TempWorkspaceTestCase):
    def test_inside(self) -> None:
        target = self.workspace / "pkg" / "a.py"
        self.assertEqual(_relative_to(target, self.workspace), "pkg/a.py")

    def test_same_directory(self) -> None:
        self.assertEqual(_relative_to(self.workspace, self.workspace), ".")

    def test_outside_falls_back_to_absolute(self) -> None:
        other = self.workspace.parent / "elsewhere.py"
        self.assertEqual(_relative_to(other, self.workspace), str(other))


# --------------------------------------------------------------------------- #
# 会话命令
# --------------------------------------------------------------------------- #
class TestReviewSession(TempWorkspaceTestCase):
    def make_session(self, **option_kwargs) -> tuple[ReviewSession, ConfigStore]:
        store = ConfigStore(self.workspace / "config.json")
        options = SessionOptions(color=False, **option_kwargs)
        return ReviewSession(store, Binding(), options), store

    def test_help_lists_every_command(self) -> None:
        session, _ = self.make_session()
        _, out = quiet(session.handle, "help")
        for command in ("help", "api", "review", "model", "url", "config", "exit"):
            self.assertIn(command, out)
        self.assertIn("API Key", HELP_TEXT)
        self.assertIn("提示输入", HELP_TEXT)

    def test_exit_returns_true(self) -> None:
        session, _ = self.make_session()
        for word in ("exit", "quit", "q", "退出"):
            self.assertTrue(quiet(session.handle, word)[0], word)

    def test_unknown_text_becomes_instruction(self) -> None:
        """没有绑定密钥时给自然语言，应提示先绑定而不是崩溃。"""
        session, _ = self.make_session()
        _, out = quiet(session.handle, "帮我检查空值处理")
        self.assertIn("还没有绑定 API Key", out)

    def test_review_missing_path(self) -> None:
        session, _ = self.make_session()
        _, out = quiet(session.handle, "review no/such/path.py")
        self.assertIn("路径不存在", out)

    def test_api_clear(self) -> None:
        session, store = self.make_session()
        store.save(Binding(api_key="sk-x"))
        session.binding = store.load()
        _, out = quiet(session.handle, "api clear")
        self.assertIn("已解除绑定", out)
        self.assertFalse((self.workspace / "config.json").exists())

    def test_api_show_masks_key(self) -> None:
        session, _ = self.make_session()
        session.binding = Binding(api_key="sk-abcdefghijklmnop")
        _, out = quiet(session.handle, "api show")
        self.assertIn("*", out)
        self.assertNotIn("abcdefghijkl", out)

    def test_model_and_url_commands_persist(self) -> None:
        session, store = self.make_session()
        quiet(session.handle, "model qwen-plus")
        quiet(session.handle, "url https://dashscope.aliyuncs.com/compatible-mode/v1")
        restored = store.load()
        self.assertEqual(restored.model, "qwen-plus")
        self.assertIn("dashscope", restored.base_url)
        # 参数变了，之前的校验结论必须失效
        self.assertFalse(restored.verified)

    def test_config_command_reports_paths(self) -> None:
        session, _ = self.make_session()
        _, out = quiet(session.handle, "config")
        self.assertIn("程序目录", out)
        self.assertIn("config.json", out)

    # -- 审查（离线 Mock 模型） ---------------------------------------- #
    def test_mock_review_produces_report(self) -> None:
        self.write("sample.py", "def f():\n    return 1\n")
        session, _ = self.make_session(mock=True)
        target = str(self.workspace / "sample.py")

        _, out = quiet(session.handle, f"review {target}")

        self.assertIn("read_file", out)
        self.assertTrue(session.last_report, "应产出一份报告")
        self.assertEqual(session.stats.reviews, 1)
        self.assertGreater(session.stats.tool_calls, 0)

    def test_mock_review_of_directory(self) -> None:
        self.write("a.py", "x = 1\n")
        self.write("b.py", "y = 2\n")
        session, _ = self.make_session(mock=True)
        _, out = quiet(session.handle, f"review {self.workspace}")
        self.assertIn("list_dir", out)
        self.assertTrue(session.last_report)

    def test_bare_path_is_treated_as_review(self) -> None:
        self.write("sample.py", "x = 1\n")
        session, _ = self.make_session(mock=True)
        quiet(session.handle, str(self.workspace / "sample.py"))
        self.assertEqual(session.stats.reviews, 1)

    def test_workspace_is_derived_from_target_file(self) -> None:
        """审查单个文件时，沙箱根应是它的父目录，否则读不到兄弟文件。"""
        self.write("pkg/mod.py", "x = 1\n")
        session, _ = self.make_session(mock=True)
        quiet(session.handle, f"review {self.workspace / 'pkg' / 'mod.py'}")
        self.assertEqual(session.agent_workspace, (self.workspace / "pkg").resolve())

    def test_switching_workspace_resets_context(self) -> None:
        self.write("p1/a.py", "x = 1\n")
        self.write("p2/b.py", "y = 2\n")
        session, _ = self.make_session(mock=True)
        quiet(session.handle, f"review {self.workspace / 'p1'}")
        first_agent = session.agent
        quiet(session.handle, f"review {self.workspace / 'p2'}")
        self.assertIsNot(session.agent, first_agent)

    def test_save_writes_markdown_report(self) -> None:
        self.write("sample.py", "x = 1\n")
        session, _ = self.make_session(mock=True)
        quiet(session.handle, f"review {self.workspace / 'sample.py'}")

        out_path = self.workspace / "report.md"
        _, out = quiet(session.handle, f"save {out_path}")
        self.assertIn("已保存", out)
        text = out_path.read_text(encoding="utf-8")
        self.assertIn("# 代码审查报告", text)
        self.assertIn("审查目标", text)

    def test_save_without_report_is_refused(self) -> None:
        session, _ = self.make_session(mock=True)
        _, out = quiet(session.handle, "save")
        self.assertIn("还没有可保存", out)

    def test_review_without_binding_is_refused(self) -> None:
        self.write("sample.py", "x = 1\n")
        session, _ = self.make_session()  # 非 mock，且未绑定
        _, out = quiet(session.handle, f"review {self.workspace / 'sample.py'}")
        self.assertIn("还没有绑定 API Key", out)


# --------------------------------------------------------------------------- #
# Agent 与 Prompt 的接线
# --------------------------------------------------------------------------- #
class TestReviewAgentWiring(TempWorkspaceTestCase):
    def test_custom_system_prompt_is_used(self) -> None:
        from codeagent.agent import CodeAgent
        from codeagent.mock_llm import ScriptedLLM, text_response
        from codeagent.tools import build_review_registry

        settings = self.make_settings(allow_write=False)
        context = self.make_context(allow_write=False)
        registry = build_review_registry(context)
        agent = CodeAgent(
            ScriptedLLM([text_response("结论")]),
            registry,
            settings,
            tool_context=context,
            system_prompt_template=REVIEW_SYSTEM_PROMPT,
        )
        prompt = agent.memory.system_prompt
        self.assertIn("证据来自工具", prompt)
        self.assertIn("文件:行号", prompt)
        self.assertIn("read_file(", prompt)
        # 确保没有退回通用助手模板
        self.assertNotIn("你是 CodeAgent，一个运行在用户本地终端中的代码助手", prompt)

    def test_default_template_still_used_without_override(self) -> None:
        from codeagent.agent import CodeAgent
        from codeagent.mock_llm import ScriptedLLM, text_response
        from codeagent.tools import build_review_registry

        settings = self.make_settings(allow_write=False)
        context = self.make_context(allow_write=False)
        agent = CodeAgent(
            ScriptedLLM([text_response("结论")]),
            build_review_registry(context),
            settings,
            tool_context=context,
        )
        self.assertIn("你是 CodeAgent", agent.memory.system_prompt)


# --------------------------------------------------------------------------- #
# 顶部入口
# --------------------------------------------------------------------------- #
class TestEntryPoint(unittest.TestCase):
    def test_help_command_returns_zero(self) -> None:
        code, out = quiet(review_main, ["help", "--no-color"])
        self.assertEqual(code, 0)
        self.assertIn("可用指令", out)

    def test_unknown_command_returns_two(self) -> None:
        code, out = quiet(review_main, ["definitely-not-a-command", "--no-color"])
        self.assertEqual(code, 2)
        self.assertIn("未知指令", out)

    def test_app_dir_points_at_project_root(self) -> None:
        root = app_dir()
        self.assertTrue((root / "main.py").is_file(), root)
        self.assertTrue((root / "codeagent").is_dir(), root)

    def test_config_path_is_beside_the_program(self) -> None:
        store = ConfigStore()
        self.assertEqual(store.primary_path.name, "config.json")
        self.assertEqual(store.primary_path.parent, app_dir())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
