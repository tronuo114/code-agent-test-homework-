"""LLM 客户端：OpenAI 兼容的 ``/chat/completions`` 协议。

只依赖标准库 ``urllib``，因此 DeepSeek / 通义千问 / OpenAI / Kimi / Ollama
等任何 OpenAI 兼容端点都能直接使用。

关键能力：
- **重试**：区分可重试（429 / 5xx / 超时 / 连接失败）与不可重试（400 / 401 / 404），
  指数退避 + 抖动，并尊重服务端的 ``Retry-After``。
- **流式**：解析 SSE，逐 token 回调；同时把分片的 ``tool_calls`` 增量按 index 合并。
- **降级**：流式请求失败时自动回退到非流式，避免因个别服务商 SSE 实现差异导致不可用。
- **可测试**：HTTP 传输层被抽象成 :class:`Transport`，测试中可注入假传输层，
  无需真实网络即可覆盖重试与解析逻辑。
"""

from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Iterator, Protocol, Sequence

from .config import Settings
from .errors import (
    LLMConnectionError,
    LLMError,
    LLMHTTPError,
    LLMResponseError,
    LLMTimeoutError,
)

Message = dict[str, Any]

#: 值得重试的 HTTP 状态码。
RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 522, 524})


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
@dataclass
class ToolCall:
    """一次工具调用请求（已尽可能解析成结构化参数）。"""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    raw_arguments: str = ""
    parse_error: str | None = None

    def to_message(self) -> dict[str, Any]:
        """转回 OpenAI 协议的 assistant.tool_calls 元素。"""
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": self.raw_arguments or "{}"},
        }


