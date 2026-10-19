"""生命周期 Hooks（对齐 Claude Code 的 hooks 机制）。

在工作区 `.workcode/hooks.json` 里配置「事件 → shell 命令」，Agent 在对应节点执行：

```json
{
  "UserPromptSubmit": "echo '收到用户消息'",
  "PreToolUse": "test -f .lint-staged && exit 0 || exit 2",
  "PostToolUse": "npx prettier --write \"$WORKCODE_PATH\"",
  "Stop": "echo '对话结束'"
}
```

- `PreToolUse` 退出码 2 → 拦截该工具（当作权限拒绝）；0/其他 → 放行
- 事件命令可通过环境变量拿到上下文：WORKCODE_TOOL / WORKCODE_ARGS / WORKCODE_PATH
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

DEFAULT_HOOKS_PATH = ".workcode/hooks.json"


def load_hooks(workspace_root: str) -> dict:
    """读工作区 .workcode/hooks.json，返回 {事件: 命令}。"""
    if not workspace_root:
        return {}
    p = Path(workspace_root) / DEFAULT_HOOKS_PATH
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return {k: v for k, v in data.items() if isinstance(v, str)}
    except Exception:
        return {}


async def run_hook(hooks: dict, event: str, env: dict | None = None) -> int:
    """执行某个事件绑定的命令，返回退出码（没配置返回 0）。"""
    cmd = hooks.get(event)
    if not cmd:
        return 0
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, **(env or {})},
        )
        await proc.communicate()
        return proc.returncode or 0
    except Exception:
        return 0
