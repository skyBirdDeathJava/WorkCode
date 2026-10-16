"""权限闸门：对每次工具调用做 allow / deny / ask 三档决策。"""

from __future__ import annotations

import asyncio
import os
import re
from typing import Optional

# 危险命令黑名单（正则，命中直接 deny）
DANGEROUS_CMD_PATTERNS = [
    r"\brm\s+(-[a-z]*[fr]+[a-z]*\s+)?/",
    r"\brm\s+-rf\b",
    r"\bmkfs\b",
    r"\bmkfs\.",
    r"\bsudo\b",
    r"\bgit\s+push\s+--force\b",
    r"\bshutdown\b",
    r"\breboot\b",
    r"\bdd\b\s+if=",
    r">\s*/dev/sd",
    r":\s*\(\)\s*\{",  # fork bomb
]

READ_TOOLS = {"read_file", "list_directory", "search_files"}
WRITE_TOOLS: set[str] = {"write_file", "edit_file", "apply_patch"}
COMMAND_TOOL = "run_command"

# 安全只读/构建/测试命令：匹配前缀自动 allow（不弹确认）
SAFE_COMMAND_PREFIXES = [
    "ls", "cat", "pwd", "echo", "which", "find", "grep",
    "git status", "git diff", "git log", "git branch", "git remote", "git stash list",
    "go build", "go test", "go vet", "go fmt",
    "npm test", "npm run build", "npm run lint",
    "pnpm test", "pnpm run build", "pnpm run lint",
    "yarn test", "yarn build",
    "pytest", "python -m pytest", "python -m unittest", "python -m compileall",
    "cargo build", "cargo test", "cargo check",
    "make build", "make test", "make check",
    "tsc", "tsc --noEmit", "eslint", "prettier --check",
]


def _within_workspace(workspace_root: str, path: str) -> bool:
    if not workspace_root or not path:
        return False
    root = os.path.realpath(workspace_root)
    target = os.path.realpath(path)
    return target == root or target.startswith(root + os.sep)


class Gateway:
    """每个 WebSocket 连接持有一个实例。

    - 权限闸门：ask 决策通过 request_id 等待插件响应
    - Diff 闸门：写操作暂存后通过 request_id 等待用户接受/拒绝
    """

    def __init__(self) -> None:
        self._pending: dict[str, asyncio.Future] = {}
        self._diff_pending: dict[str, asyncio.Future] = {}
        self._terminal_pending: dict[str, asyncio.Future] = {}
        self.allowed_commands: list[str] = []  # 预批准命令前缀，匹配则不确认

    def decide(self, tool: str, args: dict, workspace_root: str, mode: str = "manual") -> str:
        if tool in READ_TOOLS:
            return "allow"
        if tool == "web_search":
            return "allow"  # 只读外部搜索
        if tool == "web_fetch":
            return "ask"  # 网络抓取需确认（工具内还有 SSRF 防护）
        if tool == COMMAND_TOOL:
            cmd = args.get("command", "")
            if any(re.search(p, cmd) for p in DANGEROUS_CMD_PATTERNS):
                return "deny"
            stripped = cmd.strip().lower()
            if any(stripped.startswith(prefix) for prefix in SAFE_COMMAND_PREFIXES):
                return "allow"
            # 预批准命令（用户配置）：匹配前缀直接 allow，任何模式都不弹确认
            if any(stripped.startswith(p.lower().strip()) for p in self.allowed_commands if p.strip()):
                return "allow"
            # Auto 模式：非危险命令自动执行；Manual/Plan 仍需确认
            if mode == "auto":
                return "allow"
            return "ask"
        if tool in WRITE_TOOLS:
            path = args.get("path", "")
            if not _within_workspace(workspace_root, path):
                return "deny"
            return "ask"
        # 未知工具：不直接执行
        return "deny"

    async def wait_for_decision(self, request_id: str, timeout: float = 30.0) -> bool:
        loop = asyncio.get_event_loop()
        future = loop.create_future()
        self._pending[request_id] = future
        try:
            return await asyncio.wait_for(future, timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            return False
        finally:
            self._pending.pop(request_id, None)

    def resolve(self, request_id: str, allowed: bool) -> None:
        future = self._pending.get(request_id)
        if future is not None and not future.done():
            future.set_result(allowed)

    async def wait_for_diff(self, request_id: str, timeout: float = 180.0) -> bool:
        loop = asyncio.get_event_loop()
        future = loop.create_future()
        self._diff_pending[request_id] = future
        try:
            return await asyncio.wait_for(future, timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            return False
        finally:
            self._diff_pending.pop(request_id, None)

    def resolve_diff(self, request_id: str, accepted: bool) -> None:
        future = self._diff_pending.get(request_id)
        if future is not None and not future.done():
            future.set_result(accepted)

    async def wait_for_terminal(self, request_id: str, timeout: float = 30.0) -> Optional[dict]:
        """等待扩展在 VS Code 终端里执行命令的结果。超时/取消返回 None（走 pty 回退）。"""
        loop = asyncio.get_event_loop()
        future = loop.create_future()
        self._terminal_pending[request_id] = future
        try:
            return await asyncio.wait_for(future, timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            return None
        finally:
            self._terminal_pending.pop(request_id, None)

    def resolve_terminal(self, request_id: str, result: dict) -> None:
        future = self._terminal_pending.get(request_id)
        if future is not None and not future.done():
            future.set_result(result)
