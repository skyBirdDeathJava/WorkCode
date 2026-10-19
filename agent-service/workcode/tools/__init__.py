"""MVP 工具集：只读（allow）+ 写（Diff 确认）+ 命令（ask/deny）+ Web（搜索 allow / 抓取 ask）+ 子代理。"""

from __future__ import annotations

from . import cmd, read, web, write
from .registry import ToolRegistry, ToolDef


def _stub_tool(_tool: str) -> tuple[bool, str]:
    """task 由 Agent 循环特殊处理，不会走到 registry 执行。"""
    return (False, "task 工具应由循环特殊处理")


def build_registry(read_only: bool = False) -> ToolRegistry:
    """构建工具注册表。read_only=True 用于子 Agent：只保留读 + 搜索（避免嵌套确认/递归）。"""
    r = ToolRegistry()

    r.register(
        ToolDef(
            name="read_file",
            description="读取指定文件的文本内容。offset 从第几行开始（0 起），limit 限制最多读取行数。",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "要读取的文件路径"},
                    "offset": {"type": "integer", "description": "起始行号（0 起）"},
                    "limit": {"type": "integer", "description": "最多读取的行数"},
                },
                "required": ["path"],
            },
            handler=read.read_file,
        )
    )

    r.register(
        ToolDef(
            name="list_directory",
            description="列出目录下的文件和子目录（子目录以 / 结尾）。",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "目录路径，缺省为当前工作目录"},
                },
            },
            handler=read.list_directory,
        )
    )

    r.register(
        ToolDef(
            name="search_files",
            description="按文件名或文件内容搜索，大小写不敏感，返回文件路径与匹配行。",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键字"},
                    "path": {"type": "string", "description": "搜索起点目录，缺省为当前工作目录"},
                },
                "required": ["query"],
            },
            handler=read.search_files,
        )
    )

    r.register(
        ToolDef(
            name="web_search",
            description="在互联网上搜索，返回标题、链接和摘要（如查 API 文档、报错解法）。",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键字"},
                    "max_results": {"type": "integer", "description": "返回结果条数，默认 5"},
                },
                "required": ["query"],
            },
            handler=web.web_search,
        )
    )

    if not read_only:
        r.register(
            ToolDef(
                name="write_file",
                description="把完整内容写入文件（新建或覆盖）。覆盖已有文件需用户确认。",
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "目标文件路径"},
                        "content": {"type": "string", "description": "要写入的完整文件内容"},
                    },
                    "required": ["path", "content"],
                },
                handler=write.write_file,
            )
        )

        r.register(
            ToolDef(
                name="edit_file",
                description=(
                    "在文件中做精确替换。oldText 必须与文件原文完全一致（含空白），"
                    "且默认只允许出现一次；出现多次需 replaceAll=true。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "目标文件路径"},
                        "oldText": {"type": "string", "description": "要被替换的原文（必须精确匹配，只出现一次）"},
                        "newText": {"type": "string", "description": "替换后的新文本"},
                        "replaceAll": {"type": "boolean", "description": "出现多次时是否全部替换"},
                    },
                    "required": ["path", "oldText", "newText"],
                },
                handler=write.edit_file,
            )
        )

        r.register(
            ToolDef(
                name="web_fetch",
                description="抓取指定网页并转为纯文本（如读文档、看线上报错页）。",
                parameters={
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "要抓取的网页 URL"},
                    },
                    "required": ["url"],
                },
                handler=web.web_fetch,
            )
        )

        r.register(
            ToolDef(
                name="apply_patch",
                description=(
                    "一次对多个文件做精确替换（批量修改）。changes 是数组，每项含 "
                    "path / oldText / newText（可选 replaceAll）。所有改动会合并成一个确认。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "changes": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "path": {"type": "string", "description": "文件路径"},
                                    "oldText": {"type": "string", "description": "要被替换的原文（必须精确匹配）"},
                                    "newText": {"type": "string", "description": "替换后的新文本"},
                                    "replaceAll": {"type": "boolean", "description": "出现多次时是否全部替换"},
                                },
                                "required": ["path", "oldText", "newText"],
                            },
                            "description": "多个文件的精确替换",
                        }
                    },
                    "required": ["changes"],
                },
                handler=write.apply_apply_patch,
            )
        )

        r.register(
            ToolDef(
                name="run_command",
                description=(
                    "在长驻终端会话里执行命令（shell，测试、构建、版本控制等）。"
                    "cwd 与环境变量跨命令保持：可以先 cd 再运行；连续多步任务一条条来即可。"
                    "程序拿到真实终端，交互式命令可配合 input 提供输入。"
                    "危险命令会被直接拦截；普通命令需用户确认。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "command": {"type": "string", "description": "要执行的命令"},
                        "cwd": {"type": "string", "description": "首次执行时的工作目录，缺省为工作区根目录；之后保持 shell 状态"},
                        "timeout": {"type": "number", "description": "超时秒数，默认 30"},
                        "input": {"type": "string", "description": "命令读取 stdin 时提供的输入（交互式命令用），可选"},
                    },
                    "required": ["command"],
                },
                handler=cmd.run_command,
            )
        )

        r.register(
            ToolDef(
                name="task",
                description=(
                    "派生一个子 Agent 去独立梳理/调研（只读工具），返回它的结论。"
                    "适合大范围代码调研、并行探索。子 Agent 只能读/搜，不能改文件。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "prompt": {"type": "string", "description": "给子 Agent 的聚焦任务描述"},
                    },
                    "required": ["prompt"],
                },
                handler=_stub_tool,
            )
        )

    return r
