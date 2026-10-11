"""文件系统类工具：列目录 / 读文件 / 写文件 / 精确编辑 / 正则检索。

这些工具是代码助手的"眼睛和手"。实现上特别处理了几件在真实使用中
最容易翻车的小事：

- **编码**：依次尝试 UTF-8 / UTF-8-BOM / GBK，最后才用替换字符兜底，
  避免中文注释文件读出来是乱码（Windows 环境尤其常见）。
- **换行符**：读入时统一成 ``\\n`` 方便模型匹配，写回时恢复原文件的
  CRLF/LF 风格，做到"只改该改的地方"。
- **二进制**：拒绝读写二进制文件，防止把 prompt 塞爆。
- **噪声目录**：递归时跳过 ``.git`` / ``node_modules`` / ``__pycache__`` 等。
"""

from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path
from typing import Any, Iterator

from ..errors import ToolExecutionError
from .base import Tool, ToolContext, ToolResult, schema_properties, truncate

#: 递归遍历时跳过的目录名（避免把 token 浪费在依赖与缓存上）。
SKIP_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "__pycache__",
        "node_modules",
        ".venv",
        "venv",
        "env",
        ".env",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        "dist",
        "build",
        ".next",
        ".nuxt",
        ".idea",
        ".vscode",
        ".cache",
        "target",
        "bin",
        "obj",
    }
)

#: 判定为二进制文件的扩展名。
BINARY_SUFFIXES = frozenset(
    {
        ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".svgz",
        ".pdf", ".zip", ".gz", ".tar", ".7z", ".rar", ".jar", ".war",
        ".exe", ".dll", ".so", ".dylib", ".pyd", ".pyc", ".pyo", ".class",
        ".mp3", ".mp4", ".avi", ".mov", ".wav", ".flac",
        ".woff", ".woff2", ".ttf", ".otf", ".eot",
        ".db", ".sqlite", ".sqlite3", ".bin", ".dat", ".iso", ".img",
    }
)

#: 单文件读取上限（超过则要求用行号范围分段读）。
MAX_READ_BYTES = 2 * 1024 * 1024

#: 超过该长度时截断单行显示。
MAX_LINE_WIDTH = 600


# --------------------------------------------------------------------------- #
# 编码 / 换行 处理
# --------------------------------------------------------------------------- #
def _decode_bytes(data: bytes) -> tuple[str, str]:
    """按常见编码依次尝试解码，返回 (文本, 实际使用的编码名)。"""
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig", errors="replace"), "utf-8-sig"
    for encoding in ("utf-8", "gbk", "big5", "latin-1"):
        try:
            return data.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace"), "utf-8(replace)"


def _detect_newline(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _looks_binary(data: bytes) -> bool:
    return b"\x00" in data[:4096]


def count_lines(text: str) -> int:
    """统计文本的"逻辑行数"。

    朴素写法 ``len(text.split("\\n"))`` 会把结尾换行产生的空串也算成一行，
    导致 ``"a\\nb\\n"`` 被报告为 3 行。这里按编辑器的习惯处理：
    结尾换行只是行终止符，不额外产生一行。
    """
    if not text:
        return 0
    return max(1, len(text.split("\n")) - (1 if text.endswith("\n") else 0))


def read_text_file(path: Path) -> tuple[str, str, str]:
    """读取文本文件，返回 (已归一化换行的文本, 编码, 原始换行符)。"""
    raw = path.read_bytes()
    if _looks_binary(raw):
        raise ToolExecutionError(
            f"{path.name} 看起来是二进制文件，read_file 只支持文本文件。"
        )
    text, encoding = _decode_bytes(raw)
    newline = _detect_newline(text)
    return _normalize_newlines(text), encoding, newline


def write_text_file(path: Path, text: str, newline: str = "\n") -> int:
    """按指定换行风格写入文本，返回写入的字节数。"""
    normalized = _normalize_newlines(text)
    payload = normalized.replace("\n", newline) if newline != "\n" else normalized
    data = payload.encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return len(data)


def iter_files(
    root: Path,
    *,
    glob: str | None = None,
    ignore_hidden: bool = True,
    max_files: int = 5000,
) -> Iterator[Path]:
    """递归遍历 ``root`` 下的文件，跳过噪声目录。"""
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d
            for d in dirnames
            if d not in SKIP_DIRS and not (ignore_hidden and d.startswith("."))
        )
        for filename in sorted(filenames):
            if ignore_hidden and filename.startswith("."):
                continue
            candidate = Path(dirpath) / filename
            if glob and not (
                fnmatch.fnmatch(filename, glob)
                or fnmatch.fnmatch(candidate.as_posix(), glob)
            ):
                continue
            yield candidate
            count += 1
            if count >= max_files:
                return


