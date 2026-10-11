"""工具的抽象基类、统一返回结构与沙箱上下文。

设计要点：

- **统一返回结构** :class:`ToolResult` —— 工具永远不抛异常给 Agent 循环，
  失败也返回一个 ``ok=False`` 的结果，由循环回灌给模型自纠错。
- **沙箱上下文** :class:`ToolContext` —— 集中实现路径越界校验（jail），
  所有文件类工具都通过它解析路径，避免各工具各写一份校验逻辑。
- **参数自校验** :meth:`Tool.execute` —— 在调用 ``run`` 之前统一做
  「必填项 / 未知字段 / 类型强转」检查，把模型常见的参数错误变成清晰的中文报错。
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from ..errors import (
    ToolArgumentError,
    ToolError,
    ToolExecutionError,
    WorkspaceViolation,
)

_JSON_TYPE_MAP: dict[str, type | tuple[type, ...]] = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "array": list,
    "object": dict,
}


# --------------------------------------------------------------------------- #
# 沙箱上下文
# --------------------------------------------------------------------------- #
@dataclass
class ToolContext:
    """一次会话中所有工具共享的运行环境与安全策略。"""

    workspace: Path
    allow_write: bool = True
    max_output: int = 12_000
    command_timeout: float = 60.0

    def __post_init__(self) -> None:
        self.workspace = Path(self.workspace).expanduser().resolve()

    # -- 路径沙箱 ------------------------------------------------------ #
    def resolve(self, raw: str | Path | None, *, purpose: str = "访问") -> Path:
        """把用户/模型给出的路径解析成绝对路径，并确保它位于工作目录内。

        Raises:
            WorkspaceViolation: 路径逃逸出工作目录。
        """
        text = "" if raw is None else str(raw).strip()
        if not text or text in {".", "./", ".\\"}:
            candidate = self.workspace
        else:
            path = Path(text).expanduser()
            candidate = path if path.is_absolute() else self.workspace / path

        try:
            resolved = candidate.resolve()
        except OSError as exc:  # 例如路径过长 / 权限异常
            raise ToolExecutionError(f"无法解析路径 {text!r}：{exc}") from exc

        if not self._is_inside(resolved):
            raise WorkspaceViolation(
                f"拒绝{purpose}工作目录之外的路径：{text!r}\n"
                f"  解析结果：{resolved}\n"
                f"  允许的根目录：{self.workspace}\n"
                f"  如确需访问，请把文件复制到工作目录内，或调整会话的工作目录。"
            )
        return resolved

    def _is_inside(self, path: Path) -> bool:
        if path == self.workspace:
            return True
        try:
            path.relative_to(self.workspace)
            return True
        except ValueError:
            return False

    def require_write(self, action: str) -> None:
        """在只读模式下拦截写操作。"""
        if not self.allow_write:
            from ..errors import ToolPermissionError

            raise ToolPermissionError(
                f"当前会话为只读模式（--read-only），不允许{action}。"
                f"如需修改文件，请去掉 --read-only 重新启动。"
            )

    # -- 展示辅助 ------------------------------------------------------ #
    def display(self, path: Path) -> str:
        """转成相对工作目录的展示路径，让日志更短。"""
        try:
            rel = path.relative_to(self.workspace)
            return "." if str(rel) == "." else rel.as_posix()
        except ValueError:
            return str(path)


# --------------------------------------------------------------------------- #
# 统一返回结构
# --------------------------------------------------------------------------- #
@dataclass
class ToolResult:
    """工具执行的统一返回值。"""

    ok: bool
    content: str
    error_kind: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    # -- 构造快捷方式 -------------------------------------------------- #
    @classmethod
    def success(cls, content: str, **meta: Any) -> "ToolResult":
        return cls(ok=True, content=content, meta=meta)

    @classmethod
    def failure(cls, content: str, kind: str = "tool.execution", **meta: Any) -> "ToolResult":
        return cls(ok=False, content=content, error_kind=kind, meta=meta)

    @classmethod
    def from_exception(cls, exc: Exception) -> "ToolResult":
        kind = getattr(exc, "kind", "tool.execution")
        return cls(ok=False, content=f"{type(exc).__name__}: {exc}", error_kind=kind)

    # -- 序列化 -------------------------------------------------------- #
    def render(self, max_chars: int = 12_000) -> str:
        """转成回灌给模型的文本，超长时保留首尾并给出省略说明。"""
        body = self.content if self.content.strip() else "(工具未返回任何内容)"
        return truncate(body, max_chars)


def truncate(text: str, limit: int, *, head_ratio: float = 0.7) -> str:
    """超出 ``limit`` 时保留头部与尾部，中间用省略标记替代。

    保留尾部很重要：编译错误、异常堆栈、``tail`` 类输出往往在末尾。
    省略标记里的数字使用**定宽**格式，保证结果长度可预测且严格不超过 ``limit``。
    """
    if limit <= 0 or len(text) <= limit:
        return text

    # 数字定宽到 12 位，使 reserve 与最终长度都确定。
    template = "\n... [中间省略 {n:>12} 个字符，可用更精确的参数缩小输出范围] ...\n"
    reserve = len(template.format(n=0))
    if limit <= reserve + 40:
        return text[:limit]

    budget = limit - reserve
    head_len = max(1, int(budget * head_ratio))
    tail_len = max(1, budget - head_len)
    omitted = max(0, len(text) - head_len - tail_len)
    return text[:head_len] + template.format(n=omitted) + text[-tail_len:]


# --------------------------------------------------------------------------- #
# 工具基类
# --------------------------------------------------------------------------- #
class Tool(ABC):
    """所有工具的基类。

    子类需要声明四个类属性并实现 :meth:`run`：

    - ``name``：模型调用时使用的名字（建议 ``snake_case``）。
    - ``description``：给模型看的能力说明，写得越清楚模型选得越准。
    - ``parameters``：JSON Schema 的 ``properties`` 部分。
    - ``required``：必填参数名列表。
    - ``requires_write``：是否需要写权限（只读模式下会被直接拒绝）。
    """

    name: str = ""
    description: str = ""
    parameters: dict[str, Any] = {}
    required: tuple[str, ...] = ()
    requires_write: bool = False

    #: 该工具是否可能修改磁盘/系统状态（用于 CLI 提示与统计）。
    mutating: bool = False

    def __init__(self, context: ToolContext) -> None:
        self.context = context

    # ------------------------------------------------------------------ #
    # 协议适配
    # ------------------------------------------------------------------ #
    def schema(self) -> dict[str, Any]:
        """导出 OpenAI 函数调用格式的工具描述。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": self.parameters or {},
                    "required": list(self.required),
                },
            },
        }

    # ------------------------------------------------------------------ #
    # 执行入口
    # ------------------------------------------------------------------ #
    def execute(self, arguments: Mapping[str, Any] | None) -> ToolResult:
        """校验参数后执行；**任何异常都会被转成失败结果**，不会向上抛出。"""
        try:
            if self.requires_write:
                self.context.require_write(f"调用工具 {self.name}")
            kwargs = self.validate(arguments or {})
            result = self.run(**kwargs)
            if not isinstance(result, ToolResult):
                return ToolResult.success(str(result))
            return result
        except ToolError as exc:
            return ToolResult.from_exception(exc)
        except Exception as exc:  # 兜底：工具内部未预料的 bug 也不应炸掉整个 Agent
            return ToolResult.failure(
                f"工具 {self.name} 内部异常：{type(exc).__name__}: {exc}",
                kind="tool.internal",
            )

    # ------------------------------------------------------------------ #
    # 参数校验
    # ------------------------------------------------------------------ #
    def validate(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """校验并规范化参数，返回可直接解包给 :meth:`run` 的字典。"""
        if not isinstance(arguments, Mapping):
            raise ToolArgumentError(
                f"{self.name} 的参数应为 JSON 对象，实际收到 {type(arguments).__name__}"
            )

        props: dict[str, Any] = self.parameters or {}
        unknown = [k for k in arguments if k not in props]
        if unknown:
            raise ToolArgumentError(
                f"{self.name} 收到未知参数 {unknown}；"
                f"可接受参数为 {sorted(props) or '（无）'}"
            )

        # 只把「键缺失」或「显式为 null」视为缺参。
        # 注意不能把空字符串也算作缺参：edit_file 的 new_string="" 是文档承诺的
        # "删除该片段"用法，write_file 的 content="" 是"创建空文件"，都合法。
        # 各工具若确实不允许空串，由 run() 自己给出更贴切的报错。
        missing = [
            name
            for name in self.required
            if name not in arguments or arguments.get(name) is None
        ]
        if missing:
            raise ToolArgumentError(
                f"{self.name} 缺少必填参数 {missing}；"
                f"参数说明：{json.dumps(props, ensure_ascii=False)}"
            )

        cleaned: dict[str, Any] = {}
        for key, spec in props.items():
            if key not in arguments or arguments[key] is None:
                continue
            cleaned[key] = _coerce_value(self.name, key, arguments[key], spec)
        return cleaned

    # ------------------------------------------------------------------ #
    @abstractmethod
    def run(self, **kwargs: Any) -> ToolResult:
        """执行工具逻辑（参数已完成校验）。"""
        raise NotImplementedError

    # ------------------------------------------------------------------ #
    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<{type(self).__name__} name={self.name!r}>"


def _coerce_value(tool_name: str, key: str, value: Any, spec: Mapping[str, Any]) -> Any:
    """按 JSON Schema 做轻量类型强转。

    模型经常把 ``"10"`` 当整数传，或把布尔值传成 ``"true"``；
    这里做宽容强转，但遇到无法转换的值仍报明确的错。
    """
    expected = spec.get("type")
    if expected == "array":
        if isinstance(value, list):
            return value
        if isinstance(value, str) and value.strip():
            return [item.strip() for item in value.split(",") if item.strip()]
        raise ToolArgumentError(f"{tool_name}.{key} 应为数组，实际收到 {value!r}")

    if expected == "string":
        if isinstance(value, bool):
            return "true" if value else "false"
        return value if isinstance(value, str) else str(value)

    if expected == "boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            low = value.strip().lower()
            if low in {"true", "1", "yes", "y", "on"}:
                return True
            if low in {"false", "0", "no", "n", "off"}:
                return False
        if isinstance(value, (int, float)) and value in (0, 1):
            return bool(value)
        raise ToolArgumentError(f"{tool_name}.{key} 应为布尔值，实际收到 {value!r}")

    if expected in {"integer", "number"}:
        if isinstance(value, bool):
            raise ToolArgumentError(f"{tool_name}.{key} 应为数值，实际收到布尔值")
        if isinstance(value, (int, float)):
            return int(value) if expected == "integer" else float(value)
        if isinstance(value, str):
            try:
                return int(value.strip()) if expected == "integer" else float(value.strip())
            except ValueError as exc:
                raise ToolArgumentError(
                    f"{tool_name}.{key} 应为数值，无法解析 {value!r}"
                ) from exc
        raise ToolArgumentError(f"{tool_name}.{key} 应为数值，实际收到 {value!r}")

    if expected == "object" and not isinstance(value, dict):
        raise ToolArgumentError(f"{tool_name}.{key} 应为对象，实际收到 {value!r}")

    return value


def schema_properties(
    *,
    description: str,
    type: str = "string",
    enum: Iterable[Any] | None = None,
    default: Any = None,
    **extra: Any,
) -> dict[str, Any]:
    """构造单个参数的 JSON Schema 片段的小工具函数。"""
    spec: dict[str, Any] = {"type": type, "description": description}
    if enum is not None:
        spec["enum"] = list(enum)
    if default is not None:
        spec["default"] = default
    spec.update(extra)
    return spec


__all__ = [
    "Tool",
    "ToolContext",
    "ToolResult",
    "truncate",
    "schema_properties",
]