@dataclass
class Usage:
    """token 用量，缺失时按 0 计。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    @classmethod
    def from_raw(cls, raw: dict[str, Any] | None) -> "Usage":
        raw = raw or {}
        return cls(
            prompt_tokens=int(raw.get("prompt_tokens") or 0),
            completion_tokens=int(raw.get("completion_tokens") or 0),
            total_tokens=int(raw.get("total_tokens") or 0),
        )

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            self.prompt_tokens + other.prompt_tokens,
            self.completion_tokens + other.completion_tokens,
            self.total_tokens + other.total_tokens,
        )


@dataclass
class LLMResponse:
    """归一化后的模型响应。"""

    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"
    usage: Usage = field(default_factory=Usage)
    model: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)

    def to_assistant_message(self) -> Message:
        """构造要写回上下文的 assistant 消息。"""
        message: Message = {"role": "assistant", "content": self.content or ""}
        if self.tool_calls:
            message["tool_calls"] = [tc.to_message() for tc in self.tool_calls]
        return message


@dataclass
class RetryEvent:
    """一次重试的发生记录，供 CLI 展示与测试断言。"""

    attempt: int
    max_attempts: int
    delay: float
    reason: str


# --------------------------------------------------------------------------- #
# 传输层抽象
# --------------------------------------------------------------------------- #
class Transport(Protocol):
    """HTTP 传输层接口；测试中可注入假实现。"""

    def post(
        self,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout: float,
        stream: bool,
    ) -> Any:  # pragma: no cover - 协议声明
        ...


class UrllibTransport:
    """基于标准库的默认传输层。"""

    def post(
        self,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout: float,
        stream: bool,
    ) -> Any:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            response = urllib.request.urlopen(request, timeout=timeout)
        except urllib.error.HTTPError as exc:  # 非 2xx
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")
            except Exception:  # pragma: no cover - 读 body 失败不影响主流程
                detail = ""
            raise LLMHTTPError(
                _http_error_message(exc.code, detail),
                status_code=exc.code,
                body=detail,
                retryable=exc.code in RETRYABLE_STATUS,
            ) from exc
        except TimeoutError as exc:
            raise LLMTimeoutError(f"请求超时（>{timeout:g}s）：{url}") from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, TimeoutError):
                raise LLMTimeoutError(f"请求超时（>{timeout:g}s）：{url}") from exc
            raise LLMConnectionError(
                f"无法连接到 {url}：{reason}。请检查网络、base_url 与代理设置。"
            ) from exc
        except OSError as exc:  # 兜底：DNS / TLS / 连接重置
            raise LLMConnectionError(f"网络错误：{exc}") from exc

        if stream:
            return _StreamingResponse(response)

        try:
            raw = response.read().decode("utf-8", errors="replace")
        finally:
            response.close()
        return raw


class _StreamingResponse:
    """把 urllib 的响应包装成逐行可迭代对象，并在迭代结束时关闭连接。"""

    def __init__(self, response: Any) -> None:
        self._response = response

    def __iter__(self) -> Iterator[bytes]:
        try:
            for line in self._response:
                yield line
        finally:
            self._response.close()


def _http_error_message(code: int, detail: str) -> str:
    """把 HTTP 状态码翻译成人能看懂的中文提示。"""
    hints = {
        400: "请求格式有误（可能是模型不支持 tools / 参数越界）",
        401: "鉴权失败：API Key 无效或已过期",
        402: "账户余额不足",
        403: "没有访问该模型的权限",
        404: "端点或模型不存在：请检查 base_url 是否包含 /v1、模型名是否正确",
        413: "请求体过大：上下文超出模型限制",
        422: "参数校验失败",
        429: "触发限流（请求过于频繁）",
    }
    hint = hints.get(code, "服务端返回错误")
    snippet = detail.strip()
    if len(snippet) > 600:
        snippet = snippet[:600] + "…"
    return f"HTTP {code} — {hint}" + (f"\n响应体：{snippet}" if snippet else "")


# --------------------------------------------------------------------------- #
# 客户端
# --------------------------------------------------------------------------- #
class LLMClient:
    """OpenAI 兼容 Chat Completions 客户端。"""

    def __init__(
        self,
        settings: Settings,
        transport: Transport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        on_retry: Callable[[RetryEvent], None] | None = None,
    ) -> None:
        self.settings = settings
        self.transport = transport or UrllibTransport()
        self._sleep = sleep
        self._on_retry = on_retry
        self.last_usage = Usage()

    # ------------------------------------------------------------------ #
    # 对外接口
    # ------------------------------------------------------------------ #
    def chat(
        self,
        messages: Sequence[Message],
        tools: Sequence[dict[str, Any]] | None = None,
        *,
        stream: bool | None = None,
        on_text: Callable[[str], None] | None = None,
        tool_choice: str | None = None,
    ) -> LLMResponse:
        """发起一次对话补全。

        Args:
            messages: OpenAI 格式的消息列表。
            tools: 工具 schema 列表；为空表示本轮不允许调用工具。
            stream: 是否流式；``None`` 表示使用配置默认值。
            on_text: 流式文本增量回调。
            tool_choice: ``"auto"`` / ``"none"`` / ``"required"``。
        """
        use_stream = self.settings.stream if stream is None else stream
        payload = self._build_payload(messages, tools, stream=use_stream, tool_choice=tool_choice)

        if use_stream and on_text is not None:
            try:
                return self._chat_streaming(payload, on_text)
            except LLMError as exc:
                # 只有在"尚未输出任何内容"时才降级，否则会造成重复输出。
                if getattr(exc, "_partial_output", False):
                    raise
                if self.settings.verbose:
                    print(f"[llm] 流式失败，回退非流式：{exc}")
                payload["stream"] = False
                return self._chat_once(payload)

        payload["stream"] = False
        return self._chat_once(payload)

    # ------------------------------------------------------------------ #
    # 内部实现
    # ------------------------------------------------------------------ #
    def _build_payload(
        self,
        messages: Sequence[Message],
        tools: Sequence[dict[str, Any]] | None,
        *,
        stream: bool,
        tool_choice: str | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.settings.model,
            "messages": list(messages),
            "temperature": float(self.settings.temperature),
            "stream": stream,
        }
        if self.settings.max_tokens and self.settings.max_tokens > 0:
            payload["max_tokens"] = int(self.settings.max_tokens)
        if tools:
            payload["tools"] = list(tools)
            payload["tool_choice"] = tool_choice or "auto"
        return payload

    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if self.settings.stream else "application/json",
            "Authorization": f"Bearer {self.settings.api_key}",
            "User-Agent": "code-agent/1.0 (+stdlib-urllib)",
        }

    # -- 非流式 -------------------------------------------------------- #
    def _chat_once(self, payload: dict[str, Any]) -> LLMResponse:
        raw = self._request_with_retry(payload, stream=False)
        return self._parse_response(raw)

    def _parse_response(self, raw: Any) -> LLMResponse:
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", errors="replace")
        if not isinstance(raw, str):
            raise LLMResponseError(f"非预期的响应类型：{type(raw).__name__}")

        text = raw.strip()
        if not text:
            raise LLMResponseError("服务端返回了空响应体")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMResponseError(
                f"响应不是合法 JSON：{exc}\n原始内容前 500 字符：{text[:500]}"
            ) from exc

        if isinstance(data, dict) and data.get("error"):
            err = data["error"]
            detail = err.get("message") if isinstance(err, dict) else str(err)
            raise LLMResponseError(f"服务端返回错误对象：{detail}")

        choices = (data or {}).get("choices") or []
        if not choices:
            raise LLMResponseError(f"响应中缺少 choices 字段：{text[:500]}")

        choice = choices[0]
        message = choice.get("message") or {}
        usage = Usage.from_raw((data or {}).get("usage"))

        response = LLMResponse(
            content=_stringify_content(message.get("content")),
            tool_calls=_parse_tool_calls(message.get("tool_calls")),
            finish_reason=choice.get("finish_reason") or "stop",
            usage=usage,
            model=(data or {}).get("model") or self.settings.model,
            raw=data if isinstance(data, dict) else {},
        )
        self.last_usage = self.last_usage + usage
        return response

    # -- 流式 ---------------------------------------------------------- #
    def _chat_streaming(
        self, payload: dict[str, Any], on_text: Callable[[str], None]
    ) -> LLMResponse:
        payload = dict(payload, stream=True)
        stream = self._request_with_retry(payload, stream=True)

        content_parts: list[str] = []
        tool_fragments: dict[int, dict[str, Any]] = {}
        finish_reason = "stop"
        model = self.settings.model
        usage = Usage()
        emitted = False

        try:
            for event in _iter_sse(stream):
                if event == "[DONE]":
                    break
                try:
                    chunk = json.loads(event)
                except json.JSONDecodeError:
                    continue
                if isinstance(chunk, dict) and chunk.get("error"):
                    err = chunk["error"]
                    detail = err.get("message") if isinstance(err, dict) else str(err)
                    raise LLMResponseError(f"流式响应中返回错误：{detail}")

                model = (chunk or {}).get("model") or model
                if (chunk or {}).get("usage"):
                    usage = Usage.from_raw(chunk["usage"])

                for choice in (chunk or {}).get("choices") or []:
                    if choice.get("finish_reason"):
                        finish_reason = choice["finish_reason"]
                    delta = choice.get("delta") or {}
                    piece = _stringify_content(delta.get("content"))
                    if piece:
                        content_parts.append(piece)
                        emitted = True
                        on_text(piece)
                    for frag in delta.get("tool_calls") or []:
                        _merge_tool_fragment(tool_fragments, frag)
        except LLMError:
            raise
        except Exception as exc:  # 网络中断等
            error = LLMConnectionError(f"流式读取中断：{exc}")
            setattr(error, "_partial_output", emitted)
            raise error from exc

        if not content_parts and not tool_fragments:
            raise LLMResponseError("流式响应结束但未收到任何内容")

        response = LLMResponse(
            content="".join(content_parts),
            tool_calls=_finalize_tool_fragments(tool_fragments),
            finish_reason=finish_reason,
            usage=usage,
            model=model,
        )
        self.last_usage = self.last_usage + usage
        return response

    # -- 重试 ---------------------------------------------------------- #
    def _request_with_retry(self, payload: dict[str, Any], *, stream: bool) -> Any:
        url = self.settings.chat_endpoint
        headers = self._headers()
        attempts = max(1, int(self.settings.max_retries))
        last_error: LLMError | None = None

        for attempt in range(1, attempts + 1):
            try:
                return self.transport.post(
                    url, headers, payload, float(self.settings.timeout), stream
                )
            except LLMError as exc:
                last_error = exc
                if not getattr(exc, "retryable", False) or attempt >= attempts:
                    raise
                delay = self._backoff_delay(attempt, exc)
                if self._on_retry:
                    self._on_retry(
                        RetryEvent(
                            attempt=attempt,
                            max_attempts=attempts,
                            delay=delay,
                            reason=exc.message.splitlines()[0],
                        )
                    )
                self._sleep(delay)

        # 理论不可达，仅为类型完备
        raise last_error or LLMError("未知的 LLM 调用失败")

    @staticmethod
    def _backoff_delay(attempt: int, error: LLMError) -> float:
        """指数退避 + 抖动；若服务端给了 Retry-After 则优先采用。"""
        body = getattr(error, "body", "") or ""
        hint = _parse_retry_after(body)
        if hint is not None:
            return min(hint, 30.0)
        base = 0.8 * (2 ** (attempt - 1))
        return min(base + random.uniform(0, 0.4), 20.0)


# --------------------------------------------------------------------------- #
# 解析辅助
# --------------------------------------------------------------------------- #
def _stringify_content(content: Any) -> str:
    """兼容 ``content`` 为字符串、``None`` 或内容块数组（多模态）的情况。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
                elif isinstance(text, dict) and isinstance(text.get("value"), str):
                    parts.append(text["value"])
        return "".join(parts)
    return str(content)


