"""M3 真实模型验证：让 DeepSeek 修改一个临时文件，自动允许权限。

观察模型是否会主动调用 edit_file / write_file，以及文件是否落盘修改。
模型行为有随机性，脚本只做观测并打印，不做硬断言。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("WORKCODE_MOCK", "0")

from fastapi.testclient import TestClient  # noqa: E402
from workcode.server import app  # noqa: E402


def main() -> int:
    tmp = Path(tempfile.mkdtemp())
    target = tmp / "greet.py"
    target.write_text(
        "def greet(name):\n    return f'Hi, {name}'\n",
        encoding="utf-8",
    )

    client = TestClient(app)
    with client.websocket_connect("/ws") as ws:
        ws.send_json(
            {"type": "connect", "payload": {"apiKey": os.environ.get("DEEPSEEK_API_KEY", "")}}
        )
        ws.receive_json()  # ready

        editor = {
            "workspaceRoot": str(tmp),
            "currentFile": {
                "path": str(target),
                "content": target.read_text(encoding="utf-8"),
                "languageId": "python",
            },
            "selection": None,
            "openFiles": [str(target)],
        }
        prompt = (
            f"请把 {target} 里 greet 函数的 return 改成 'Hello, {{name}}!'。"
            "用 edit_file 工具精确修改这一处即可，不要整文件重写。"
        )
        ws.send_json(
            {"type": "chat_request", "payload": {"content": prompt, "editorContext": editor}}
        )

        text = ""
        tool_calls = []
        tool_results = []
        diff_previews = []
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
                ws.send_json(
                    {
                        "type": "permission_response",
                        "payload": {"requestId": m["payload"]["requestId"], "allowed": True},
                    }
                )
            elif t == "diff_preview":
                diff_previews.append(m["payload"])
                ws.send_json(
                    {
                        "type": "apply_diff",
                        "payload": {"requestId": m["payload"]["requestId"], "accepted": True},
                    }
                )
            elif t == "error":
                print("ERROR:", m["payload"]["message"])
                break
            elif t == "done":
                break

    print("=== 模型文本回复 ===")
    print(text[:500])
    print("=== 工具调用 ===")
    for tc in tool_calls:
        print(" ", tc["tool"], json.dumps(tc["arguments"], ensure_ascii=False)[:300])
    print("=== Diff 预览 ===")
    for dp in diff_previews:
        print("  path:", dp["path"], "| orig", len(dp["originalContent"]), "字符 → new", len(dp["newContent"]), "字符")
    print("=== 工具结果 ===")
    for tr in tool_results:
        print(" ", tr["tool"], "ok=", tr["ok"], "|", tr["output"][:200])
    print("=== 落盘文件 ===")
    print(target.read_text(encoding="utf-8") if target.exists() else "(文件不存在)")
    print("工具调用数:", len(tool_calls), "| Diff 预览数:", len(diff_previews))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
