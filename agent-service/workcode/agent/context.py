"""上下文管理：把编辑器上下文渲染进用户消息，控制 token 预算。"""

from __future__ import annotations

from ..protocol import EditorContext

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


def render_user_message(content: str, ctx: EditorContext | dict) -> str:
    """把编辑器上下文折叠进用户消息。"""
    parts = [content]

    if isinstance(ctx, dict):
        ctx = EditorContext.model_validate(ctx)

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
    """system + 历史，token 超限时从最早的非 system 消息开始裁剪。"""
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history
    while len(messages) > 2:
        total = sum(estimate_tokens(m.get("content", "")) for m in messages)
        if total <= max_tokens:
            break
        messages.pop(1)  # 丢弃最旧的 user/assistant/tool 轮次
    return messages