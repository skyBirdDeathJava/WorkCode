"""会话持久化：每个会话一个 JSON 文件。

MVP 阶段只做本地存储（消息历史、上下文快照、创建时间）。
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Session:
    id: str
    created_at: float
    messages: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "created_at": self.created_at,
            "messages": self.messages,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Session":
        return cls(
            id=data["id"],
            created_at=data.get("created_at", 0.0),
            messages=data.get("messages", []),
        )


class SessionStore:
    def __init__(self, directory: str):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, session_id: str) -> Path:
        return self.dir / f"{session_id}.json"

    def create_session(self) -> Session:
        session = Session(id=uuid.uuid4().hex[:12], created_at=time.time())
        self.save(session)
        return session

    def load(self, session_id: str) -> Session | None:
        path = self._path(session_id)
        if not path.exists():
            return None
        try:
            return Session.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            return None

    def save(self, session: Session) -> None:
        self._path(session.id).write_text(
            json.dumps(session.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
