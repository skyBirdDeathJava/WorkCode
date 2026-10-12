"""读类工具（默认 allow）。"""

from __future__ import annotations

from pathlib import Path

# 搜索时跳过的目录
SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "dist", "out", "build",
    ".venv", "venv", "__pycache__", ".idea", ".next", ".cache", ".pytest_cache",
    ".workcode", "target",
}

# 按扩展名视为二进制、不读内容
BINARY_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg", ".pdf", ".zip", ".gz",
    ".tar", ".exe", ".dll", ".so", ".dylib", ".woff", ".woff2", ".ttf", ".class",
    ".jar", ".pyc", ".o", ".a", ".wasm", ".mp4", ".mp3", ".webm",
}


def read_file(path: str, offset: int = 0, limit: int | None = None) -> tuple[bool, str]:
    """读取文件文本，按行返回（带行号）。"""
    p = Path(path)
    if not p.is_file():
        return (False, f"文件不存在: {path}")
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return (False, f"读取失败: {e}")

    lines = text.splitlines()
    start = max(offset, 0)
    end = len(lines) if limit is None else min(start + limit, len(lines))
    if start > end:
        return (False, f"偏移 {offset} 超出文件总行数 {len(lines)}")

    width = len(str(end))
    body = "\n".join(f"{i + 1:>{width}}| {line}" for i, line in enumerate(lines[start:end], start=start))
    meta = f"{p}（{len(lines)} 行，显示 {start + 1}-{end} 行）"
    return (True, f"{meta}\n{body}")


def list_directory(path: str = ".") -> tuple[bool, str]:
    """列出目录下的条目。"""
    p = Path(path)
    if not p.is_dir():
        return (False, f"目录不存在: {path}")
    try:
        entries = sorted(p.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
    except OSError as e:
        return (False, f"读取目录失败: {e}")

    lines = []
    for e in entries:
        try:
            suffix = "/" if e.is_dir() else ""
            lines.append(f"{e.name}{suffix}")
        except OSError:
            lines.append(f"{e.name}?")
    if not lines:
        return (True, f"{p}（空目录）")
    return (True, f"{p}\n" + "\n".join(lines))


def search_files(query: str, path: str = ".") -> tuple[bool, str]:
    """按文件名或文件内容搜索（大小写不敏感），最多 100 条结果。"""
    q = query.lower()
    root = Path(path)
    if not root.exists():
        return (False, f"路径不存在: {path}")

    results: list[str] = []
    visited = 0
    max_visited = 3000

    def walk(d: Path) -> None:
        nonlocal visited
        try:
            children = sorted(d.iterdir(), key=lambda e: e.name.lower())
        except OSError:
            return
        for e in children:
            if visited >= max_visited:
                return
            visited += 1
            if e.is_dir():
                if e.name in SKIP_DIRS:
                    continue
                walk(e)
            elif e.is_file():
                if q in e.name.lower():
                    results.append(f"{e}（文件名匹配）")
                    if len(results) >= 100:
                        return
                elif e.suffix.lower() not in BINARY_EXTS and e.stat().st_size <= 512 * 1024:
                    try:
                        content = e.read_text(encoding="utf-8", errors="replace")
                    except Exception:
                        continue
                    if q in content.lower():
                        lines = content.splitlines()
                        for i, line in enumerate(lines[:200]):
                            if q in line.lower():
                                results.append(f"{e}:{i + 1}: {line.strip()[:200]}")
                                if len(results) >= 100:
                                    return
                                break
                if len(results) >= 100:
                    return

    walk(root)
    if not results:
        return (True, f"未找到匹配 '{query}'（已扫描 {visited} 个条目）")
    return (True, f"匹配 '{query}'（{len(results)} 条）：\n" + "\n".join(results))
