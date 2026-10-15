"""Agent 循环单测：重点锁住「assistant tool_calls 消息」的写入顺序。

历史顺序必须满足 OpenAI 兼容协议：user → assistant(tool_calls) → tool → assistant(text)，
否则模型 API 会 400（M3 期间踩过的坑）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from workcode.agent.loop import AgentLoop
from workcode.agent.model import MockModel, StepResult, ModelProvider


class DeadlockModel(ModelProvider):
    """永远返回同一个工具调用（模拟模型跑偏死循环）。"""

    async def stream(self, messages, tools, on_delta, on_reasoning=None) -> StepResult:
        return StepResult(
            tool_calls=[{"id": "x", "name": "read_file", "arguments": {"path": "/tmp/__loop__"}}]
        )

    async def summarize(self, text: str) -> str:
        return "[摘要]"
from workcode.config import Settings
from workcode.gateway import Gateway
from workcode.session.store import SessionStore
from workcode.tools import build_registry

pytestmark = pytest.mark.asyncio


def test_append_assistant_tool_call_format(tmp_path: Path) -> None:
    store = SessionStore(str(tmp_path / "sessions"))
    session = store.create_session()
    loop = AgentLoop(
        MockModel(), build_registry(), Gateway(), store,
        Settings(session_dir=str(tmp_path / "sessions")),
    )
    result = StepResult(
        text="",
        tool_calls=[
            {"id": "c1", "name": "read_file", "arguments": {"path": "/x/y.py"}},
            {"id": "c2", "name": "write_file", "arguments": '{"path": "/a.py", "content": "hi"}'},
        ],
    )
    loop._append_assistant_tool_call(session, result)
    msg = session.messages[0]
    assert msg["role"] == "assistant"
    assert msg["tool_calls"][0]["id"] == "c1"
    assert msg["tool_calls"][0]["type"] == "function"
    assert msg["tool_calls"][0]["function"]["name"] == "read_file"
    assert msg["tool_calls"][0]["function"]["arguments"] == '{"path": "/x/y.py"}'
    # 字符串参数保持原样
    assert msg["tool_calls"][1]["function"]["arguments"] == '{"path": "/a.py", "content": "hi"}'


async def test_loop_tool_message_ordering(tmp_path: Path) -> None:
    sessions = str(tmp_path / "sessions")
    store = SessionStore(sessions)
    session = store.create_session()
    gateway = Gateway()
    loop = AgentLoop(
        MockModel(),
        build_registry(),
        gateway,
        store,
        Settings(session_dir=sessions, permission_timeout_seconds=5.0),
    )

    target = tmp_path / "out.txt"
    editor = {"workspaceRoot": str(tmp_path), "currentFile": None, "selection": None, "openFiles": []}
    events: list[tuple[str, dict]] = []
    diff_event = asyncio.Event()
    diff_rid: dict[str, str] = {}

    async def emit(kind: str, payload: dict) -> None:
        events.append((kind, payload))
        if kind == "diff_preview":
            diff_rid["id"] = payload["requestId"]
            diff_event.set()

    loop_task = asyncio.create_task(loop.run(session, f"!tool write_file {target}", editor, emit))
    # 模拟用户点「接受」（与真实协议时序一致：先收到 Diff 预览，再回包）
    await diff_event.wait()
    gateway.resolve_diff(diff_rid["id"], True)
    await loop_task

    # 文件应已落盘
    assert target.read_text(encoding="utf-8").startswith("hello from WorkCode")

    # Diff 预览被触发（M4 的暂存 → 确认链路）
    assert any(k == "diff_preview" for k, _ in events)

    # 历史顺序：user → assistant(tool_calls) → tool → assistant(text)
    msgs = session.messages
    assert msgs[0]["role"] == "user"
    idx_assistant_tc = next(i for i, m in enumerate(msgs) if m["role"] == "assistant" and m.get("tool_calls"))
    idx_tool = next(i for i, m in enumerate(msgs) if m["role"] == "tool")
    assert idx_assistant_tc < idx_tool, "assistant tool_calls 消息必须排在 tool 结果之前"
    assert msgs[-1]["role"] == "assistant" and msgs[-1]["content"]


async def test_loop_auto_compact(tmp_path: Path) -> None:
    """历史超限时自动压缩，摘要写入会话。"""
    sessions = str(tmp_path / "sessions")
    store = SessionStore(sessions)
    session = store.create_session()
    # 预置超长历史
    for i in range(10):
        session.messages.append({"role": "user", "content": f"问题{i} " + "字" * 2000})
        session.messages.append({"role": "assistant", "content": f"回答{i} " + "字" * 2000})

    loop = AgentLoop(
        MockModel(),
        build_registry(),
        Gateway(),
        store,
        Settings(session_dir=sessions, max_context_tokens=3000),
    )
    editor = {"workspaceRoot": str(tmp_path), "currentFile": None, "selection": None, "openFiles": []}
    events: list[str] = []

    async def emit(kind: str, payload: dict) -> None:
        events.append(kind)

    await loop.run(session, "你好", editor, emit)

    # 压缩后应有摘要消息
    assert any(
        m.get("content", "").startswith("（历史摘要）") for m in session.messages
    ), "应触发自动压缩并写入摘要"
    # 摘要应落盘（JSONL 有 summary 行）
    path = store._path(session.id)
    assert path.exists()
    assert any(
        '"type": "summary"' in line for line in path.read_text(encoding="utf-8").splitlines()
    ), "摘要应落盘 JSONL"


async def test_loop_deadlock_detection(tmp_path: Path) -> None:
    """模型反复调用同一工具（无进展）→ 应被死循环检测停止，而不是跑满步数。"""
    sessions = str(tmp_path / "sessions")
    store = SessionStore(sessions)
    session = store.create_session()
    loop = AgentLoop(
        DeadlockModel(),
        build_registry(),
        Gateway(),
        store,
        Settings(session_dir=sessions),
    )
    editor = {"workspaceRoot": str(tmp_path), "currentFile": None, "selection": None, "openFiles": []}
    events: list[tuple[str, dict]] = []

    async def emit(kind: str, payload: dict) -> None:
        events.append((kind, payload))

    await loop.run(session, "hi", editor, emit)

    assert any(
        kind == "error" and "重复调用" in payload.get("message", "")
        for kind, payload in events
    ), "应触发重复调用检测"
    # 工具调用次数应很少（远小于 max_steps）
    tool_calls = sum(1 for k, _ in events if k == "tool_call")
    assert tool_calls <= 3, f"应尽早停止，实际调用了 {tool_calls} 次"
