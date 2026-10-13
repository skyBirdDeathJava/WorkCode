"""上下文管理：把编辑器上下文渲染进用户消息，控制 token 预算。"""

from __future__ import annotations

import re
from pathlib import Path

from ..protocol import EditorContext
from ..session.store import SUMMARY_PREFIX

# @ 引用文件：匹配 @后面紧跟的路径 token（直到空白/标点）
AT_REF_RE = re.compile(r"@([^\s,;，。！？]+)")

# 项目记忆文件名（优先级从高到低）
MEMORY_FILE_NAMES = ("WorkCode.md", "CLAUDE.md")

MAX_MEMORY_CHARS = 8000
MAX_REF_FILE_CHARS = 6000

MAX_FILE_BODY_CHARS = 8000

SYSTEM_PROMPT = """你是 WorkCode，一个运行在 VS Code 中的编程 Agent。

你能读取工作区文件、搜索代码，帮助用户解释、生成和修改代码。

规则：
- 用与用户提问相同的语言回答。
- 用 Markdown 结构化输出：解释代码时用 ## 小节标题分段（如「## 这段代码的作用」「## 逐行解释」），关键结论加粗，代码用 ``` 代码块，必要时用列表和引用。
- 引用文件时给出路径和行号（如 `src/foo.ts:42`）。
- 需要了解代码时，优先使用 read_file / list_directory / search_files 工具，不要凭空猜测。
- 写文件、修改文件需经用户确认；执行命令需经用户确认，危险命令会被拦截。
- 如果信息不足以作答，说明你还需要什么，并建议如何补充。"""


def estimate_tokens(text: str) -> int:
    """粗略估算 token 数（中文约 1 字/token，此处用字符数/2 保守估算）。"""
    if not text:
        return 0
    # 中文字符计 1，ASCII 计 1/4
    cjk = sum(1 for ch in text if ord(ch) > 0x2FFF)
    ascii_count = len(text) - cjk
    return cjk + ascii_count // 4


def estimate_total_tokens(messages: list[dict]) -> int:
    return sum(estimate_tokens(m.get("content", "")) for m in messages)


def load_project_memory(workspace_root: str) -> str:
    """读取工作区根目录的 WorkCode.md（或 CLAUDE.md），作为项目记忆注入。"""
    if not workspace_root:
        return ""
    root = Path(workspace_root)
    for name in MEMORY_FILE_NAMES:
        p = root / name
        if p.is_file():
            try:
                return p.read_text(encoding="utf-8", errors="replace")[:MAX_MEMORY_CHARS]
            except OSError:
                return ""
    return ""


def resolve_refs(content: str, workspace_root: str) -> list[tuple[str, str]]:
    """解析消息里的 @路径 引用，读取对应文件内容（相对工作区根目录）。"""
    refs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for m in AT_REF_RE.finditer(content):
        ref = m.group(1)
        if ref in seen:
            continue
        seen.add(ref)
        p = Path(ref)
        if not p.is_absolute() and workspace_root:
            p = Path(workspace_root) / ref
        if p.is_file() and p.stat().st_size <= 256 * 1024:
            try:
                body = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            refs.append((ref, body[:MAX_REF_FILE_CHARS]))
    return refs


def render_user_message(content: str, ctx: EditorContext | dict) -> str:
    """把编辑器上下文 + @ 引用文件折叠进用户消息。"""
    parts = [content]

    if isinstance(ctx, dict):
        ctx = EditorContext.model_validate(ctx)

    # @ 引用文件：把被引用文件的内容注入
    for ref, body in resolve_refs(content, ctx.workspaceRoot):
        parts.append(f"【引用文件】{ref}\n```\n{body}\n```")

    if ctx.currentFile:
        f = ctx.currentFile
        body = f.content
        if len(body) > MAX_FILE_BODY_CHARS:
            body = body[:MAX_FILE_BODY_CHARS] + "\n...(文件过大，已截断)"
        lang = f.languageId or ""
        parts.append(f"【当前文件】{f.path}\n```{lang}\n{body}\n```")

    if ctx.selection and ctx.selection.text:
        s = ctx.selection
        parts.append(f"【选中代码】第 {s.startLine}-{s.endLine} 行\n```\n{s.text}\n```")

    if ctx.openFiles:
        parts.append("【已打开文件】" + ", ".join(ctx.openFiles[:10]))

    return "\n\n".join(parts)


def build_messages(
    history: list[dict],
    ctx: EditorContext | dict,
    max_tokens: int,
) -> list[dict]:
    """system + 历史（含 WorkCode.md 项目记忆），token 超限时裁剪最早消息。"""
    workspace_root = ctx.get("workspaceRoot", "") if isinstance(ctx, dict) else getattr(ctx, "workspaceRoot", "")
    system_prompt = SYSTEM_PROMPT
    memory = load_project_memory(workspace_root)
    if memory:
        system_prompt += f"\n\n【项目记忆（WorkCode.md）】\n{memory}"

    messages = [{"role": "system", "content": system_prompt}] + history
    while len(messages) > 2:
        total = sum(estimate_tokens(m.get("content", "")) for m in messages)
        if total <= max_tokens:
            break
        messages.pop(1)  # 丢弃最旧的 user/assistant/tool 轮次
    return messages


def extract_summary_source(messages: list[dict], max_chars: int = 8000) -> str:
    """从旧消息里提取适合做摘要的文本（跳过 tool 内部细节）。"""
    parts: list[str] = []
    for m in messages:
        role = m.get("role")
        if role in ("user", "assistant"):
            content = m.get("content", "")
            if content and not content.startswith("（历史摘要）"):
                parts.append(f"{role}: {content[:500]}")
    return "\n".join(parts)[:max_chars]


async def compact(
    messages: list[dict],
    model,
    max_tokens: int,
    keep_fraction: float = 0.4,
) -> tuple[list[dict] | None, str]:
    """历史超限时：保留最近的 keep_fraction，把更早的轮次用模型摘要。

    返回 (新消息列表, 摘要文本)；未超限返回 (None, "")。
    """
    if estimate_total_tokens(messages) <= max_tokens:
        return None, ""

    keep_n = max(2, int(len(messages) * keep_fraction))
    old = messages[:-keep_n]
    recent = messages[-keep_n:]

    source = extract_summary_source(old)
    if not source:
        # 没可摘要的内容（全是 tool 内部）→ 直接丢弃旧的
        return recent, ""

    summary = await model.summarize(source)
    new_messages = [{"role": "system", "content": SUMMARY_PREFIX + summary}] + recent
    return new_messages, summary