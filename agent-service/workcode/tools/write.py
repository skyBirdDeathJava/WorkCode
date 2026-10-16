"""写类工具（默认 ask：需用户确认，路径必须在工作区内）。

M4 起采用「暂存 → Diff 预览 → 用户接受 → 落盘」流程：
- preview_* 只计算新内容，不写盘
- 用户接受后才调用 write_file / edit_file 落盘
"""

from __future__ import annotations

from pathlib import Path


def preview_write_file(path: str, content: str) -> tuple[bool, str, str, str]:
    """计算写入后的内容（不落盘）。返回 (ok, original, new, error)。"""
    p = Path(path)
    original = p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""
    return (True, original, content, "")


def preview_edit_file(
    path: str, oldText: str, newText: str, replaceAll: bool = False
) -> tuple[bool, str, str, str]:
    """计算精确替换后的内容（不落盘）。返回 (ok, original, new, error)。"""
    p = Path(path)
    if not p.is_file():
        return (False, "", "", f"文件不存在: {path}")
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return (False, "", "", f"读取失败: {e}")
    if not oldText:
        return (False, "", "", "oldText 不能为空")
    count = text.count(oldText)
    if count == 0:
        return (False, "", "", f"未找到要替换的文本：{oldText[:60]}")
    if count > 1 and not replaceAll:
        return (
            False,
            "",
            "",
            f"oldText 出现 {count} 次，请提供更精确的文本，或设置 replaceAll=true",
        )
    new = text.replace(oldText, newText) if replaceAll else text.replace(oldText, newText, 1)
    return (True, text, new, "")


def preview_apply_patch(
    changes: list[dict],
) -> tuple[bool, list[tuple[str, str, str]], str]:
    """批量精确替换多个文件（不落盘）。返回 (ok, [(path, original, new), ...], error)。"""
    staged: list[tuple[str, str, str]] = []
    for ch in changes:
        path = ch.get("path", "")
        old = ch.get("oldText", "")
        new = ch.get("newText", "")
        p = Path(path)
        if not p.is_file():
            return False, [], f"文件不存在: {path}"
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            return False, [], f"读取失败: {path}: {e}"
        if not old:
            return False, [], f"{path} 的 oldText 不能为空"
        count = text.count(old)
        if count == 0:
            return False, [], f"未找到要替换的文本: {old[:50]}（于 {path}）"
        if count > 1 and not ch.get("replaceAll"):
            return False, [], f"{path} 中 oldText 出现 {count} 次，需更精确或 replaceAll=true"
        new_text = text.replace(old, new) if ch.get("replaceAll") else text.replace(old, new, 1)
        staged.append((path, text, new_text))
    return True, staged, ""


def apply_apply_patch(changes: list[dict]) -> tuple[bool, str]:
    """把批量替换真正落盘（假设已通过 preview 校验）。"""
    for ch in changes:
        path = ch.get("path", "")
        old = ch.get("oldText", "")
        new = ch.get("newText", "")
        p = Path(path)
        if not p.is_file():
            return False, f"文件不存在: {path}"
        text = p.read_text(encoding="utf-8", errors="replace")
        new_text = text.replace(old, new) if ch.get("replaceAll") else text.replace(old, new, 1)
        p.write_text(new_text, encoding="utf-8")
    return True, f"已批量修改 {len(changes)} 个文件"


def write_file(path: str, content: str) -> tuple[bool, str]:
    """写入文件（新建或覆盖）。"""
    p = Path(path)
    existed = p.exists()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    except OSError as e:
        return (False, f"写入失败: {e}")
    return (True, f"{'覆盖' if existed else '新建'} {path}（{len(content)} 字符）")


def edit_file(path: str, oldText: str, newText: str, replaceAll: bool = False) -> tuple[bool, str]:
    """精确替换文件中的文本。默认要求 oldText 只出现一次，避免误替换。"""
    p = Path(path)
    if not p.is_file():
        return (False, f"文件不存在: {path}")
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return (False, f"读取失败: {e}")

    if not oldText:
        return (False, "oldText 不能为空")
    count = text.count(oldText)
    if count == 0:
        return (False, f"未找到要替换的文本：{oldText[:60]}")
    if count > 1 and not replaceAll:
        return (
            False,
            f"oldText 出现 {count} 次，请提供更精确的文本，或设置 replaceAll=true",
        )

    new = text.replace(oldText, newText) if replaceAll else text.replace(oldText, newText, 1)
    try:
        p.write_text(new, encoding="utf-8")
    except OSError as e:
        return (False, f"写入失败: {e}")
    return (True, f"已修改 {path}（替换 {count} 处）")