# --------------------------------------------------------------------------- #
# list_dir
# --------------------------------------------------------------------------- #
class ListDirTool(Tool):
    name = "list_dir"
    description = (
        "列出目录内容，用于了解项目结构。默认只列一层；recursive=true 时递归展开"
        "（会自动跳过 .git / node_modules / __pycache__ 等噪声目录）。"
    )
    parameters = {
        "path": schema_properties(description="要列出的目录，默认当前工作目录"),
        "recursive": schema_properties(
            description="是否递归展开子目录，默认 false", type="boolean"
        ),
        "max_depth": schema_properties(
            description="递归时的最大深度，默认 3", type="integer"
        ),
        "max_entries": schema_properties(
            description="最多返回多少条目，默认 200", type="integer"
        ),
        "include_hidden": schema_properties(
            description="是否包含以 . 开头的隐藏文件，默认 false", type="boolean"
        ),
    }
    required = ()

    def run(self, **kwargs: Any) -> ToolResult:
        raw_path = kwargs.get("path")
        target = self.context.resolve(raw_path, purpose="列出")
        recursive = bool(kwargs.get("recursive", False))
        max_depth = int(kwargs.get("max_depth", 3) or 3)
        max_entries = int(kwargs.get("max_entries", 200) or 200)
        include_hidden = bool(kwargs.get("include_hidden", False))

        if not target.exists():
            raise ToolExecutionError(
                f"目录不存在：{self.context.display(target)}"
            )
        if not target.is_dir():
            raise ToolExecutionError(
                f"{self.context.display(target)} 不是目录，请改用 read_file"
            )

        lines: list[str] = []
        truncated = False

        def emit(rel: str, is_dir: bool, size: int | None) -> None:
            nonlocal truncated
            if len(lines) >= max_entries:
                truncated = True
                return
            if is_dir:
                lines.append(f"{rel}/")
            else:
                lines.append(f"{rel}  ({_human_size(size or 0)})")

        counters = {"dirs": 0, "files": 0}

        def walk(directory: Path, prefix: str, depth: int) -> None:
            if depth > max_depth:
                return
            try:
                entries = sorted(
                    directory.iterdir(), key=lambda p: (p.is_file(), p.name.lower())
                )
            except OSError as exc:
                lines.append(f"{prefix}<无法读取：{exc}>")
                return
            for entry in entries:
                name = entry.name
                if not include_hidden and name.startswith("."):
                    continue
                if entry.is_dir() and name in SKIP_DIRS:
                    counters["dirs"] += 1
                    emit(f"{prefix}{name} (已跳过内部)", True, None)
                    continue
                if entry.is_dir():
                    counters["dirs"] += 1
                    emit(f"{prefix}{name}", True, None)
                    if recursive:
                        walk(entry, f"{prefix}{name}/", depth + 1)
                else:
                    counters["files"] += 1
                    try:
                        size = entry.stat().st_size
                    except OSError:
                        size = 0
                    emit(f"{prefix}{name}", False, size)

        display_root = self.context.display(target)
        walk(target, "", 1)

        header = (
            f"目录 {display_root} 的内容"
            f"（{counters['dirs']} 个子目录, {counters['files']} 个文件）:"
        )
        body = "\n".join(lines) if lines else "  (空目录)"
        if truncated:
            body += f"\n... 已达 {max_entries} 条上限，请用更具体的 path 或调小 max_depth ..."
        return ToolResult.success(f"{header}\n{body}", path=display_root, count=len(lines))


