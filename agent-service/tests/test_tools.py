"""工具层与权限闸门单测。"""

from __future__ import annotations

from pathlib import Path

import pytest

from workcode.gateway import Gateway
from workcode.tools import build_registry, write
from workcode.tools.registry import ToolRegistry

TMP = Path(__file__).parent / "tmp"


async def _run(registry: ToolRegistry, name: str, args: dict) -> tuple[bool, str]:
    return await registry.execute(name, args)


@pytest.mark.asyncio
async def test_read_file_existing() -> None:
    registry = build_registry()
    sample = TMP / "sample.txt"
    sample.parent.mkdir(parents=True, exist_ok=True)
    sample.write_text("line1\nline2\nline3\n", encoding="utf-8")

    ok, out = await _run(registry, "read_file", {"path": str(sample)})
    assert ok
    assert "line2" in out and "3" in out


@pytest.mark.asyncio
async def test_read_file_missing() -> None:
    registry = build_registry()
    ok, out = await _run(registry, "read_file", {"path": str(TMP / "nope.txt")})
    assert not ok
    assert "不存在" in out


@pytest.mark.asyncio
async def test_list_directory_and_search() -> None:
    registry = build_registry()
    ok, out = await _run(registry, "list_directory", {"path": str(TMP)})
    assert ok
    assert "sample.txt" in out

    ok, out = await _run(registry, "search_files", {"query": "line2", "path": str(TMP)})
    assert ok
    assert "sample.txt" in out


@pytest.mark.asyncio
async def test_write_file_creates_and_overwrites() -> None:
    registry = build_registry()
    target = TMP / "new.txt"
    target.unlink(missing_ok=True)

    ok, out = await _run(registry, "write_file", {"path": str(target), "content": "hello\n"})
    assert ok and target.read_text(encoding="utf-8") == "hello\n"
    assert "新建" in out

    ok, out = await _run(registry, "write_file", {"path": str(target), "content": "world\n"})
    assert ok and target.read_text(encoding="utf-8") == "world\n"
    assert "覆盖" in out


@pytest.mark.asyncio
async def test_edit_file_exact_replace() -> None:
    registry = build_registry()
    target = TMP / "edit.txt"
    target.write_text("a\nb\na\n", encoding="utf-8")

    # oldText 出现两次、未指定 replaceAll → 拒绝，防止误替换
    ok, out = await _run(
        registry, "edit_file", {"path": str(target), "oldText": "a", "newText": "x"}
    )
    assert not ok
    assert "2 次" in out
    assert target.read_text(encoding="utf-8") == "a\nb\na\n", "不应被修改"

    # 指定 replaceAll → 全部替换
    ok, out = await _run(
        registry,
        "edit_file",
        {"path": str(target), "oldText": "a", "newText": "x", "replaceAll": True},
    )
    assert ok
    assert target.read_text(encoding="utf-8") == "x\nb\nx\n"

    # 未找到 → 报错
    ok, out = await _run(
        registry, "edit_file", {"path": str(target), "oldText": "zzz", "newText": "y"}
    )
    assert not ok
    assert "未找到" in out


def test_preview_write_file_does_not_touch_disk(tmp_path: Path) -> None:
    target = tmp_path / "pv.txt"
    target.write_text("old\n", encoding="utf-8")
    ok, original, new, error = write.preview_write_file(str(target), "new\n")
    assert ok and original == "old\n" and new == "new\n" and not error
    # 磁盘不变
    assert target.read_text(encoding="utf-8") == "old\n"


def test_preview_edit_file_replaces_without_disk(tmp_path: Path) -> None:
    target = tmp_path / "pv_edit.txt"
    target.write_text("a\nb\n", encoding="utf-8")
    ok, original, new, error = write.preview_edit_file(str(target), "a", "x")
    assert ok and original == "a\nb\n" and new == "x\nb\n" and not error
    assert target.read_text(encoding="utf-8") == "a\nb\n"


def test_preview_edit_file_missing() -> None:
    ok, _orig, _new, error = write.preview_edit_file("/no/such/file.py", "a", "b")
    assert not ok
    assert "不存在" in error


def test_gateway_read_allow() -> None:
    g = Gateway()
    assert g.decide("read_file", {"path": "/x/y.py"}, "/") == "allow"
    assert g.decide("search_files", {}, "/") == "allow"


def test_gateway_write_ask_and_deny() -> None:
    g = Gateway()
    # 工作区内 → ask
    assert g.decide("write_file", {"path": "/ws/x.py"}, "/ws") == "ask"
    assert g.decide("edit_file", {"path": "/ws/x.py"}, "/ws") == "ask"
    # 工作区外 → deny
    assert g.decide("write_file", {"path": "/tmp/x.py"}, "/ws") == "deny"
    assert g.decide("edit_file", {"path": "/tmp/x.py"}, "/ws") == "deny"
    # 无工作区 → deny（安全默认）
    assert g.decide("write_file", {"path": "/anything/x.py"}, "") == "deny"


