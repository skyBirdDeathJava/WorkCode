"""M5 真实模型验证：让 DeepSeek 调用 run_command，自动允许权限。

观测模型是否会主动调用 run_command、权限请求与执行结果。
模型行为有随机性，脚本只做观测并打印，不做硬断言。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("WORKCODE_MOCK", "0")

from fastapi.testclient import TestClient  # noqa: E402
from workcode.server import app  # noqa: E402


def main() -> int:
    client = TestClient(app)
    with client.websocket_connect("/ws") as ws:
        ws.send_json(
            {"type": "connect", "payload": {"apiKey": os.environ.get("DEEPSEEK_API_KEY", "")}}
        )
        ws.receive_json()  # ready

        editor = {
            "workspaceRoot": ".",
            "currentFile": None,
            "selection": None,
            "openFiles": [],
        }
        prompt = (
            "请使用 run_command 工具执行 `python3 --version`，然后把结果告诉我。"
            "只运行这一条命令即可。"
        )
        ws.send_json(
            {"type": "chat_request", "payload": {"content": prompt, "editorContext": editor}}
        )

        text = ""
        tool_calls = []
        tool_results = []
        permissions = []
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
                permissions.append(m["payload"])
                ws.send_json(
                    {
                        "type": "permission_response",
                        "payload": {"requestId": m["payload"]["requestId"], "allowed": True},
                    }
                )
            elif t == "error":
                print("ERROR:", m["payload"]["message"])
                break
            elif t == "done":
                break

    print("=== 模型文本回复 ===")
    print(text[:600])
    print("=== 工具调用 ===")
    for tc in tool_calls:
        print(" ", tc["tool"], json.dumps(tc["arguments"], ensure_ascii=False)[:300])
    print("=== 权限请求 ===")
    for p in permissions:
        print(" ", p["tool"], "|", p["description"][:120])
    print("=== 工具结果 ===")
    for tr in tool_results:
        print(" ", tr["tool"], "ok=", tr["ok"], "|", tr["output"][:300])
    print("工具调用数:", len(tool_calls), "| 权限请求数:", len(permissions))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