# --------------------------------------------------------------------------- #
# read_file
# --------------------------------------------------------------------------- #
class ReadFileTool(Tool):
    name = "read_file"
    description = (
        "读取文本文件内容，返回带行号的正文。支持用 start_line/end_line 只读片段，"
        "大文件请务必分段读取。"
    )
    parameters = {
        "path": schema_properties(description="文件路径（相对工作目录或其中的绝对路径）"),
        "start_line": schema_properties(description="起始行号，从 1 开始，默认 1", type="integer"),
        "end_line": schema_properties(description="结束行号（含），默认读到文件末尾", type="integer"),
        "max_lines": schema_properties(
            description="最多返回多少行，默认 400；超出会提示继续读取的起始行", type="integer"
        ),
    }
    required = ("path",)

    def run(self, **kwargs: Any) -> ToolResult:
        target = self.context.resolve(kwargs["path"], purpose="读取")
        if not target.exists():
            parent = target.parent
            hint = ""
            if parent.is_dir():
                siblings = [p.name for p in sorted(parent.iterdir())[:20]]
                hint = f"\n该目录下现有条目：{siblings}"
            raise ToolExecutionError(
                f"文件不存在：{self.context.display(target)}{hint}"
            )
        if target.is_dir():
            raise ToolExecutionError(
                f"{self.context.display(target)} 是目录，请改用 list_dir"
            )
        if target.suffix.lower() in BINARY_SUFFIXES:
            raise ToolExecutionError(
                f"{target.name} 是二进制文件（{target.suffix}），read_file 无法读取。"
            )

        size = target.stat().st_size
        if size > MAX_READ_BYTES:
            raise ToolExecutionError(
                f"文件过大（{_human_size(size)}），超过 {_human_size(MAX_READ_BYTES)} 上限。"
                f"请用 start_line/end_line 分段读取。"
            )

        text, encoding, _ = read_text_file(target)
        all_lines = text.split("\n")
        total = count_lines(text)

        start = max(1, int(kwargs.get("start_line", 1) or 1))
        end = int(kwargs.get("end_line", total) or total)
        max_lines = int(kwargs.get("max_lines", 400) or 400)
        end = min(end, total)
        if start > total:
            raise ToolExecutionError(
                f"start_line={start} 超出文件总行数（{total} 行）"
            )
        if end < start:
            raise ToolExecutionError(f"end_line={end} 小于 start_line={start}")

        clipped = False
        if end - start + 1 > max_lines:
            end = start + max_lines - 1
            clipped = True

        width = len(str(end))
        body_lines = []
        for number in range(start, end + 1):
            content = all_lines[number - 1]
            if len(content) > MAX_LINE_WIDTH:
                content = content[:MAX_LINE_WIDTH] + f" …[本行共 {len(content)} 字符]"
            body_lines.append(f"{number:>{width}} | {content}")

        rel = self.context.display(target)
        header = (
            f"文件 {rel}（共 {total} 行, {_human_size(size)}, {encoding}）"
            f"，以下为第 {start}-{end} 行："
        )
        trailer = ""
        if clipped:
            trailer = f"\n... 本次截断到 {max_lines} 行，可继续读取 start_line={end + 1} ..."
        elif end < total:
            trailer = f"\n... 文件还有 {total - end} 行未显示 ..."

        return ToolResult.success(
            f"{header}\n" + "\n".join(body_lines) + trailer,
            path=rel,
            total_lines=total,
            start=start,
            end=end,
        )


# --------------------------------------------------------------------------- #
# write_file
# --------------------------------------------------------------------------- #
class WriteFileTool(Tool):
    name = "write_file"
    description = (
        "创建或整体覆盖一个文本文件（会自动创建父目录）。"
        "修改已有文件时请优先使用 edit_file，只有新建文件或整体重写时才用本工具。"
    )
    parameters = {
        "path": schema_properties(description="目标文件路径"),
        "content": schema_properties(description="要写入的完整文本内容"),
        "overwrite": schema_properties(
            description="目标已存在时是否覆盖，默认 true；设为 false 可避免误覆盖",
            type="boolean",
        ),
    }
    required = ("path", "content")
    requires_write = True
    mutating = True

    def run(self, **kwargs: Any) -> ToolResult:
        target = self.context.resolve(kwargs["path"], purpose="写入")
        content: str = kwargs["content"]
        overwrite = bool(kwargs.get("overwrite", True))

        existed = target.exists()
        if existed and target.is_dir():
            raise ToolExecutionError(
                f"{self.context.display(target)} 是一个目录，无法写入文件"
            )
        if existed and not overwrite:
            raise ToolExecutionError(
                f"文件已存在且 overwrite=false，已拒绝覆盖：{self.context.display(target)}"
            )

        old_text = ""
        newline = "\n"
        if existed:
            try:
                old_text, _, newline = read_text_file(target)
            except ToolExecutionError:
                old_text = ""

        written = write_text_file(target, content, newline)
        new_lines = count_lines(content)
        old_lines = count_lines(old_text)

        if existed:
            summary = (
                f"已更新 {self.context.display(target)}"
                f"（{old_lines} 行 → {new_lines} 行, 写入 {_human_size(written)}）"
            )
        else:
            summary = (
                f"已创建 {self.context.display(target)}"
                f"（{new_lines} 行, {_human_size(written)}）"
            )
        return ToolResult.success(
            summary, path=self.context.display(target), bytes=written, lines=new_lines
        )


