"""API Key 联网校验。

用户绑定时必须立刻拿到"成功还是失败"的结论，所以这里真的发一次请求，
而不是只检查字符串格式。为了把成本压到最低，请求被设计成**极小**的一次
``chat/completions`` 调用（一个字的提示词 + ``max_tokens=8``）。

校验结果分三档，而不是简单的成功/失败：

- ``ok``   —— 明确可用（HTTP 2xx，能正常返回内容）；
- ``warn`` —— 无法判定或暂时不可用（限流、服务端 5xx、网络不通）。
  这类情况**不该**判用户的密钥有错，因此仍然保存，只标注"未通过校验"；
- ``fail`` —— 明确不可用（401 鉴权失败、403 无权限、404 端点或模型不存在等）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .config import Settings
from .errors import LLMError, LLMHTTPError

#: 校验用的极小提示词。
_PROBE_MESSAGE = "1"

#: 校验请求允许生成的最大 token 数（越小越省钱）。
_PROBE_MAX_TOKENS = 8


@dataclass
class VerifyResult:
    """一次密钥校验的结果。"""

    level: str  # "ok" | "warn" | "fail"
    message: str
    detail: str = ""
    latency: float = 0.0
    model_reply: str = ""
    context: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """是否应视为"绑定成功"（``warn`` 也算成功，因为它不代表密钥有错）。"""
        return self.level in {"ok", "warn"}

    @property
    def verified(self) -> bool:
        """是否**确实验证通过**（只有 ``ok`` 才算）。"""
        return self.level == "ok"


def verify_binding(
    api_key: str,
    base_url: str,
    model: str,
    *,
    timeout: float = 30.0,
    transport: Any = None,
) -> VerifyResult:
    """通过一次极小请求验证密钥是否可用。

    Args:
        transport: 可选的 HTTP 传输层替身，仅用于测试 —— 有了它才能离线覆盖
            "401 / 403 / 429 / 5xx 分别怎么归类"这类分支。
    """
    if not api_key or not api_key.strip():
        return VerifyResult("fail", "API Key 为空")
    if not base_url.startswith(("http://", "https://")):
        return VerifyResult("fail", f"端点格式不对：{base_url!r}（应形如 https://api.deepseek.com/v1）")
    if not model.strip():
        return VerifyResult("fail", "模型名为空")

    settings = Settings(
        api_key=api_key.strip(),
        base_url=base_url.strip(),
        model=model.strip(),
        temperature=0.0,
        max_tokens=_PROBE_MAX_TOKENS,
        timeout=timeout,
        max_retries=1,
        stream=False,
    )

    # 延迟导入，避免 review_cli → verify → llm 的模块级循环依赖风险
    from .llm import LLMClient

    client = LLMClient(settings, transport=transport)
    started = time.monotonic()
    try:
        response = client.chat(
            [{"role": "user", "content": _PROBE_MESSAGE}],
            stream=False,
        )
    except LLMHTTPError as exc:
        return _classify_http_error(exc, model, time.monotonic() - started)
    except LLMError as exc:
        return VerifyResult(
            "warn",
            "无法完成校验（网络问题），密钥已保存但未验证",
            detail=str(exc),
            latency=time.monotonic() - started,
        )
    except Exception as exc:  # 兜底，绝不让校验本身把程序打崩
        return VerifyResult(
            "warn",
            "校验过程出现未预期的错误，密钥已保存但未验证",
            detail=f"{type(exc).__name__}: {exc}",
            latency=time.monotonic() - started,
        )

    latency = time.monotonic() - started
    reply = (response.content or "").strip()
    return VerifyResult(
        "ok",
        f"校验通过，模型 {response.model or model} 可正常调用",
        latency=latency,
        model_reply=reply[:60],
    )


def _classify_http_error(exc: LLMHTTPError, model: str, latency: float) -> VerifyResult:
    """把 HTTP 错误翻译成用户能直接行动的中文结论。"""
    status = exc.status_code
    snippet = _first_line(exc.body)

    if status == 401:
        return VerifyResult("fail", "API Key 无效或已被停用", snippet, latency)
    if status == 402:
        return VerifyResult("fail", "账户余额不足，请先充值", snippet, latency)
    if status == 403:
        return VerifyResult(
            "fail", f"该密钥没有访问模型 {model} 的权限", snippet, latency
        )
    if status == 404:
        return VerifyResult(
            "fail",
            "端点或模型不存在，请检查接口地址是否带 /v1、模型名是否正确",
            snippet,
            latency,
        )
    if status == 400:
        return VerifyResult(
            "fail",
            "请求被服务端拒绝（该模型可能不支持当前调用方式）",
            snippet or exc.message,
            latency,
        )
    if status == 429:
        return VerifyResult(
            "warn",
            "密钥有效，但当前请求过于频繁被限流（稍后再试即可）",
            snippet,
            latency,
        )
    if status is not None and 500 <= status < 600:
        return VerifyResult(
            "warn",
            f"服务端临时故障（HTTP {status}），密钥未能验证",
            snippet,
            latency,
        )
    return VerifyResult(
        "warn",
        f"未能确认密钥有效（HTTP {status}）",
        snippet or exc.message,
        latency,
    )


def _first_line(text: str, limit: int = 200) -> str:
    if not text:
        return ""
    line = text.strip().splitlines()[0]
    return line if len(line) <= limit else line[: limit - 1] + "…"


__all__ = ["VerifyResult", "verify_binding"]