def _parse_tool_calls(raw_calls: Any) -> list[ToolCall]:
    """解析非流式响应里的 ``message.tool_calls``。"""
    calls: list[ToolCall] = []
    if not raw_calls:
        return calls
    for index, item in enumerate(raw_calls):
        if not isinstance(item, dict):
            continue
        function = item.get("function") or {}
        name = function.get("name") or ""
        raw_args = function.get("arguments")
        call = ToolCall(
            id=item.get("id") or f"call_{index}",
            name=name,
        )
        call.arguments, call.raw_arguments, call.parse_error = _decode_arguments(raw_args)
        if not name:
            call.parse_error = call.parse_error or "模型未提供工具名"
        calls.append(call)
    return calls


def _decode_arguments(raw_args: Any) -> tuple[dict[str, Any], str, str | None]:
    """把 ``arguments`` 解码成 dict，返回 (参数, 原始文本, 错误说明)。"""
    if raw_args is None or raw_args == "":
        return {}, "{}", None
    if isinstance(raw_args, dict):
        return raw_args, json.dumps(raw_args, ensure_ascii=False), None
    if not isinstance(raw_args, str):
        return {}, str(raw_args), f"arguments 类型异常：{type(raw_args).__name__}"

    text = raw_args.strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        return {}, text, f"工具参数不是合法 JSON（{exc}）"
    if not isinstance(parsed, dict):
        return {}, text, f"工具参数应为 JSON 对象，实际为 {type(parsed).__name__}"
    return parsed, text, None


