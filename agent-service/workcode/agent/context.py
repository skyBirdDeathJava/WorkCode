"""上下文管理：把编辑器上下文渲染进用户消息，控制 token 预算。"""

from __future__ import annotations

import json
import re
import subprocess
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

SYSTEM_PROMPT = """你是 WorkCode，一个运行在 VS Code 中的编程 Agent，帮助用户在编辑器里解释、生成、修改代码，并执行命令、搜索网页。

你能访问这些工具：
- read_file / list_directory / search_files：读文件、列目录、按文件名或内容搜索
- write_file / edit_file：写文件、精确替换修改（会先展示 Diff，确认后才落盘）
- run_command：在工作区执行命令（测试、构建、git、脚本等）
- web_search / web_fetch：搜索网页、抓取网页内容

规则：
- 用与用户提问相同的语言回答。
- 用 Markdown 结构化输出：解释代码时用 ## 小节标题分段（如「## 这段代码的作用」「## 逐行解释」），关键结论加粗，代码用 ``` 代码块，必要时用列表和引用。
- 引用文件时给出路径和行号（如 `src/foo.ts:42`）。

注入的上下文：
- 【当前文件】【选中代码】【引用文件】（@ 引用）是你正在看的代码，直接基于它们作答，不要重复读取。
- 【Git 状态】是仓库当前状态（分支/变更/最近提交），改代码时留意，避免破坏已有改动。
- 【项目记忆（WorkCode.md）】是项目约定，遵守其中的规则。
- （历史摘要）是压缩的早前对话背景，用于延续上下文，不是新的要求。

工具选择：
- 想找某个文件或代码：search_files / list_directory
- 想读某个文件内容：read_file
- 要修改代码：先 read_file 了解现状，再用 write_file / edit_file 精确修改
- 要跑测试、构建或看 git：run_command
- 遇到不认识的 API、库或报错：web_search；需要完整内容再 web_fetch
- 不确定时先用工具查证，不要凭空猜测。

权限与执行：
- 工具是否执行由当前权限模式（Manual/Auto/Plan）决定，需要确认时界面会弹出提示。
- 只读操作通常直接执行；写文件和命令按权限模式处理。
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


def load_user_memory(memory_file: str) -> str:
    """读取跨项目用户记忆（~/.workcode/memory.md），换项目也生效。"""
    if not memory_file:
        return ""
    p = Path(memory_file)
    if p.is_file():
        try:
            return p.read_text(encoding="utf-8", errors="replace")[:MAX_MEMORY_CHARS]
        except OSError:
            return ""
    return ""


def generate_project_memory(workspace_root: str) -> str:
    """扫描项目结构，生成一份 WorkCode.md 项目约定模板（/init 用）。"""
    if not workspace_root:
        return "# WorkCode 项目约定\n\n（未检测到工作区）\n"
    root = Path(workspace_root)

    # 语言统计（跳过隐藏/产物目录）
    from collections import Counter

    langs: Counter = Counter()
    for p in root.rglob("*"):
        if p.is_file() and p.suffix:
            parts = p.relative_to(root).parts
            if any(part.startswith(".") for part in parts[:-1]):
                continue
            if p.name in ("node_modules", ".venv", "dist", "build"):
                continue
            langs[p.suffix.lstrip(".")] += 1
    top_langs = [k for k, _ in langs.most_common(5)]

    key_files = [
        n for n in ("package.json", "pyproject.toml", "tsconfig.json", "Cargo.toml", "go.mod", "Makefile", "README.md")
        if (root / n).is_file()
    ]

    scripts = ""
    pj = root / "package.json"
    if pj.is_file():
        try:
            scripts = "\n".join(f"- {k}: {v}" for k, v in json.loads(pj.read_text(encoding="utf-8")).get("scripts", {}).items())
        except Exception:
            pass

    top_dirs = sorted(
        d.name for d in root.iterdir()
        if d.is_dir() and not d.name.startswith(".") and d.name not in ("node_modules", ".venv", "dist", "build", ".git")
    )

    lines = ["# WorkCode 项目约定\n"]
    lines.append(f"## 技术栈\n\n主要语言：{', '.join(top_langs) or '（未检测）'}\n")
    if key_files:
        lines.append(f"关键配置文件：{', '.join(key_files)}\n")
    if scripts:
        lines.append(f"## 常用命令\n\n{scripts}\n")
    if top_dirs:
        lines.append(f"## 顶层结构\n\n{', '.join(top_dirs)}\n")
    lines.append("\n> 由 WorkCode /init 生成，可编辑补充项目约定。")
    return "\n".join(lines)


def estimate_cost_usd(model: str, usage: dict) -> float:
    """按模型估算美元成本（约；每百万 token 单价）。"""
    key = "deepseek-v4-pro" if "v4-pro" in model else "deepseek-flash"
    price = MODEL_PRICES.get(key, MODEL_PRICES["deepseek-flash"])
    prompt = usage.get("promptTokens", 0) / 1e6 * price["in"]
    completion = usage.get("completionTokens", 0) / 1e6 * price["out"]
    return prompt + completion


MODEL_PRICES = {
    "deepseek-flash": {"in": 0.14, "out": 0.28},
    "deepseek-v4-pro": {"in": 0.56, "out": 1.68},
}


def append_user_memory(memory_file: str, text: str) -> None:
    """向用户记忆文件追加一条偏好（/memory add 用）。"""
    if not memory_file or not text.strip():
        return
    p = Path(memory_file)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(f"- {text.strip()}\n")


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


def load_git_context(workspace_root: str) -> str:
    """注入 git 状态：分支、工作区变更、最近提交（让 Agent 感知仓库状态）。非 git 仓库返回空。"""
    if not workspace_root:
        return ""
    try:
        status = subprocess.run(
            ["git", "-C", workspace_root, "status", "--short", "--branch"],
            capture_output=True, text=True, timeout=2,
        )
        log = subprocess.run(
            ["git", "-C", workspace_root, "log", "--oneline", "-5"],
            capture_output=True, text=True, timeout=2,
        )
    except Exception:
        return ""
    if status.returncode != 0:
        return ""  # 不是 git 仓库
    lines: list[str] = []
    for ln in status.stdout.strip().splitlines():
        if ln.startswith("## "):
            branch = ln[3:].split("...")[0]
            lines.append(f"分支: {branch}")
        elif ln.strip():
            lines.append(f"变更: {ln.strip()}")
    if not lines:
        lines.append("分支: (未知) 工作区干净")
    commits = log.stdout.strip()
    if commits:
        lines.append("最近提交:\n" + commits)
    return "\n".join(lines)


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


MODE_HINTS = {
    "manual": "权限模式：Manual——写文件、执行命令前都会请求你确认。",
    "auto": "权限模式：Auto——非危险命令自动执行，写文件仍会请求确认。",
    "plan": (
        "权限模式：Plan——先给出清晰的修改方案（步骤 + 涉及文件 + 预期改动），"
        "等用户批准后再动手。在用户明确批准前，不要调用 write_file / edit_file / run_command。"
    ),
}


def build_messages(
    history: list[dict],     # 用户消息（包含引用文件等）+历史对话
    ctx: EditorContext | dict,
    max_tokens: int,
    mode: str = "manual",
    extra_system: str = "",
) -> list[dict]:
    """system + 历史（含项目记忆 + 权限模式 + git 上下文），token 超限时裁剪最早消息。"""
    workspace_root = ctx.get("workspaceRoot", "") if isinstance(ctx, dict) else getattr(ctx, "workspaceRoot", "")
    system_prompt = SYSTEM_PROMPT # agent的系统提示词
    memory = load_project_memory(workspace_root) #项目记忆 wordcode.md
    if memory:
        system_prompt += f"\n\n【项目记忆（WorkCode.md）】\n{memory}"
    hint = MODE_HINTS.get(mode)
    if hint:
        system_prompt += f"\n\n{hint}"
    if extra_system:
        system_prompt += f"\n\n{extra_system}"
    #系统提示词包含项目记忆+权限模式提示+git 状态
    messages = [{"role": "system", "content": system_prompt}] + history
    while len(messages) > 2:
        total = sum(estimate_tokens(m.get("content", "")) for m in messages)
        if total <= max_tokens:
            break
        # 丢弃最旧轮次。若最旧的是 assistant(tool_calls)，必须连同它跟随的
        # tool 消息一起删，否则会留下没有前置 tool_calls 的 tool 消息 → 模型 400。
        end = 1
        if messages[1].get("role") == "assistant" and messages[1].get("tool_calls"):
            while end + 1 < len(messages) and messages[end + 1].get("role") == "tool":
                end += 1
        del messages[1 : end + 1]
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