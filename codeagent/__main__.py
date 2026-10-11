"""支持 ``python -m codeagent`` 方式运行。"""

from __future__ import annotations

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