# --------------------------------------------------------------------------- #
# edit_file
# --------------------------------------------------------------------------- #
class EditFileTool(Tool):
    name = "edit_file"
    description = (
        "在文件中做精确字符串替换（推荐用于代码修改）。old_string 必须与文件内容"
        "完全一致，默认要求它在文件中唯一出现，否则会报错并提示出现次数。"
    )
    parameters = {
        "path": schema_properties(description="要修改的文件路径"),
        "old_string": schema_properties(description="要被替换的原文（需与文件内容完全一致，含缩进）"),
        "new_string": schema_properties(description="替换成的新内容；传空字符串表示删除该片段"),
        "replace_all": schema_properties(
            description="是否替换所有出现的位置，默认 false（要求唯一匹配）",
            type="boolean",
        ),
    }
    required = ("path", "old_string", "new_string")
    requires_write = True
    mutating = True

    def run(self, **kwargs: Any) -> ToolResult:
        target = self.context.resolve(kwargs["path"], purpose="修改")
        if not target.exists():
            raise ToolExecutionError(
                f"文件不存在：{self.context.display(target)}。"
                f"新建文件请使用 write_file。"
            )
        if target.is_dir():
            raise ToolExecutionError(f"{self.context.display(target)} 是目录")

        old_string: str = kwargs["old_string"]
        new_string: str = kwargs["new_string"]
        replace_all = bool(kwargs.get("replace_all", False))

        if old_string == "":
            raise ToolExecutionError(
                "old_string 不能为空。若要整体重写文件，请使用 write_file。"
            )

        text, encoding, newline = read_text_file(target)
        occurrences = text.count(old_string)

        if occurrences == 0:
            raise ToolExecutionError(
                f"在 {self.context.display(target)} 中找不到 old_string。\n"
                f"常见原因：缩进/空格不一致、换行符差异、或文件内容与预期不同。\n"
                f"请先用 read_file 确认原文。\n"
                f"你提供的 old_string 前 200 字符：\n{old_string[:200]!r}"
            )
        if occurrences > 1 and not replace_all:
            positions = [
                _line_of(text, index) for index in _find_all(text, old_string)[:10]
            ]
            raise ToolExecutionError(
                f"old_string 在文件中出现了 {occurrences} 次（行号：{positions}），"
                f"无法确定要改哪一处。请提供更长的上下文使其唯一，"
                f"或显式设置 replace_all=true 来全部替换。"
            )

        updated = text.replace(old_string, new_string) if replace_all else text.replace(
            old_string, new_string, 1
        )
        written = write_text_file(target, updated, newline)

        replaced = occurrences if replace_all else 1
        delta = updated.count("\n") - text.count("\n")
        return ToolResult.success(
            f"已修改 {self.context.display(target)}：替换 {replaced} 处，"
            f"行数变化 {delta:+d}，写入 {_human_size(written)}（编码 {encoding}）",
            path=self.context.display(target),
            replaced=replaced,
        )


