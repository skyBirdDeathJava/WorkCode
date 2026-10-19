"""Agent 主循环：组装上下文 → 流式调用模型 → 解析输出 → 工具执行 → 继续。

MVP 阶段只接只读工具（默认 allow），权限闸门的 ask 路径已接通但暂不触发。
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any, Awaitable, Callable, Optional

from ..config import Settings
from ..gateway import Gateway
from ..hooks import load_hooks, run_hook
from ..session.store import Session, SessionStore
from ..tools import build_registry, write as write_tools
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
        self.hooks: dict = {}

    async def run(
        self,
        session: Session,
        content: str,
        editor_ctx: Any,
        emit: Emit,
    ) -> Optional[str]:
        if isinstance(editor_ctx, dict):
            self._workspace_root = editor_ctx.get("workspaceRoot", "")
        else:
            self._workspace_root = getattr(editor_ctx, "workspaceRoot", "")

        # 历史超限 → 自动压缩（摘要落盘），长会话不失忆
        await self._maybe_compact(session)

        self._append_message(session, {"role": "user", "content": context.render_user_message(content, editor_ctx)})

        # git 上下文 + 跨项目用户记忆：每轮加载一次注入系统提示
        git_ctx = context.load_git_context(self._workspace_root)
        user_memory = context.load_user_memory(self.settings.memory_file)
        extras = []
        if user_memory:
            extras.append(f"【用户记忆（memory.md）】\n{user_memory}")
        if git_ctx:
            extras.append(f"【Git 状态】\n{git_ctx}")
        extra_system = "\n\n".join(extras)

        # Hooks：生命周期脚本（.workcode/hooks.json）
        self.hooks = load_hooks(self._workspace_root)
        await run_hook(self.hooks, "UserPromptSubmit")

        # 死循环检测状态 + 是否因错误提前停止
        last_sig: tuple = ()
        repeat_count = 0
        aborted = False

        for step in range(self.settings.max_steps):
            # messages 包含用户消息（包含引用文件等）+历史对话+系统提示词
            messages = context.build_messages(
                session.messages,# 用户消息（包含引用文件等）+历史对话
                editor_ctx, # 工作根路径，选中文件，选中代码
                self.settings.max_context_tokens, # 上下文最大token数
                mode=self.mode,
                extra_system=extra_system,
            )
            # 用量统计（估算 输入给大模型的token）
            self._usage["promptTokens"] += context.estimate_total_tokens(messages)
            async def on_delta(delta: str) -> None:
                self._usage["completionTokens"] += context.estimate_tokens(delta) #推送给前端的token,输出
                await emit("text_delta", {"delta": delta})

            async def on_reasoning(delta: str) -> None:
                await emit("reasoning_delta", {"delta": delta})

            try:
                result = await asyncio.wait_for(
                    self.model.stream(messages, self.registry.schema(), on_delta, on_reasoning),
                    timeout=self.settings.model_timeout_seconds,
                )
            except asyncio.CancelledError:
                await emit("error", {"message": "已取消"})
                return
            except asyncio.TimeoutError:
                aborted = True
                await emit("error", {"message": "模型响应超时"})
                break
            except Exception as e:  # noqa: BLE001 — 需要兜底并把原因反馈给用户
                logger.exception("model stream failed")
                aborted = True
                await emit("error", {"message": f"模型调用失败：{e}"})
                break

            if result.tool_calls: #本次agent循环调用大模型返回了工具调用，无content的
                # 死循环检测：连续 3 次agent循环调用完全相同的工具+参数且无进展 → 停止
                sig = tuple(
                    (c.get("name", ""), str(c.get("arguments", ""))[:200])
                    for c in result.tool_calls
                )
                if sig == last_sig:
                    repeat_count += 1
                else:
                    repeat_count = 0
                last_sig = sig
                if repeat_count >= 3:
                    aborted = True
                    await emit(
                        "error",
                        {"message": "模型在重复调用相同的工具且没有进展，为避免浪费已停止。你可以换个说法再试。"},
                    )
                    break

                # 先把 assistant 的 tool_calls 消息写进历史，再执行工具，
                # 否则模型 API 会因缺少该消息而 400（OpenAI 兼容协议要求）
                self._append_assistant_tool_call(session, result)
                self._usage["completionTokens"] += context.estimate_tokens(result.text)
                await self._handle_tool_calls(result.tool_calls, session, editor_ctx, emit)
                continue

            if result.text: #本次agent循环调用大模型返回了答案文本，最终答案，因为如果不是最终答案，模型会返回工具调用，工具调用时没有content的
                self._append_message(session, {"role": "assistant", "content": result.text})
                await run_hook(self.hooks, "Stop")
                usage = dict(self._usage)
                usage["costUsd"] = round(
                    context.estimate_cost_usd(getattr(self.model, "model", ""), usage), 6
                )
                await emit("done", {"usage": usage})
                return result.text

            # 既无文本也无工具调用：异常情况
            aborted = True
            await emit("error", {"message": "模型未返回任何内容，已停止"})
            break

        await run_hook(self.hooks, "Stop")
        # 只有真的跑满步数才报「已达上限」；出错/死循环停止不报（避免误导）
        if not aborted:
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
            if name == "run_command" and not args.get("cwd") and workspace_root: #cwd，执行命令的路径
                args["cwd"] = workspace_root

            await emit("tool_call", {"requestId": call_id, "tool": name, "arguments": args})

            # PreToolUse Hook：退出码 2 → 拦截该工具（当作拒绝）
            tool_env = {
                "WORKCODE_TOOL": name,
                "WORKCODE_ARGS": json.dumps(args, ensure_ascii=False)[:2000],
            }
            if await run_hook(self.hooks, "PreToolUse", env=tool_env) == 2:
                ok, output = False, f"PreToolUse 拦截了 {name}"
                await emit("tool_result", _result_payload(call_id, name, ok, output, args))
                self._append_message(
                    session,
                    {"role": "tool", "tool_call_id": call_id, "content": f"[工具失败] {output}"[:MAX_TOOL_OUTPUT_CHARS]},
                )
                continue

            decision = self.gateway.decide(name, args, workspace_root, mode=self.mode)

            if decision == "deny":
                ok, output = False, f"权限被拒绝：不允许执行 {name}"
                await emit("tool_result", _result_payload(call_id, name, ok, output, args))
            elif decision == "ask":
                if name in WRITE_TOOLS:
                    # 写操作走「Diff 预览 → 用户接受 → 落盘」流程
                    ok, output = await self._handle_write_with_diff(name, args, emit)
                else: # 非写工具走通用权限弹窗（M5 的 run_command）
                    ok, output = await self._handle_permission_ask(name, args, emit)
                await emit("tool_result", _result_payload(call_id, name, ok, output, args))
            else:  # allow
                ok, output = await self._execute(name, args, emit)
                await emit("tool_result", _result_payload(call_id, name, ok, output, args))

            # PostToolUse Hook：工具执行后触发（改文件后自动格式化等）
            post_env = {
                "WORKCODE_TOOL": name,
                "WORKCODE_PATH": args.get("path", "") if isinstance(args.get("path", ""), str) else "",
                "WORKCODE_OK": "1" if ok else "0",
            }
            await run_hook(self.hooks, "PostToolUse", env=post_env)

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
        """统一执行入口：run_command 走终端桥，task 派生子 Agent，其余走注册表。"""
        if name == "run_command":
            return await self._execute_run_command(args, emit)
        if name == "task":
            return await self._handle_task(args, emit)
        return await self.registry.execute(name, args)

    async def _handle_task(self, args: dict[str, Any], emit: Emit) -> tuple[bool, str]:
        """派生一个只读子 Agent（独立会话 + 独立上下文），跑完把结论喂回主 Agent。"""
        subtask = str(args.get("prompt", "")).strip()
        if not subtask:
            return False, "task 需要 prompt 参数（给子 Agent 的聚焦任务）"
        await emit(
            "subagent_log",
            {"tool": "task", "brief": f"启动子 Agent：{subtask[:40]}", "ok": None},
        )

        sub_session = self.store.create_session()
        sub_registry = build_registry(read_only=True)
        sub_gateway = Gateway()
        sub_loop = AgentLoop(
            self.model, sub_registry, sub_gateway, self.store, self.settings,
        )
        sub_editor = {
            "workspaceRoot": self._workspace_root,
            "currentFile": None,
            "selection": None,
            "openFiles": [],
        }
        # 子 Agent 的工具调用转发给前端（subagent_log），供「子代理」标签显示；文本/权限不外泄
        async def sub_emit(kind: str, payload: dict[str, Any]) -> None:
            if kind in ("tool_call", "tool_result"):
                args = payload.get("arguments", {})
                brief = json.dumps(args, ensure_ascii=False)[:80]
                await emit(
                    "subagent_log",
                    {
                        "tool": payload.get("tool", ""),
                        "brief": brief,
                        "ok": None if kind == "tool_call" else bool(payload.get("ok", False)),
                    },
                )

        try:
            result_text = await asyncio.wait_for(
                sub_loop.run(sub_session, subtask, sub_editor, sub_emit),
                timeout=self.settings.task_timeout_seconds,
            )
        except asyncio.TimeoutError:
            return False, "子 Agent 超时"
        except Exception as e:  # noqa: BLE001
            logger.exception("subagent failed")
            return False, f"子 Agent 失败：{e}"

        if result_text:
            return True, f"[子 Agent 结论]\n{result_text[:3000]}"
        return False, "子 Agent 未产出结论"

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
        """写操作：先暂存并展示 Diff，用户接受后才落盘。apply_patch 支持多文件一次确认。"""
        request_id = f"diff_{uuid.uuid4().hex[:8]}"

        if name == "apply_patch":
            changes = args.get("changes", [])
            if not isinstance(changes, list) or not changes:
                return False, "apply_patch 需要非空的 changes 数组"
            ok, staged, error = write_tools.preview_apply_patch(changes)
            if not ok:
                return False, error
            # 合并成一个 Diff 预览（按文件分节）
            parts_orig = [f"=== {path} ===\n{original}" for path, original, _ in staged]
            parts_new = [f"=== {path} ===\n{new}" for path, _, new in staged]
            await emit(
                "diff_preview",
                {
                    "requestId": request_id,
                    "path": f"📦 批量修改 {len(staged)} 个文件",
                    "originalContent": "\n\n".join(parts_orig),
                    "newContent": "\n\n".join(parts_new),
                },
            )
            accepted = await self.gateway.wait_for_diff(
                request_id, timeout=self.settings.diff_timeout_seconds
            )
            if not accepted:
                return False, "用户未接受该修改（Diff 被拒绝或超时）"
            ok, output = write_tools.apply_apply_patch(changes)
            if ok and self.settings.auto_commit:
                await self._auto_commit(name, args)
            return ok, output

        ok, original, new, error = self._preview_change(name, args)
        if not ok:
            return False, error
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
        ok, output = await self.registry.execute(name, args)
        if ok and self.settings.auto_commit:
            await self._auto_commit(name, args)
        return ok, output

    async def _auto_commit(self, name: str, args: dict[str, Any]) -> None:
        """编辑落盘后自动 git add + commit（只提交被改的文件）。默认关闭，WORKCODE_AUTO_COMMIT=1 开启。"""
        root = self._workspace_root
        if not root:
            return
        paths: list[str] = []
        if name == "apply_patch":
            paths = [c.get("path", "") for c in args.get("changes", []) if c.get("path")]
        else:
            p = args.get("path", "")
            if p:
                paths = [p]
        if not paths:
            return
        try:
            add = await asyncio.create_subprocess_exec(
                "git", "-C", root, "add", "--", *paths,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            await add.communicate()
            msg = f"WorkCode: 修改 {', '.join(paths[:3])}"
            commit = await asyncio.create_subprocess_exec(
                "git", "-C", root, "commit", "-m", msg,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            await commit.communicate()
        except Exception:  # noqa: BLE001 — 自动 commit 是尽力而为
            pass

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
