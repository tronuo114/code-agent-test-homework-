"""配置层测试：.env 解析、来源优先级、校验与脱敏。"""

from __future__ import annotations

import unittest

from tests.helpers import PROJECT_ROOT, TempWorkspaceTestCase  # noqa: F401

from codeagent.config import Settings, parse_dotenv
from codeagent.errors import ConfigError


class TestDotenvParsing(TempWorkspaceTestCase):
    def test_parses_basic_pairs(self) -> None:
        path = self.workspace / ".env"
        path.write_text(
            "\n".join(
                [
                    "# 注释行",
                    "",
                    "CODEAGENT_MODEL=deepseek-chat",
                    "export CODEAGENT_BASE_URL=https://api.example.com/v1",
                    'CODEAGENT_API_KEY="sk-quoted"',
                    "CODEAGENT_TEMPERATURE=0.7",
                ]
            ),
            encoding="utf-8",
        )
        values = parse_dotenv(path)
        self.assertEqual(values["CODEAGENT_MODEL"], "deepseek-chat")
        self.assertEqual(values["CODEAGENT_BASE_URL"], "https://api.example.com/v1")
        self.assertEqual(values["CODEAGENT_API_KEY"], "sk-quoted")
        self.assertEqual(values["CODEAGENT_TEMPERATURE"], "0.7")

    def test_missing_file_returns_empty(self) -> None:
        self.assertEqual(parse_dotenv(self.workspace / "nope.env"), {})

    def test_malformed_lines_are_skipped(self) -> None:
        path = self.workspace / ".env"
        path.write_text("这行没有等号\n=没有键\nA=1\n", encoding="utf-8")
        self.assertEqual(parse_dotenv(path), {"A": "1"})


class TestSettingsLoading(TempWorkspaceTestCase):
    def test_cli_overrides_env(self) -> None:
        settings = Settings.load(
            cli_args={"model": "from-cli"},
            env={"CODEAGENT_MODEL": "from-env"},
            cwd=self.workspace,
        )
        self.assertEqual(settings.model, "from-cli")
        self.assertEqual(settings.sources["model"], "cli")

    def test_env_overrides_dotenv(self) -> None:
        (self.workspace / ".env").write_text("CODEAGENT_MODEL=from-file\n", encoding="utf-8")
        settings = Settings.load(
            env={"CODEAGENT_MODEL": "from-env"},
            dotenv_path=self.workspace / ".env",
            cwd=self.workspace,
        )
        self.assertEqual(settings.model, "from-env")

    def test_dotenv_used_when_nothing_else(self) -> None:
        (self.workspace / ".env").write_text(
            "CODEAGENT_MODEL=from-file\nCODEAGENT_MAX_STEPS=7\n", encoding="utf-8"
        )
        settings = Settings.load(
            env={}, dotenv_path=self.workspace / ".env", cwd=self.workspace
        )
        self.assertEqual(settings.model, "from-file")
        self.assertEqual(settings.max_steps, 7)
        self.assertIsInstance(settings.max_steps, int)

    def test_api_key_aliases(self) -> None:
        for name in ("CODEAGENT_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY"):
            settings = Settings.load(env={name: "sk-x"}, cwd=self.workspace)
            self.assertEqual(settings.api_key, "sk-x", name)

    def test_boolean_coercion(self) -> None:
        settings = Settings.load(env={"CODEAGENT_ALLOW_WRITE": "false"}, cwd=self.workspace)
        self.assertFalse(settings.allow_write)
        settings = Settings.load(env={"CODEAGENT_ALLOW_WRITE": "1"}, cwd=self.workspace)
        self.assertTrue(settings.allow_write)


class TestValidation(TempWorkspaceTestCase):
    def test_missing_key_raises_with_actionable_message(self) -> None:
        settings = Settings.load(env={}, cwd=self.workspace)
        with self.assertRaises(ConfigError) as ctx:
            settings.validate()
        self.assertIn("API Key", str(ctx.exception))
        self.assertIn("--mock", str(ctx.exception))

    def test_mock_mode_skips_key_requirement(self) -> None:
        settings = Settings.load(cli_args={"mock": True}, env={}, cwd=self.workspace)
        settings.validate()  # 不应抛异常

    def test_bad_base_url(self) -> None:
        settings = Settings.load(
            cli_args={"api_key": "k", "base_url": "api.example.com"}, env={}, cwd=self.workspace
        )
        with self.assertRaises(ConfigError):
            settings.validate()

    def test_bad_temperature(self) -> None:
        settings = Settings.load(
            cli_args={"api_key": "k", "temperature": 5.0}, env={}, cwd=self.workspace
        )
        with self.assertRaises(ConfigError):
            settings.validate()

    def test_missing_workspace(self) -> None:
        settings = Settings.load(
            cli_args={"api_key": "k", "workspace": str(self.workspace / "ghost")},
            env={},
            cwd=self.workspace,
        )
        with self.assertRaises(ConfigError):
            settings.validate()


class TestHelpers(TempWorkspaceTestCase):
    def test_redacted_masks_key(self) -> None:
        settings = self.make_settings(api_key="sk-1234567890abcdef")
        snapshot = settings.redacted()
        self.assertNotIn("1234567890", str(snapshot["api_key"]))
        self.assertIn("*", snapshot["api_key"])

    def test_endpoint_join(self) -> None:
        base = self.make_settings(base_url="https://api.deepseek.com/v1")
        self.assertEqual(base.chat_endpoint, "https://api.deepseek.com/v1/chat/completions")

        trailing = self.make_settings(base_url="https://api.deepseek.com/v1/")
        self.assertEqual(trailing.chat_endpoint, "https://api.deepseek.com/v1/chat/completions")

        full = self.make_settings(base_url="https://x.com/v1/chat/completions")
        self.assertEqual(full.chat_endpoint, "https://x.com/v1/chat/completions")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
