"""FastAPI 服务：提供 /ws WebSocket 端点，承接插件的聊天请求。

消息走「写入队列 → 单 writer 发送」模式，避免多个协程并发 send 同一连接。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from .agent import context
from .agent.loop import AgentLoop
from .agent.model import DeepSeekModel, MockModel
from .config import Settings, load_settings
from .gateway import Gateway
from .session.store import Session, SessionStore
from .tools import build_registry
from .tools.cmd import TerminalSession

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")


def make_app(settings: Settings) -> FastAPI:
    store = SessionStore(settings.session_dir)
    registry = build_registry()

    app = FastAPI(title="WorkCode Agent Service", version="0.1.0")

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True, "model": settings.model, "mock": settings.mock}

    @app.websocket("/ws")
    async def ws_endpoint(websocket: WebSocket) -> None:
        await websocket.accept()

        out: asyncio.Queue = asyncio.Queue()

        async def writer() -> None:
            while True:
                item = await out.get()
                if item is None:
                    break
                await websocket.send_text(item)

        writer_task = asyncio.create_task(writer())

        session: Optional[Session] = None
        task: Optional[asyncio.Task] = None
        gateway = Gateway()
        # 每个连接一个长驻终端会话（cwd/环境变量跨命令保持）
        terminal = TerminalSession()
        api_key: str = settings.api_key
        model_name: str = settings.model
        effort: str = settings.effort
        mode = "manual"  # manual | auto | plan
        terminal_supported = False  # 插件连接时声明，决定 run_command 是否桥接到 VS Code 终端

        def session_id() -> Optional[str]:
            return session.id if session else None

        async def emit(kind: str, payload: dict[str, Any]) -> None:
            await out.put(json.dumps({"type": kind, "sessionId": session_id(), "payload": payload}))

        def build_loop() -> AgentLoop:
            model = MockModel() if settings.mock else DeepSeekModel(api_key=api_key, model=model_name, effort=effort)
            return AgentLoop(
                model, registry, gateway, store, settings,
                terminal=terminal, terminal_supported=terminal_supported, mode=mode,
            )

        try:
            while True:
                raw = await websocket.receive_text()
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    await emit("error", {"message": "无效的 JSON 消息"})
                    continue

                kind = msg.get("type")
                payload = msg.get("payload") or {}

                if kind == "connect":
                    if payload.get("apiKey"):
                        api_key = payload["apiKey"]
                    if payload.get("model"):
                        model_name = payload["model"]
                    if payload.get("terminalSupport"):
                        terminal_supported = True
                    await emit(
                        "ready",
                        {
                            "ok": True,
                            "message": f"WorkCode 服务就绪（model={model_name}, mock={settings.mock}）",
                            "model": model_name,
                            "mode": mode,
                        },
                    )

                elif kind == "set_mode":
                    new_mode = payload.get("mode", "")
                    if new_mode in ("manual", "auto", "plan"):
                        mode = new_mode
                        await emit("ready", {"ok": True, "message": f"权限模式已切换：{mode}", "model": model_name, "mode": mode})

                elif kind == "set_model":
                    new_model = payload.get("model", "").strip()
                    if new_model:
                        model_name = new_model
                        await emit("ready", {"ok": True, "message": f"模型已切换：{model_name}", "model": model_name, "mode": mode})

                elif kind == "set_effort":
                    new_effort = payload.get("effort", "")
                    if new_effort in ("none", "low", "high", "max"):
                        effort = new_effort
                        await emit("ready", {"ok": True, "message": f"思考程度已切换：{effort}", "model": model_name, "mode": mode})

                elif kind == "get_memory":
                    content = context.load_user_memory(settings.memory_file)
                    await emit("memory_content", {"requestId": payload.get("requestId", ""), "content": content})

                elif kind == "memory_add":
                    text = payload.get("text", "").strip()
                    if text:
                        context.append_user_memory(settings.memory_file, text)
                    content = context.load_user_memory(settings.memory_file)
                    await emit("memory_content", {"requestId": payload.get("requestId", ""), "content": content})

                elif kind == "new_session":
                    if task is not None and not task.done():
                        task.cancel()
                    session = store.create_session()
                    await emit("session_started", {"sessionId": session.id})

                elif kind == "chat_request":
                    if task is not None and not task.done():
                        task.cancel()
                    if session is None:
                        session = store.create_session()
                        await emit("session_started", {"sessionId": session.id})
                    content = payload.get("content", "")
                    editor_ctx = payload.get("editorContext") or {}
                    loop = build_loop()
                    task = asyncio.create_task(loop.run(session, content, editor_ctx, emit))

                elif kind == "permission_response":
                    gateway.resolve(payload.get("requestId", ""), bool(payload.get("allowed", False)))

                elif kind == "apply_diff":
                    gateway.resolve_diff(payload.get("requestId", ""), bool(payload.get("accepted", False)))

                elif kind == "terminal_exec_result":
                    gateway.resolve_terminal(payload.get("requestId", ""), payload)

                elif kind == "list_sessions":
                    await emit(
                        "session_list",
                        {"requestId": payload.get("requestId", ""), "sessions": store.list_sessions(20)},
                    )

                elif kind == "resume_session":
                    loaded = store.load(payload.get("sessionId", ""))
                    if loaded is not None:
                        session = loaded
                        await emit(
                            "session_resumed",
                            {
                                "requestId": payload.get("requestId", ""),
                                "sessionId": session.id,
                                "messages": session.messages,
                            },
                        )
                    else:
                        await emit("error", {"message": f"会话不存在：{payload.get('sessionId', '')}"})

                elif kind == "cancel":
                    if task is not None and not task.done():
                        task.cancel()

        except WebSocketDisconnect:
            pass
        except Exception as exc:  # noqa: BLE001
            logger.exception("websocket handler error: %s", exc)
        finally:
            if task is not None and not task.done():
                task.cancel()
            await terminal.close()  # 关闭长驻终端，释放子进程
            await out.put(None)
            if not writer_task.done():
                writer_task.cancel()

    return app


settings = load_settings()
app = make_app(settings)
