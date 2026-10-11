"""CodeAgent —— 一个可读、可测、零第三方依赖的代码助手 Agent。

包内模块职责：

- :mod:`codeagent.config` —— 配置分层加载
- :mod:`codeagent.llm` —— OpenAI 兼容客户端
- :mod:`codeagent.prompts` —— Prompt 设计
- :mod:`codeagent.memory` —— 上下文记忆与裁剪
- :mod:`codeagent.tools` —— 工具抽象、注册表与内置工具
- :mod:`codeagent.agent` —— ReAct 主循环
- :mod:`codeagent.cli` —— 命令行交互层

为保持导入轻量且避免循环依赖，本模块**不做任何再导出**。
按需从具体子模块导入即可，例如：

    from codeagent.config import Settings
    from codeagent.llm import LLMClient
    from codeagent.agent import CodeAgent
    from codeagent.tools import ToolContext, build_default_registry
"""

from __future__ import annotations

__version__ = "1.0.0"
__all__ = ["__version__"]
