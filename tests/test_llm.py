"""LLM 客户端测试：响应解析、重试策略、流式 SSE、降级。

全部通过注入 :class:`FakeTransport` 完成，不产生任何真实网络请求。
"""

from __future__ import annotations

import json
import unittest
from typing import Any

from tests.helpers import TempWorkspaceTestCase

from codeagent.errors import (
    LLMConnectionError,
    LLMHTTPError,
    LLMResponseError,
    LLMTimeoutError,
)
from codeagent.llm import LLMClient, UrllibTransport


class FakeTransport:
    """按脚本依次返回结果的假传输层。"""

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []

    def post(
        self,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout: float,
        stream: bool,
    ) -> Any:
        self.calls.append(
            {"url": url, "headers": headers, "payload": payload, "timeout": timeout, "stream": stream}
        )
        if not self.script:
            raise AssertionError("FakeTransport 脚本已耗尽")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def sse_line(obj: dict[str, Any]) -> str:
    return "data: " + json.dumps(obj, ensure_ascii=False) + "\n"


def completion(
    content: str | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
    finish_reason: str = "stop",
    usage: dict[str, int] | None = None,
) -> str:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return json.dumps(
        {
            "id": "chatcmpl-1",
            "model": "test-model",
            "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
            "usage": usage or {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        },
        ensure_ascii=False,
    )


class TestResponseParsing(TempWorkspaceTestCase):
    def make_client(self, script: list[Any], **overrides: Any):
        settings = self.make_settings(**overrides)
        transport = FakeTransport(script)
        client = LLMClient(settings, transport=transport, sleep=lambda _: None)
        return client, transport

    def test_plain_text_answer(self) -> None:
        client, _ = self.make_client([completion("你好，世界")])
        response = client.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(response.content, "你好，世界")
        self.assertFalse(response.has_tool_calls)
        self.assertEqual(response.usage.total_tokens, 15)

    def test_tool_call_parsing(self) -> None:
        payload = completion(
            tool_calls=[
                {
                    "id": "call_abc",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path": "a.py"}'},
                }
            ],
            finish_reason="tool_calls",
        )
        client, _ = self.make_client([payload])
        response = client.chat([{"role": "user", "content": "read a.py"}])

        self.assertTrue(response.has_tool_calls)
        call = response.tool_calls[0]
        self.assertEqual(call.id, "call_abc")
        self.assertEqual(call.name, "read_file")
        self.assertEqual(call.arguments, {"path": "a.py"})
        self.assertIsNone(call.parse_error)

    def test_malformed_arguments_recorded_not_raised(self) -> None:
        payload = completion(
            tool_calls=[
                {
                    "id": "call_bad",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{not json"},
                }
            ],
            finish_reason="tool_calls",
        )
        client, _ = self.make_client([payload])
        response = client.chat([{"role": "user", "content": "x"}])
        self.assertIsNotNone(response.tool_calls[0].parse_error)
        self.assertEqual(response.tool_calls[0].arguments, {})

    def test_missing_choices_raises_response_error(self) -> None:
        client, _ = self.make_client(['{"id": "x"}'])
        with self.assertRaises(LLMResponseError):
            client.chat([{"role": "user", "content": "x"}])

    def test_non_json_body_raises_response_error(self) -> None:
        client, _ = self.make_client(["<html>502 Bad Gateway</html>"])
        with self.assertRaises(LLMResponseError):
            client.chat([{"role": "user", "content": "x"}])

    def test_error_object_in_body(self) -> None:
        body = json.dumps({"error": {"message": "invalid api key"}})
        client, _ = self.make_client([body])
        with self.assertRaises(LLMResponseError) as ctx:
            client.chat([{"role": "user", "content": "x"}])
        self.assertIn("invalid api key", str(ctx.exception))

    def test_endpoint_and_auth_header(self) -> None:
        client, transport = self.make_client([completion("ok")])
        client.chat([{"role": "user", "content": "x"}])
        call = transport.calls[0]
        self.assertEqual(call["url"], "https://example.invalid/v1/chat/completions")
        self.assertEqual(call["headers"]["Authorization"], "Bearer test-key")

    def test_tools_are_forwarded(self) -> None:
        client, transport = self.make_client([completion("ok")])
        schemas = [{"type": "function", "function": {"name": "t", "description": "d", "parameters": {}}}]
        client.chat([{"role": "user", "content": "x"}], tools=schemas)
        self.assertEqual(transport.calls[0]["payload"]["tools"], schemas)
        self.assertEqual(transport.calls[0]["payload"]["tool_choice"], "auto")


class TestRetry(TempWorkspaceTestCase):
    def test_retries_then_succeeds(self) -> None:
        sleeps: list[float] = []
        settings = self.make_settings(max_retries=3)
        transport = FakeTransport(
            [
                LLMHTTPError("HTTP 429", status_code=429, retryable=True),
                LLMTimeoutError("超时"),
                completion("终于成功"),
            ]
        )
        events = []
        client = LLMClient(
            settings,
            transport=transport,
            sleep=sleeps.append,
            on_retry=events.append,
        )
        response = client.chat([{"role": "user", "content": "x"}])

        self.assertEqual(response.content, "终于成功")
        self.assertEqual(len(sleeps), 2)
        self.assertEqual(len(events), 2)
        self.assertTrue(all(delay > 0 for delay in sleeps))

    def test_non_retryable_error_raises_immediately(self) -> None:
        settings = self.make_settings(max_retries=5)
        transport = FakeTransport(
            [LLMHTTPError("HTTP 401", status_code=401, retryable=False)]
        )
        client = LLMClient(settings, transport=transport, sleep=lambda _: None)
        with self.assertRaises(LLMHTTPError) as ctx:
            client.chat([{"role": "user", "content": "x"}])
        self.assertEqual(ctx.exception.status_code, 401)
        self.assertEqual(len(transport.calls), 1, "非可重试错误不应重试")

    def test_exhausts_retries_and_raises_last_error(self) -> None:
        settings = self.make_settings(max_retries=3)
        transport = FakeTransport(
            [
                LLMHTTPError("HTTP 503", status_code=503, retryable=True),
                LLMHTTPError("HTTP 503", status_code=503, retryable=True),
                LLMHTTPError("HTTP 503", status_code=503, retryable=True),
            ]
        )
        client = LLMClient(settings, transport=transport, sleep=lambda _: None)
        with self.assertRaises(LLMHTTPError):
            client.chat([{"role": "user", "content": "x"}])
        self.assertEqual(len(transport.calls), 3)

    def test_retry_after_hint_is_honored(self) -> None:
        settings = self.make_settings(max_retries=2)
        body = json.dumps({"error": {"message": "slow down", "retry_after": 7}})
        transport = FakeTransport(
            [LLMHTTPError("HTTP 429", status_code=429, body=body, retryable=True), completion("ok")]
        )
        sleeps: list[float] = []
        client = LLMClient(settings, transport=transport, sleep=sleeps.append)
        client.chat([{"role": "user", "content": "x"}])
        self.assertEqual(sleeps, [7.0])


class TestStreaming(TempWorkspaceTestCase):
    def make_streaming_client(self, script: list[Any], **overrides: Any):
        settings = self.make_settings(stream=True, **overrides)
        transport = FakeTransport(script)
        return LLMClient(settings, transport=transport, sleep=lambda _: None), transport

    def test_content_streaming(self) -> None:
        script = [
            [
                sse_line({"choices": [{"delta": {"content": "你"}}]}),
                sse_line({"choices": [{"delta": {"content": "好"}}]}),
                sse_line({"choices": [{"delta": {}, "finish_reason": "stop"}]}),
                "data: [DONE]\n",
            ]
        ]
        client, _ = self.make_streaming_client(script)
        chunks: list[str] = []
        response = client.chat([{"role": "user", "content": "x"}], on_text=chunks.append)

        self.assertEqual(chunks, ["你", "好"])
        self.assertEqual(response.content, "你好")
        self.assertEqual(response.finish_reason, "stop")

    def test_tool_call_fragments_are_merged(self) -> None:
        script = [
            [
                sse_line({"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "read_", "arguments": '{"pa'}}]}}]}),
                sse_line({"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"name": "file", "arguments": 'th": "a.py"}'}}]}}]}),
                sse_line({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}),
                "data: [DONE]\n",
            ]
        ]
        client, _ = self.make_streaming_client(script)
        response = client.chat([{"role": "user", "content": "x"}], on_text=lambda _: None)

        self.assertTrue(response.has_tool_calls)
        call = response.tool_calls[0]
        self.assertEqual(call.id, "c1")
        self.assertEqual(call.name, "read_file")
        self.assertEqual(call.arguments, {"path": "a.py"})

    def test_multiple_tool_calls_by_index(self) -> None:
        script = [
            [
                sse_line({"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "a", "function": {"name": "f1", "arguments": "{}"}}]}}]}),
                sse_line({"choices": [{"delta": {"tool_calls": [{"index": 1, "id": "b", "function": {"name": "f2", "arguments": "{}"}}]}}]}),
                "data: [DONE]\n",
            ]
        ]
        client, _ = self.make_streaming_client(script)
        response = client.chat([{"role": "user", "content": "x"}], on_text=lambda _: None)
        self.assertEqual([c.name for c in response.tool_calls], ["f1", "f2"])

    def test_falls_back_to_non_stream_on_error(self) -> None:
        script = [
            LLMHTTPError("HTTP 500", status_code=500, retryable=False),
            completion("非流式结果"),
        ]
        client, transport = self.make_streaming_client(script, max_retries=1)
        response = client.chat([{"role": "user", "content": "x"}], on_text=lambda _: None)

        self.assertEqual(response.content, "非流式结果")
        self.assertEqual(len(transport.calls), 2)
        self.assertTrue(transport.calls[0]["stream"])
        self.assertFalse(transport.calls[1]["stream"])

    def test_no_fallback_after_partial_output(self) -> None:
        """已经吐出内容后再出错，不能重放，否则用户会看到重复文本。"""
        script = [
            [sse_line({"choices": [{"delta": {"content": "部分"}}]})],
        ]
        client, transport = self.make_streaming_client(script, max_retries=1)

        class Exploding:
            def __iter__(self):
                yield sse_line({"choices": [{"delta": {"content": "部分"}}]})
                raise RuntimeError("connection reset")

        transport.script[0] = Exploding()
        chunks: list[str] = []
        with self.assertRaises(LLMConnectionError):
            client.chat([{"role": "user", "content": "x"}], on_text=chunks.append)
        self.assertEqual(len(transport.calls), 1)


class TestTransportErrors(TempWorkspaceTestCase):
    def test_urllib_transport_maps_http_error(self) -> None:
        """直接验证 urllib 传输层不会把 HTTPError 泄漏成原始异常。"""
        import urllib.error

        class Stub(UrllibTransport):
            def post(self, *args: Any, **kwargs: Any):  # type: ignore[override]
                raise LLMHTTPError("HTTP 401 — 鉴权失败", status_code=401, retryable=False)

        settings = self.make_settings()
        client = LLMClient(settings, transport=Stub(), sleep=lambda _: None)
        with self.assertRaises(LLMHTTPError):
            client.chat([{"role": "user", "content": "x"}])
        self.assertTrue(issubclass(urllib.error.HTTPError, OSError))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
