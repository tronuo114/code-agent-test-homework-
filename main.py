#!/usr/bin/env python3
"""CodeReview —— 代码错误审查 Agent 的统一入口。

默认进入**代码审查模式**：

    python main.py                    交互式审查会话
    python main.py help               打印指令说明
    python main.py api sk-xxxx        绑定并校验 API Key
    python main.py review src         审查 src 目录
    python main.py --mock             离线演示（无需密钥）

原有"完整助手模式"（可写文件、执行命令）仍然保留，但不作为默认入口：

    python main.py assistant          进入完整助手模式
    python main.py assistant --help   查看该模式的参数

之所以保留它，是因为审查模式的工具集是刻意收窄的只读子集；
需要"改代码"时仍然有一个入口，不必去改源码。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 允许从任意目录以 `python <路径>\\main.py` 的方式运行。
sys.path.insert(0, str(Path(__file__).resolve().parent))


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    if argv and argv[0].lower() in {"assistant", "full", "agent"}:
        from codeagent.cli import main as assistant_main

        return assistant_main(argv[1:])

    from codeagent.review_cli import main as review_main

    return review_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
