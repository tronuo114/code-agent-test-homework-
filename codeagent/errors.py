"""统一异常体系。

设计原则：
- 所有异常都继承自 ``AgentError``，上层（CLI / Agent 循环）只需捕获一个基类。
- LLM 层与工具层的错误分开，便于分别决定"重试"还是"回灌给模型自纠错"。
- 每个异常都携带足够的上下文（状态码、工具名、参数），避免丢失诊断信息。
"""

from __future__ import annotations

from typing import Any


class AgentError(Exception):
    """本项目的所有异常基类。"""

    #: 供 CLI 展示时使用的简短分类标签。
    kind: str = "error"

    def __init__(self, message: str, **context: Any) -> None:
        super().__init__(message)
        self.message = message
        self.context: dict[str, Any] = context

    def __str__(self) -> str:  # pragma: no cover - 简单透传
        return self.message


# --------------------------------------------------------------------------- #
# 配置层
# --------------------------------------------------------------------------- #
class ConfigError(AgentError):
    """配置缺失或非法（例如没有 API Key）。"""

    kind = "config"


# --------------------------------------------------------------------------- #
# LLM 层
# --------------------------------------------------------------------------- #
class LLMError(AgentError):
    """LLM 调用相关错误基类。"""

    kind = "llm"

    #: 该错误是否值得重试。
    retryable: bool = False


class LLMHTTPError(LLMError):
    """服务端返回了非 2xx 状态码。"""

    kind = "llm.http"

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        body: str = "",
        retryable: bool = False,
    ) -> None:
        super().__init__(message, status_code=status_code, body=body)
        self.status_code = status_code
        self.body = body
        self.retryable = retryable


class LLMTimeoutError(LLMError):
    """请求超时（连接超时或读取超时）。"""

    kind = "llm.timeout"
    retryable = True


class LLMConnectionError(LLMError):
    """网络层不可达（DNS、连接被拒绝、TLS 失败等）。"""

    kind = "llm.connection"
    retryable = True


class LLMResponseError(LLMError):
    """响应体不符合预期（非法 JSON、缺少 choices、工具参数无法解析等）。"""

    kind = "llm.response"
    retryable = False


# --------------------------------------------------------------------------- #
# 工具层
# --------------------------------------------------------------------------- #
class ToolError(AgentError):
    """工具执行失败基类。

    这类错误通常**不会**中断 Agent 循环，而是被格式化成一条 ``tool`` 消息回灌给
    模型，让模型自行修正参数后重试。
    """

    kind = "tool"


class ToolNotFound(ToolError):
    """模型请求了一个未注册的工具。"""

    kind = "tool.not_found"


class ToolArgumentError(ToolError):
    """工具参数缺失、类型错误或包含未知字段。"""

    kind = "tool.argument"


class ToolExecutionError(ToolError):
    """工具内部执行失败（IO 错误、子进程非零退出等）。"""

    kind = "tool.execution"


class ToolPermissionError(ToolError):
    """当前会话不允许该操作（例如只读模式下写文件）。"""

    kind = "tool.permission"


class WorkspaceViolation(ToolPermissionError):
    """试图访问工作目录之外（沙箱越界）的路径。"""

    kind = "tool.workspace"