def _merge_tool_fragment(store: dict[int, dict[str, Any]], frag: dict[str, Any]) -> None:
    """把流式分片的 tool_call 增量按 ``index`` 合并。"""
    index = frag.get("index")
    if not isinstance(index, int):
        index = len(store)
    slot = store.setdefault(index, {"id": "", "name": "", "arguments": ""})
    if frag.get("id"):
        slot["id"] = frag["id"]
    function = frag.get("function") or {}
    if function.get("name"):
        # 工具名同样可能被切成多片（例如 "read_" + "file"），必须累加而不是覆盖。
        slot["name"] += function["name"]
    args = function.get("arguments")
    if isinstance(args, str):
        slot["arguments"] += args
    elif isinstance(args, dict):
        slot["arguments"] += json.dumps(args, ensure_ascii=False)


def _finalize_tool_fragments(store: dict[int, dict[str, Any]]) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for index in sorted(store):
        slot = store[index]
        call = ToolCall(
            id=slot.get("id") or f"call_{index}",
            name=slot.get("name") or "",
        )
        call.arguments, call.raw_arguments, call.parse_error = _decode_arguments(
            slot.get("arguments")
        )
        if not call.name:
            call.parse_error = call.parse_error or "模型未提供工具名"
        calls.append(call)
    return calls


def _iter_sse(stream: Any) -> Iterator[str]:
    """从字节流中解析 SSE，产出 ``data:`` 后的负载文本。"""
    if isinstance(stream, (str, bytes)):
        text = stream.decode("utf-8", "replace") if isinstance(stream, bytes) else stream
        for line in text.splitlines():
            payload = _sse_payload(line)
            if payload is not None:
                yield payload
        return

    for raw_line in stream:
        if isinstance(raw_line, bytes):
            line = raw_line.decode("utf-8", errors="replace")
        else:
            line = str(raw_line)
        payload = _sse_payload(line)
        if payload is not None:
            yield payload


def _sse_payload(line: str) -> str | None:
    line = line.strip()
    if not line or line.startswith(":"):
        return None
    if line.startswith("data:"):
        return line[len("data:") :].strip()
    return None


def _parse_retry_after(body: str) -> float | None:
    """尽力从错误响应体里挖出 ``retry_after`` 秒数。"""
    if not body:
        return None
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    for key in ("retry_after", "retryAfter"):
        value = data.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    error = data.get("error")
    if isinstance(error, dict):
        value = error.get("retry_after")
        if isinstance(value, (int, float)):
            return float(value)
    return None


__all__ = [
    "LLMClient",
    "LLMResponse",
    "LLMError",
    "ToolCall",
    "Usage",
    "RetryEvent",
    "Transport",
    "UrllibTransport",
    "Message",
]
