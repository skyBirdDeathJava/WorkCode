"""冒烟测试：验证 WS 连接 → 聊天 → 流式回复 → done 全链路，含工具调用路径。

默认用 Mock 模型（无需 API Key）。要测真实 DeepSeek：先设置 DEEPSEEK_API_KEY，
再以 WORKCODE_MOCK=0 运行。

用法：
  .venv/bin/python scripts/smoke_test.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("WORKCODE_MOCK", "1")

from fastapi.testclient import TestClient  # noqa: E402
from workcode.server import app  # noqa: E402


def _chat(ws, content: str, editor=None, auto_accept: bool = True, reject_diffs: bool = False) -> dict:
    """发送一条 chat_request，收集事件直到 done/error。

    - auto_accept=True：自动允许权限、并接受 Diff（模拟“允许/接受”）
    - reject_diffs=True：显式拒绝 Diff（模拟“拒绝”，立即回包不等待超时）
    """
    ws.send_json(
        {
            "type": "chat_request",
            "payload": {
                "content": content,
                "editorContext": editor
                or {
                    "workspaceRoot": ".",
                    "currentFile": {
                        "path": "example.py",
                        "content": "def greet(name):\n    return f'hi, {name}'\n",
                        "languageId": "python",
                    },
                    "selection": {"text": "def greet", "startLine": 1, "endLine": 1},
                    "openFiles": ["example.py"],
                },
            },
        }
    )
    text = ""
    tool_calls = []
    tool_results = []
    permission_requests = []
    diff_previews = []
    terminal_execs = []
    while True:
        m = ws.receive_json()
        t = m["type"]
        if t == "text_delta":
            text += m["payload"]["delta"]
        elif t == "tool_call":
            tool_calls.append(m["payload"])
        elif t == "tool_result":
            tool_results.append(m["payload"])
        elif t == "permission_request":
            permission_requests.append(m["payload"])
            if auto_accept:
                ws.send_json(
                    {
                        "type": "permission_response",
                        "payload": {"requestId": m["payload"]["requestId"], "allowed": True},
                    }
                )
        elif t == "diff_preview":
            diff_previews.append(m["payload"])
            if auto_accept or reject_diffs:
                ws.send_json(
                    {
                        "type": "apply_diff",
                        "payload": {
                            "requestId": m["payload"]["requestId"],
                            "accepted": auto_accept,
                        },
                    }
                )
        elif t == "terminal_exec":
            terminal_execs.append(m["payload"])
            # 模拟扩展在 VS Code 终端执行后回传结果
            ws.send_json(
                {
                    "type": "terminal_exec_result",
                    "payload": {
                        "requestId": m["payload"]["requestId"],
                        "ok": True,
                        "output": "[终端桥] " + m["payload"]["command"][:60],
                        "exitCode": 0,
                    },
                }
            )
        elif t == "error":
            return {
                "ok": False,
                "error": m["payload"]["message"],
                "text": text,
                "tool_calls": tool_calls,
                "tool_results": tool_results,
                "permission_requests": permission_requests,
                "diff_previews": diff_previews,
                "terminal_execs": terminal_execs,
            }
        elif t == "done":
            break
    return {
        "ok": True,
        "text": text,
        "tool_calls": tool_calls,
        "tool_results": tool_results,
        "permission_requests": permission_requests,
        "diff_previews": diff_previews,
        "terminal_execs": terminal_execs,
    }


def main() -> int:
    client = TestClient(app)
    failures = 0

    # 1) 握手
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "connect", "payload": {"apiKey": os.environ.get("DEEPSEEK_API_KEY", "")}})
        ready = ws.receive_json()
        assert ready["type"] == "ready", ready
        print("✓ 握手 ready")

        # 2) 普通聊天（流式文本）
        r = _chat(ws, "你好，介绍一下你自己")
        assert r["ok"] and len(r["text"]) > 10, r
        print(f"✓ 流式回复: {r['text'][:60]}…")

        # 3) 工具调用路径（仅 Mock 模式触发 read_file；真实模型会自行决定是否用工具）
        if os.environ.get("WORKCODE_MOCK", "1") in ("1", "true"):
            sample_file = Path(__file__).resolve().parent / "tmp_sample.py"
            sample_file.write_text("def greet(name):\n    return f'hi, {name}'\n", encoding="utf-8")
            r2 = _chat(ws, f"!tool read_file {sample_file}")
            assert r2["ok"], r2
            assert len(r2["tool_calls"]) == 1 and r2["tool_calls"][0]["tool"] == "read_file", r2
            assert len(r2["tool_results"]) == 1 and r2["tool_results"][0]["ok"], r2
            assert "def greet" in r2["tool_results"][0]["output"], r2
            sample_file.unlink(missing_ok=True)
            print("✓ 工具调用（read_file）→ 结果写回历史 → 继续对话")

            write_target = Path(__file__).resolve().parent / "tmp_write.txt"
            write_target.unlink(missing_ok=True)
            r3 = _chat(ws, f"!tool write_file {write_target}")
            assert r3["ok"], r3
            assert write_target.exists(), "write_file 未落盘"
            assert write_target.read_text(encoding="utf-8").startswith("hello from WorkCode"), r3
            assert len(r3["diff_previews"]) >= 1, "write_file 应先发 Diff 预览"
            assert any(tr["tool"] == "write_file" and tr["ok"] for tr in r3["tool_results"]), r3
            write_target.unlink(missing_ok=True)
            print("✓ 写文件（write_file）→ Diff 预览 → 接受 → 落盘")

            # 拒绝路径：不接受 Diff，文件不应被修改
            write_target.write_text("原内容\n", encoding="utf-8")
            r4 = _chat(ws, f"!tool write_file {write_target}", auto_accept=False, reject_diffs=True)
            assert r4["ok"], r4
            assert write_target.read_text(encoding="utf-8") == "原内容\n", "拒绝后文件不应被修改"
            assert len(r4["diff_previews"]) >= 1, r4
            write_target.unlink(missing_ok=True)
            print("✓ 拒绝 Diff → 不落盘，Agent 收到「未接受」结果")

            # 执行命令（非白名单 → 权限弹窗 → 允许 → 执行 → 输出回传）
            r5 = _chat(ws, '!tool run_command python3 -c "print(\'hello from cmd\')"')
            assert r5["ok"], r5
            assert any(
                tr["tool"] == "run_command" and tr["ok"] and "hello from cmd" in tr["output"]
                for tr in r5["tool_results"]
            ), r5
            assert len(r5["permission_requests"]) >= 1, "非白名单命令应触发权限请求"
            print("✓ 执行命令（run_command）→ 权限确认 → 执行 → 输出回传")

            # 危险命令被拦截（无论模型是否尝试）
            r6 = _chat(ws, "!tool run_command rm -rf /")
            assert r6["ok"], r6
            assert any(tr["tool"] == "run_command" and not tr["ok"] for tr in r6["tool_results"]), r6
            print("✓ 危险命令（rm -rf /）被拦截")

            # 长驻终端：cd 状态跨命令保持
            r7 = _chat(ws, "!tool run_command cd /tmp")
            assert r7["ok"], r7
            r8 = _chat(ws, "!tool run_command pwd")
            assert r8["ok"], r8
            assert any(tr["tool"] == "run_command" and tr["ok"] and "/tmp" in tr["output"] for tr in r8["tool_results"]), r8
            print("✓ 长驻终端：cd /tmp → 后续 pwd 保持 /tmp")
        else:
            print("· 跳过工具调用路径（真实模式）")

        # 4) 新会话
        ws.send_json({"type": "new_session", "payload": {}})
        sess = ws.receive_json()

    # 5) 终端桥：声明 terminalSupport，run_command 走 VS Code 终端（模拟扩展回传）
    if os.environ.get("WORKCODE_MOCK", "1") in ("1", "true"):
        with client.websocket_connect("/ws") as ws2:
            ws2.send_json({"type": "connect", "payload": {"terminalSupport": True}})
            ws2.receive_json()
            rb = _chat(ws2, "!tool run_command echo bridge-test")
            assert rb["ok"], rb
            assert len(rb["terminal_execs"]) >= 1, "应发出 terminal_exec 请求"
            assert any(
                tr["tool"] == "run_command" and tr["ok"] and "[终端桥]" in tr["output"]
                for tr in rb["tool_results"]
            ), rb
            print("✓ 终端桥：run_command → terminal_exec → 扩展执行 → 结果回传")
    else:
        print("· 跳过终端桥（真实模式）")
        assert sess["type"] == "session_started", sess
        print(f"✓ 新会话 {sess['payload']['sessionId']}")

    print("SMOKE TEST PASSED" if failures == 0 else "SMOKE TEST FAILED")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
