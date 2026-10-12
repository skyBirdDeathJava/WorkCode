"""复现「选中代码问问题无输出」：把 MockTest.vue 上下文原样发给真实模型，打印全部事件。"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("WORKCODE_MOCK", "0")

from fastapi.testclient import TestClient  # noqa: E402
from workcode.server import app  # noqa: E402

VUE_PATH = "/Users/tal/ww/gexue-front/src/views/MockTest.vue"


def main() -> int:
    content = Path(VUE_PATH).read_text(encoding="utf-8")
    lines = content.splitlines()
    editor = {
        "workspaceRoot": "/Users/tal/ww/gexue-front",
        "currentFile": {"path": VUE_PATH, "content": content, "languageId": "vue"},
        "selection": {
            "text": "\n".join(lines[190:198]),
            "startLine": 191,
            "endLine": 198,
        },
        "openFiles": [VUE_PATH],
    }

    client = TestClient(app)
    with client.websocket_connect("/ws") as ws:
        ws.send_json(
            {"type": "connect", "payload": {"apiKey": os.environ.get("DEEPSEEK_API_KEY", "")}}
        )
        ready = ws.receive_json()
        print("ready:", ready["type"])

        ws.send_json(
            {
                "type": "chat_request",
                "payload": {"content": "解释一下这段代码的意思", "editorContext": editor},
            }
        )

        text_parts: list[str] = []
        while True:
            m = ws.receive_json()
            t = m["type"]
            if t == "text_delta":
                text_parts.append(m["payload"]["delta"])
            elif t in ("error", "done"):
                if t == "error":
                    print("ERROR:", m["payload"]["message"])
                else:
                    print("DONE")
                break

    full = "".join(text_parts)
    print("=== 收到的文本总长 ===", len(full))
    Path("/tmp/workcode_resp.txt").write_text(full, encoding="utf-8")
    print("=== 文本前 800 字 ===")
    print(full[:800])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