# --------------------------------------------------------------------------- #
# search_code
# --------------------------------------------------------------------------- #
class SearchCodeTool(Tool):
    name = "search_code"
    description = (
        "用正则表达式在工作目录内检索文本，返回 文件:行号: 内容。"
        "适合定位函数定义、变量引用、TODO 等；可配合 glob 限定文件类型。"
    )
    parameters = {
        "pattern": schema_properties(description="Python 正则表达式，例如 'def\\s+train'"),
        "path": schema_properties(description="检索根目录，默认当前工作目录"),
        "glob": schema_properties(description="只检索匹配的文件名，例如 '*.py' 或 '*.ts'"),
        "ignore_case": schema_properties(description="是否忽略大小写，默认 false", type="boolean"),
        "max_results": schema_properties(description="最多返回多少条匹配，默认 50", type="integer"),
        "context_lines": schema_properties(
            description="每条匹配额外附带的上下文行数，默认 0", type="integer"
        ),
    }
    required = ("pattern",)

    def run(self, **kwargs: Any) -> ToolResult:
        root = self.context.resolve(kwargs.get("path"), purpose="检索")
        if not root.exists():
            raise ToolExecutionError(f"路径不存在：{self.context.display(root)}")
        if root.is_file():
            files: list[Path] = [root]
        else:
            files = list(iter_files(root, glob=kwargs.get("glob")))

        flags = re.IGNORECASE if kwargs.get("ignore_case") else 0
        if not str(kwargs["pattern"]).strip():
            raise ToolExecutionError(
                "pattern 不能为空字符串；空正则会匹配每一行。"
                "请给出具体的关键字或正则表达式。"
            )
        try:
            regex = re.compile(kwargs["pattern"], flags)
        except re.error as exc:
            raise ToolExecutionError(
                f"正则表达式无法编译：{exc}\n你提供的 pattern：{kwargs['pattern']!r}"
            ) from exc

        max_results = int(kwargs.get("max_results", 50) or 50)
        context_lines = max(0, int(kwargs.get("context_lines", 0) or 0))

        matches: list[str] = []
        hit_files: set[str] = set()
        scanned = 0
        limit_hit = False

        for file_path in files:
            if limit_hit:
                break
            if file_path.suffix.lower() in BINARY_SUFFIXES:
                continue
            try:
                if file_path.stat().st_size > MAX_READ_BYTES:
                    continue
                data = file_path.read_bytes()
            except OSError:
                continue
            if _looks_binary(data):
                continue

            text, _ = _decode_bytes(data)
            text = _normalize_newlines(text)
            scanned += 1
            lines = text.split("\n")
            rel = self.context.display(file_path)

            for index, line in enumerate(lines):
                if not regex.search(line):
                    continue
                hit_files.add(rel)
                snippet = line.strip()
                if len(snippet) > 300:
                    snippet = snippet[:300] + " …"
                block = [f"{rel}:{index + 1}: {snippet}"]
                for offset in range(1, context_lines + 1):
                    if index + offset < len(lines):
                        block.append(f"    {index + offset + 1} | {lines[index + offset].rstrip()}")
                matches.append("\n".join(block))
                if len(matches) >= max_results:
                    limit_hit = True
                    break

        header = (
            f"在 {self.context.display(root)} 检索 {kwargs['pattern']!r}"
            f"（{scanned} 个文件, {len(hit_files)} 个命中文件）："
        )
        if not matches:
            return ToolResult.success(
                f"{header}\n(无匹配结果)\n"
                f"提示：可尝试放宽 pattern、去掉 glob 限制，或换用 list_dir 先确认目录结构。",
                matches=0,
            )
        body = "\n".join(matches)
        if limit_hit:
            body += f"\n... 已达 {max_results} 条上限，请缩小检索范围 ..."
        return ToolResult.success(
            f"{header}\n{body}", matches=len(matches), files=len(hit_files)
        )


# --------------------------------------------------------------------------- #
# 辅助函数
# --------------------------------------------------------------------------- #
def _find_all(text: str, needle: str) -> list[int]:
    positions: list[int] = []
    start = 0
    while True:
        index = text.find(needle, start)
        if index == -1:
            return positions
        positions.append(index)
        start = index + max(1, len(needle))


def _line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def _human_size(size: int) -> str:
    units = ("B", "KB", "MB", "GB")
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


__all__ = [
    "ListDirTool",
    "ReadFileTool",
    "WriteFileTool",
    "EditFileTool",
    "SearchCodeTool",
    "ToolContext",
    "count_lines",
    "iter_files",
    "read_text_file",
    "write_text_file",
    "SKIP_DIRS",
]
