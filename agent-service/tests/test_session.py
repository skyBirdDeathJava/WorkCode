"""会话持久化（JSONL）、压缩摘要、恢复的单元测试。"""

from __future__ import annotations

import pytest
from pathlib import Path

from workcode.agent.context import compact, estimate_total_tokens
from workcode.agent.model import MockModel
from workcode.session.store import SUMMARY_PREFIX, SessionStore


def test_jsonl_roundtrip(tmp_path: Path) -> None:
    store = SessionStore(str(tmp_path / "sessions"))
    s = store.create_session()
    store.append(s.id, {"role": "user", "content": "你好"})
    store.append(s.id, {"role": "assistant", "content": "回复"})

    loaded = store.load(s.id)
    assert loaded is not None
    assert loaded.id == s.id
    assert [m["content"] for m in loaded.messages] == ["你好", "回复"]


def test_list_sessions_preview_and_order(tmp_path: Path) -> None:
    store = SessionStore(str(tmp_path / "sessions"))
    s1 = store.create_session()
    store.append(s1.id, {"role": "user", "content": "第一个问题"})
    s2 = store.create_session()
    store.append(s2.id, {"role": "user", "content": "第二个问题"})

    items = store.list_sessions()
    assert len(items) == 2
    assert items[0]["preview"].startswith("第二个问题")  # 最新在前
    assert items[0]["created_at"] >= items[1]["created_at"]
    assert items[0]["id"] in (s1.id, s2.id)


def test_summary_loads_as_system_message(tmp_path: Path) -> None:
    store = SessionStore(str(tmp_path / "sessions"))
    s = store.create_session()
    store.append_summary(s.id, "之前讨论了某话题")

    loaded = store.load(s.id)
    assert loaded is not None
    assert loaded.messages[0]["role"] == "system"
    assert loaded.messages[0]["content"].startswith(SUMMARY_PREFIX)


@pytest.mark.asyncio
async def test_compact_over_budget() -> None:
    messages: list[dict] = []
    for i in range(3):
        messages.append({"role": "user", "content": f"问题{i} " + "字" * 3000})
        messages.append({"role": "assistant", "content": f"回答{i} " + "字" * 3000})

    new_messages, summary = await compact(messages, MockModel(), max_tokens=2000)
    assert new_messages is not None
    assert summary
    assert any(
        m.get("content", "").startswith(SUMMARY_PREFIX) for m in new_messages
    )
    assert estimate_total_tokens(new_messages) < estimate_total_tokens(messages)


@pytest.mark.asyncio
async def test_compact_under_budget_noop() -> None:
    messages = [{"role": "user", "content": "短问题"}]
    new_messages, summary = await compact(messages, MockModel(), max_tokens=20000)
    assert new_messages is None
    assert summary == ""


def test_load_project_memory_priority(tmp_path: Path) -> None:
    from workcode.agent.context import load_project_memory

    (tmp_path / "WorkCode.md").write_text("记忆A", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("记忆B", encoding="utf-8")
    # WorkCode.md 优先
    assert "记忆A" in load_project_memory(str(tmp_path))
    # 只有 CLAUDE.md 时用 CLAUDE.md
    (tmp_path / "WorkCode.md").unlink()
    assert "记忆B" in load_project_memory(str(tmp_path))
    # 都没有 → 空
    (tmp_path / "CLAUDE.md").unlink()
    assert load_project_memory(str(tmp_path)) == ""


def test_build_messages_injects_memory(tmp_path: Path) -> None:
    from workcode.agent.context import build_messages, SYSTEM_PROMPT

    (tmp_path / "WorkCode.md").write_text("遵守测试规范", encoding="utf-8")
    messages = build_messages(
        [{"role": "user", "content": "你好"}],
        {"workspaceRoot": str(tmp_path), "currentFile": None, "selection": None, "openFiles": []},
        max_tokens=20000,
    )
    system = messages[0]["content"]
    assert "项目记忆" in system
    assert "遵守测试规范" in system


def test_render_user_message_resolves_at_refs(tmp_path: Path) -> None:
    from workcode.agent.context import render_user_message

    (tmp_path / "greet.py").write_text("def greet(): pass\n", encoding="utf-8")
    ctx = {"workspaceRoot": str(tmp_path), "currentFile": None, "selection": None, "openFiles": []}

    rendered = render_user_message("请看看 @greet.py 这个文件", ctx)
    assert "【引用文件】greet.py" in rendered
    assert "def greet(): pass" in rendered


def test_render_user_message_ignores_missing_ref(tmp_path: Path) -> None:
    from workcode.agent.context import render_user_message

    ctx = {"workspaceRoot": str(tmp_path), "currentFile": None, "selection": None, "openFiles": []}
    rendered = render_user_message("联系 @nobody 一下", ctx)
    assert "【引用文件】" not in rendered
