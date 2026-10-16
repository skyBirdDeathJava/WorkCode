"""会话持久化：JSONL 追加式（对齐 Claude Code 的 transcript 机制）。

每个会话一个 `.jsonl` 文件，追加写入，崩溃/中断只丢当前未写完的消息。
- meta 行：会话元数据（id、创建时间）
- message 行：一条对话消息（完整 dict，round-trip）
- summary 行：上下文压缩摘要（长会话时写入，参与后续上下文）

`load()` 按行重建：旧消息 + 摘要 + 新消息；压缩时旧消息会被
build_messages 优先裁剪，摘要存活提供精简历史。
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

SUMMARY_PREFIX = "（历史摘要）"


@dataclass
class Session:
    id: str
    created_at: float
    messages: list[dict] = field(default_factory=list)


class SessionStore:
    def __init__(self, directory: str):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, session_id: str) -> Path:
        return self.dir / f"{session_id}.jsonl"

    # ---------- 写入 ----------

    def create_session(self) -> Session:
        session = Session(id=uuid.uuid4().hex[:12], created_at=time.time())
        self._write_line(
            session.id,
            {"type": "meta", "id": session.id, "created_at": session.created_at},
        )
        return session

    def append(self, session_id: str, message: dict) -> None:
        """追加一条对话消息（逐条落盘，崩溃不丢已写入部分）。"""
        self._write_line(session_id, {"type": "message", "message": message})

    def append_summary(self, session_id: str, content: str) -> None:
        self._write_line(
            session_id,
            {"type": "summary", "content": content, "created_at": time.time()},
        )

    def _write_line(self, session_id: str, obj: dict) -> None:
        path = self._path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")

    # ---------- 读取 ----------

    def load(self, session_id: str) -> Session | None:
        path = self._path(session_id)
        if not path.exists():
            return None
        session = None
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                obj = json.loads(line)
                t = obj.get("type")
                if t == "meta":
                    session = Session(id=obj.get("id", session_id), created_at=obj.get("created_at", 0.0))
                elif t == "message" and session is not None:
                    session.messages.append(obj.get("message", {}))
                elif t == "summary" and session is not None:
                    # 历史对话摘要作为 system 消息参与上下文；旧消息会被优先裁剪，摘要存活
                    session.messages.append(
                        {"role": "system", "content": SUMMARY_PREFIX + obj.get("content", "")}
                    )
        except Exception:
            return None
        if session is None:
            session = Session(id=session_id, created_at=0.0)
        return session
    # 历史会话列表：每个会话只返回「第一条用户消息」作为预览，点击某个会话后再 load 全部消息。
    def list_sessions(self, limit: int = 20) -> list[dict]:
        """按创建时间倒序列出会话：id、created_at、首条用户消息预览。"""
        items: list[dict] = []
        for path in self.dir.glob("*.jsonl"):
            try:
                meta, preview = None, "" #meta（id、创建时间）+ 第一条用户消息（预览）
                for line in path.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    obj = json.loads(line)
                    t = obj.get("type")
                    if t == "meta":
                        meta = obj
                    elif t == "message" and not preview:
                        msg = obj.get("message", {})
                        if msg.get("role") == "user":
                            preview = str(msg.get("content", ""))[:120]
                if meta:
                    items.append(
                        {
                            "id": meta.get("id", path.stem),
                            "created_at": meta.get("created_at", 0.0),
                            "preview": preview or "(空会话)",
                        }
                    )
            except Exception:
                continue
        items.sort(key=lambda it: it["created_at"], reverse=True)
        return items[:limit]
