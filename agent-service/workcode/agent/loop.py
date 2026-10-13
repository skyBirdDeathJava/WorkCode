"""Agent 主循环：组装上下文 → 流式调用模型 → 解析输出 → 工具执行 → 继续。

MVP 阶段只接只读工具（默认 allow），权限闸门的 ask 路径已接通但暂不触发。
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any, Awaitable, Callable

from ..config import Settings
from ..gateway import Gateway
from ..session.store import Session, SessionStore
from ..tools import write as write_tools
from ..tools.cmd import TerminalSession
from ..tools.registry import ToolRegistry
from . import context
from .model import ModelProvider, StepResult

logger = logging.getLogger(__name__)

# 工具结果注入历史时的长度上限
MAX_TOOL_OUTPUT_CHARS = 4000

Emit = Callable[[str, dict[str, Any]], Awaitable[None]]


WRITE_TOOLS = {"write_file", "edit_file"}


def _describe(name: str, args: dict[str, Any]) -> str:
    """给权限请求一个人类可读的摘要。"""
    if name == "write_file":
        return f"写入文件 {args.get('path', '?')}"
    if name == "edit_file":
        return f"修改文件 {args.get('path', '?')}"
    if name == "run_command":
        return f"执行命令：{args.get('command', '?')}"
    return f"{name} {json.dumps(args, ensure_ascii=False)[:200]}"


def _result_payload(call_id: str, name: str, ok: bool, output: str, args: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {"requestId": call_id, "tool": name, "ok": ok, "output": output}
    if name in WRITE_TOOLS and args.get("path"):
        payload["path"] = args["path"]
    return payload


def _args_to_json_str(arguments: Any) -> str:
    if isinstance(arguments, str):
        return arguments
    if isinstance(arguments, dict):
        return json.dumps(arguments, ensure_ascii=False)
    return ""


def _parse_arguments(arguments: Any) -> dict[str, Any]:
    if isinstance(arguments, dict):
        return arguments
    if isinstance(arguments, str):
        s = arguments.strip()
        if not s:
            return {}
        try:
            return json.loads(s)
        except Exception:
            pass
        # 容错：去掉首尾引号/反引号后重试
        s2 = s.strip().strip("`").strip()
        try:
            return json.loads(s2)
        except Exception:
            return {}
    return {}


class AgentLoop:
    def __init__(
        self,
        model: ModelProvider,
        registry: ToolRegistry,
        gateway: Gateway,
        store: SessionStore,
        settings: Settings,
        terminal: "TerminalSession | None" = None,
        terminal_supported: bool = False,
        mode: str = "manual",
    ) -> None:
        self.model = model
        self.registry = registry
        self.gateway = gateway
        self.store = store
        self.settings = settings
        self.terminal = terminal
        self.terminal_supported = terminal_supported
        self.mode = mode
        self._workspace_root = ""
        self._usage: dict[str, int] = {"promptTokens": 0, "completionTokens": 0}

    async def run(
        self,
        session: Session,
        content: str,
        editor_ctx: Any,
        emit: Emit,
    ) -> None:
        if isinstance(editor_ctx, dict):
            self._workspace_root = editor_ctx.get("workspaceRoot", "")
        else:
            self._workspace_root = getattr(editor_ctx, "workspaceRoot", "")

        # 历史超限 → 自动压缩（摘要落盘），长会话不失忆
        await self._maybe_compact(session)

        self._append_message(session, {"role": "user", "content": context.render_user_message(content, editor_ctx)})

        # git 上下文：每轮加载一次注入系统提示
        git_ctx = context.load_git_context(self._workspace_root)

        for step in range(self.settings.max_steps):
            messages = context.build_messages(
                session.messages,
                editor_ctx,
                self.settings.max_context_tokens,
                mode=self.mode,
                extra_system=(f"【Git 状态】\n{git_ctx}" if git_ctx else ""),
            )
            # 用量统计（估算 token）
            self._usage["promptTokens"] += context.estimate_total_tokens(messages)
            async def on_delta(delta: str) -> None:
                self._usage["completionTokens"] += context.estimate_tokens(delta)
                await emit("text_delta", {"delta": delta})

            try:
                result = await asyncio.wait_for(
                    self.model.stream(messages, self.registry.schema(), on_delta),
                    timeout=self.settings.model_timeout_seconds,
                )
            except asyncio.CancelledError:
                await emit("error", {"message": "已取消"})
                return
            except asyncio.TimeoutError:
                await emit("error", {"message": "模型响应超时"})
                break
            except Exception as e:  # noqa: BLE001 — 需要兜底并把原因反馈给用户
                logger.exception("model stream failed")
                await emit("error", {"message": f"模型调用失败：{e}"})
                break

            if result.tool_calls:
                # 先把 assistant 的 tool_calls 消息写进历史，再执行工具，
                # 否则模型 API 会因缺少该消息而 400（OpenAI 兼容协议要求）
                self._append_assistant_tool_call(session, result)
                self._usage["completionTokens"] += context.estimate_tokens(result.text)
                await self._handle_tool_calls(result.tool_calls, session, editor_ctx, emit)
                continue

            if result.text:
                self._append_message(session, {"role": "assistant", "content": result.text})
                await emit("done", {"usage": dict(self._usage)})
                return

            # 既无文本也无工具调用：异常情况
            await emit("error", {"message": "模型未返回任何内容，已停止"})
            break

        await emit(
            "error",
            {
                "message": (
                    f"这一轮的工具调用步骤已达上限（{self.settings.max_steps} 步），"
                    "为避免死循环已停止。你可以继续追问，会话轮数不设限。"
                )
            },
        )

    def _append_message(self, session: Session, message: dict) -> None:
        """追加一条消息：写进会话内存 + 逐条落盘 JSONL。"""
        session.messages.append(message)
        self.store.append(session.id, message)

    async def _maybe_compact(self, session: Session) -> None:
        """历史超限时压缩：保留最近轮次，更早的用模型摘要，摘要落盘。静默进行。"""
        threshold = int(self.settings.max_context_tokens * 0.8)
        if context.estimate_total_tokens(session.messages) <= threshold:
            return
        try:
            new_messages, summary = await context.compact(
                session.messages, self.model, threshold
            )
        except Exception as e:  # noqa: BLE001 — 压缩失败不阻塞对话
            logger.exception("compact failed: %s", e)
            return
        if new_messages is not None:
            session.messages = new_messages
            if summary:
                self.store.append_summary(session.id, summary)

    def _append_assistant_tool_call(self, session: Session, result: StepResult) -> None:
        tool_calls = [
            {
                "id": c.get("id", f"call_{i}"),
                "type": "function",
                "function": {"name": c["name"], "arguments": _args_to_json_str(c.get("arguments"))},
            }
            for i, c in enumerate(result.tool_calls)
        ]
        self._append_message(session, {"role": "assistant", "content": result.text or "", "tool_calls": tool_calls})

    async def _handle_tool_calls(
        self,
        calls: list[dict[str, Any]],
        session: Session,
        editor_ctx: Any,
        emit: Emit,
    ) -> None:
        workspace_root = self._workspace_root

        for call in calls:
            name = call.get("name", "")
            call_id = call.get("id", f"call_{name}")
            args = _parse_arguments(call.get("arguments"))

            # run_command 默认在工作区根目录执行（仅首次定位终端，之后保持 shell 状态）
            if name == "run_command" and not args.get("cwd") and workspace_root:
                args["cwd"] = workspace_root

            await emit("tool_call", {"requestId": call_id, "tool": name, "arguments": args})

            decision = self.gateway.decide(name, args, workspace_root, mode=self.mode)

            if decision == "deny":
                ok, output = False, f"权限被拒绝：不允许执行 {name}"
                await emit("tool_result", _result_payload(call_id, name, ok, output, args))
            elif decision == "ask":
                if name in WRITE_TOOLS:
                    # 写操作走「Diff 预览 → 用户接受 → 落盘」流程
                    ok, output = await self._handle_write_with_diff(name, args, emit)
                else:
                    ok, output = await self._handle_permission_ask(name, args, emit)
                await emit("tool_result", _result_payload(call_id, name, ok, output, args))
            else:  # allow
                ok, output = await self._execute(name, args, emit)
                await emit("tool_result", _result_payload(call_id, name, ok, output, args))

            # 把工具结果作为 tool 角色消息写回历史（落盘），供模型继续推理
            result_text = output if ok else f"[工具失败] {output}"
            self._append_message(
                session,
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": result_text[:MAX_TOOL_OUTPUT_CHARS],
                },
            )

    async def _handle_permission_ask(self, name: str, args: dict[str, Any], emit: Emit) -> tuple[bool, str]:
        """通用权限弹窗（非写工具，如 M5 的 run_command）。"""
        request_id = f"perm_{uuid.uuid4().hex[:8]}"
        await emit(
            "permission_request",
            {
                "requestId": request_id,
                "tool": name,
                "description": _describe(name, args),
                "arguments": args,
            },
        )
        allowed = await self.gateway.wait_for_decision(
            request_id, timeout=self.settings.permission_timeout_seconds
        )
        if not allowed:
            return False, "用户拒绝了该操作"
        return await self._execute(name, args, emit)

    async def _execute(self, name: str, args: dict[str, Any], emit: Emit) -> tuple[bool, str]:
        """统一执行入口：run_command 桥接到 VS Code 终端（或 pty 回退），其余走注册表。"""
        if name == "run_command":
            return await self._execute_run_command(args, emit)
        return await self.registry.execute(name, args)

    async def _execute_run_command(self, args: dict[str, Any], emit: Emit) -> tuple[bool, str]:
        if self.terminal is None:
            return (False, "终端不可用")
        command = args.get("command", "")
        cwd = args.get("cwd") or (self._workspace_root or None)
        timeout = float(args.get("timeout") or 30.0)

        # 交互命令（带 input）走 pty：交互读取可靠，输出在 chat 可见
        if args.get("input") or not self.terminal_supported:
            return await self.terminal.run(command=command, cwd=cwd, timeout=timeout, input=args.get("input"))

        # 普通命令：桥接到 VS Code 集成终端（可见、长驻进程可挂后台）
        request_id = f"term_{uuid.uuid4().hex[:8]}"
        await emit(
            "terminal_exec",
            {"requestId": request_id, "command": command, "cwd": cwd or ""},
        )
        result = await self.gateway.wait_for_terminal(
            request_id, timeout=self.settings.terminal_bridge_timeout
        )
        if result is None:
            # 扩展未响应（standalone 运行或不支持）→ 回退服务端 pty
            return await self.terminal.run(command=command, cwd=cwd, timeout=timeout)

        ok = bool(result.get("ok"))
        output = str(result.get("output", ""))
        if result.get("stillRunning"):
            output = output + "\n（命令仍在运行，可查看 VS Code 终端）"
        if result.get("exitCode") is not None and not ok and not result.get("stillRunning"):
            output = f"退出码 {result.get('exitCode')}：{output}"
        return (ok, output)

    async def _handle_write_with_diff(self, name: str, args: dict[str, Any], emit: Emit) -> tuple[bool, str]:
        """写操作：先暂存并展示 Diff，用户接受后才落盘。"""
        ok, original, new, error = self._preview_change(name, args)
        if not ok:
            return False, error

        request_id = f"diff_{uuid.uuid4().hex[:8]}"
        await emit(
            "diff_preview",
            {
                "requestId": request_id,
                "path": args.get("path", ""),
                "originalContent": original,
                "newContent": new,
            },
        )
        accepted = await self.gateway.wait_for_diff(
            request_id, timeout=self.settings.diff_timeout_seconds
        )
        if not accepted:
            return False, "用户未接受该修改（Diff 被拒绝或超时）"
        # 用户接受 → 真正落盘
        return await self.registry.execute(name, args)

    @staticmethod
    def _preview_change(name: str, args: dict[str, Any]) -> tuple[bool, str, str, str]:
        if name == "write_file":
            return write_tools.preview_write_file(args.get("path", ""), args.get("content", ""))
        if name == "edit_file":
            return write_tools.preview_edit_file(
                args.get("path", ""),
                args.get("oldText", ""),
                args.get("newText", ""),
                args.get("replaceAll", False),
            )
        return False, "", "", f"未知写工具: {name}"
