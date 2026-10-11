"""子进程执行封装：超时、进程树清理、编码自适应。

``run_command`` 与 ``run_python`` 都建立在这一个函数之上，避免两处各写一遍
超时与杀进程逻辑（这是最容易出 bug 也最容易泄漏僵尸进程的地方）。

设计取舍：

- **捕获字节而非文本**：Windows 上 ``cmd.exe`` 输出常见 GBK，而现代工具链多为
  UTF-8。统一先收字节，再用与读文件相同的编码嗅探逻辑解码，中文输出不乱码。
- **超时必须杀进程树**：``subprocess.run(timeout=...)`` 只杀直接子进程，
  ``shell=True`` 下真正干活的孙进程会成为孤儿继续跑。这里在超时后
  Windows 用 ``taskkill /T``、POSIX 用 ``killpg`` 清理整个进程组。
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

#: 子进程输出的硬上限（防止 `yes` 之类的命令把内存吃光）。
MAX_CAPTURE_BYTES = 4 * 1024 * 1024


@dataclass
class ProcessResult:
    """一次子进程执行的结果。"""

    command: str
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False
    duration: float = 0.0

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


def decode_output(data: bytes) -> str:
    """按 UTF-8 → GBK → 替换 的顺序解码子进程输出。"""
    if not data:
        return ""
    for encoding in ("utf-8", "gbk", "big5"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def kill_process_tree(process: subprocess.Popen) -> None:
    """尽最大努力结束进程及其所有后代；失败也不抛异常。"""
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                capture_output=True,
                timeout=10,
            )
        else:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except Exception:
        pass
    finally:
        try:
            process.kill()
        except Exception:
            pass


def run_process(
    command: str | Sequence[str],
    *,
    cwd: Path,
    timeout: float = 60.0,
    shell: bool = False,
    env: Mapping[str, str] | None = None,
) -> ProcessResult:
    """执行一个子进程并捕获输出。

    Args:
        command: ``shell=True`` 时为命令字符串，否则为 argv 列表。
        cwd: 工作目录（调用方需保证已在沙箱内）。
        timeout: 秒；超时后杀掉整个进程树。
        shell: 是否通过系统 shell 执行。
        env: 环境变量覆盖项（会与当前环境合并）。
    """
    display = command if isinstance(command, str) else " ".join(command)
    child_env = os.environ.copy()
    # 让被调起的 Python/Node 尽量输出 UTF-8，减少跨平台乱码。
    child_env.setdefault("PYTHONIOENCODING", "utf-8")
    child_env.setdefault("PYTHONUNBUFFERED", "1")
    child_env.setdefault("NO_COLOR", "1")
    if env:
        child_env.update({k: str(v) for k, v in env.items()})

    started = time.monotonic()
    try:
        process = subprocess.Popen(
            command,
            cwd=str(cwd),
            shell=shell,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=child_env,
            start_new_session=(os.name != "nt"),
        )
    except FileNotFoundError as exc:
        return ProcessResult(
            command=display,
            returncode=127,
            stdout="",
            stderr=f"找不到可执行文件：{exc}",
            duration=time.monotonic() - started,
        )
    except OSError as exc:
        return ProcessResult(
            command=display,
            returncode=126,
            stdout="",
            stderr=f"无法启动进程：{exc}",
            duration=time.monotonic() - started,
        )

    timed_out = False
    try:
        stdout_bytes, stderr_bytes = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        kill_process_tree(process)
        try:
            stdout_bytes, stderr_bytes = process.communicate(timeout=10)
        except Exception:
            stdout_bytes, stderr_bytes = b"", b""

    duration = time.monotonic() - started
    if len(stdout_bytes) > MAX_CAPTURE_BYTES:
        stdout_bytes = stdout_bytes[:MAX_CAPTURE_BYTES]
    if len(stderr_bytes) > MAX_CAPTURE_BYTES:
        stderr_bytes = stderr_bytes[:MAX_CAPTURE_BYTES]

    return ProcessResult(
        command=display,
        returncode=process.returncode if process.returncode is not None else -1,
        stdout=decode_output(stdout_bytes),
        stderr=decode_output(stderr_bytes),
        timed_out=timed_out,
        duration=duration,
    )


def format_process_result(result: ProcessResult, max_chars: int) -> tuple[bool, str]:
    """把进程结果渲染成给模型看的文本，返回 (是否成功, 文本)。"""
    from .base import truncate

    parts: list[str] = []
    if result.timed_out:
        parts.append(f"[超时] 命令在限定时间内未结束，已被强制终止。")
    parts.append(f"$ {result.command}")
    parts.append(f"退出码: {result.returncode}  (耗时 {result.duration:.2f}s)")

    if result.stdout.strip():
        parts.append("--- stdout ---\n" + result.stdout.rstrip())
    else:
        parts.append("--- stdout ---\n(空)")

    if result.stderr.strip():
        parts.append("--- stderr ---\n" + result.stderr.rstrip())

    if result.returncode != 0 and not result.timed_out:
        parts.append("提示：命令以非零退出码结束，请阅读 stderr 定位原因。")

    return result.ok, truncate("\n".join(parts), max_chars)


def python_executable() -> str:
    """返回当前解释器路径（用于 run_python）。"""
    return sys.executable or "python"


__all__ = [
    "ProcessResult",
    "run_process",
    "kill_process_tree",
    "decode_output",
    "format_process_result",
    "python_executable",
    "MAX_CAPTURE_BYTES",
]