def test_gateway_command_blacklist() -> None:
    g = Gateway()
    # 危险命令 → deny
    assert g.decide("run_command", {"command": "rm -rf /"}, "/") == "deny"
    assert g.decide("run_command", {"command": "sudo reboot"}, "/") == "deny"
    assert g.decide("run_command", {"command": "git push --force origin main"}, "/") == "deny"
    # 安全只读命令 → allow
    assert g.decide("run_command", {"command": "ls -la"}, "/") == "allow"
    assert g.decide("run_command", {"command": "go build ./..."}, "/") == "allow"
    assert g.decide("run_command", {"command": "npm test"}, "/") == "allow"
    # 其他 → ask
    assert g.decide("run_command", {"command": "uptime"}, "/") == "ask"


@pytest.mark.asyncio
async def test_run_command_executes() -> None:
    registry = build_registry()
    ok, out = await _run(registry, "run_command", {"command": "echo hello-from-cmd"})
    assert ok
    assert "hello-from-cmd" in out


@pytest.mark.asyncio
async def test_run_command_nonexistent() -> None:
    registry = build_registry()
    ok, out = await _run(registry, "run_command", {"command": "definitely_not_a_command_xyz"})
    assert not ok
    assert "退出码" in out


@pytest.mark.asyncio
async def test_run_command_blacklist_rejected() -> None:
    registry = build_registry()
    ok, out = await _run(registry, "run_command", {"command": "rm -rf /"})
    assert not ok
    assert "拦截" in out


@pytest.mark.asyncio
async def test_run_command_timeout() -> None:
    registry = build_registry()
    ok, out = await _run(registry, "run_command", {"command": "sleep 5", "timeout": 1})
    assert not ok
    assert "超时" in out


@pytest.mark.asyncio
async def test_terminal_session_cwd_persists(tmp_path: Path) -> None:
    from workcode.tools.cmd import TerminalSession

    t = TerminalSession(cwd=str(tmp_path))
    try:
        ok, out = await t.run("pwd")
        assert ok and str(tmp_path) in out

        ok, out = await t.run("cd .. && pwd")
        assert ok and str(tmp_path.parent) in out

        # cwd 状态保持
        ok, out = await t.run("pwd")
        assert ok and str(tmp_path.parent) in out
    finally:
        await t.close()


@pytest.mark.asyncio
async def test_terminal_session_env_persists(tmp_path: Path) -> None:
    from workcode.tools.cmd import TerminalSession

    t = TerminalSession(cwd=str(tmp_path))
    try:
        ok, _ = await t.run("export MY_WC_TEST=hello123")
        assert ok
        ok, out = await t.run("echo $MY_WC_TEST")
        assert ok and "hello123" in out
    finally:
        await t.close()


@pytest.mark.asyncio
async def test_terminal_session_exit_code(tmp_path: Path) -> None:
    from workcode.tools.cmd import TerminalSession

    t = TerminalSession(cwd=str(tmp_path))
    try:
        ok, out = await t.run("exit 3")
        assert not ok
        assert "退出码 3" in out
        ok, _ = await t.run("true")
        assert ok
    finally:
        await t.close()


@pytest.mark.asyncio
async def test_terminal_session_timeout_resets(tmp_path: Path) -> None:
    from workcode.tools.cmd import TerminalSession

    t = TerminalSession(cwd=str(tmp_path))
    try:
        ok, out = await t.run("sleep 5", timeout=1)
        assert not ok
        assert "超时" in out
        # 超时后 shell 被重置，仍可继续使用
        ok, out = await t.run("echo alive")
        assert ok and "alive" in out
    finally:
        await t.close()


@pytest.mark.asyncio
async def test_terminal_interactive_input(tmp_path: Path) -> None:
    """交互式命令：read 读取 input 提供的输入。"""
    from workcode.tools.cmd import TerminalSession

    t = TerminalSession(cwd=str(tmp_path))
    try:
        ok, out = await t.run("read X && echo got=$X", input="hello-pty")
        assert ok
        assert "got=hello-pty" in out
    finally:
        await t.close()


@pytest.mark.asyncio
async def test_terminal_isatty(tmp_path: Path) -> None:
    """pty 让程序拿到真实 tty（isatty 返回 True）。"""
    from workcode.tools.cmd import TerminalSession

    t = TerminalSession(cwd=str(tmp_path))
    try:
        ok, out = await t.run("python3 -c 'import sys; print(sys.stdin.isatty())'")
        assert ok
        assert "True" in out
    finally:
        await t.close()


def test_strip_ansi() -> None:
    from workcode.tools.cmd import _strip_ansi

    dirty = "Python\x1b[1;35m>>> \x1b[0m\x1b[?2004h5\x1b[?2004l\n"
    clean = _strip_ansi(dirty)
    assert "\x1b" not in clean
    assert "5" in clean
    assert ">>>" in clean


@pytest.mark.asyncio
async def test_terminal_interactive_repl(tmp_path: Path) -> None:
    """交互式 REPL：input 喂指令，输出不含 ANSI 噪音。"""
    from workcode.tools.cmd import TerminalSession

    t = TerminalSession(cwd=str(tmp_path))
    try:
        ok, out = await t.run("python3 -i", input="print(2+3)\nexit()\n")
        assert ok
        assert "5" in out
        assert "\x1b" not in out  # ANSI 已被剥离
    finally:
        await t.close()
