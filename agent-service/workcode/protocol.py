"""通信协议（Python 侧 pydantic 镜像）。

与 shared/src/index.ts 保持一一对应，源为 shared/ 下的 JSON Schema 设计。
新增/修改消息时两端必须同步。
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

# ---------- 编辑器上下文 ----------


class FileContext(BaseModel):
    path: str
    content: str
    languageId: Optional[str] = None


class SelectionInfo(BaseModel):
    text: str
    startLine: int = Field(description="1-based，含起始行")
    endLine: int = Field(description="1-based，含结束行")


class EditorContext(BaseModel):
    workspaceRoot: str = ""
    currentFile: Optional[FileContext] = None
    selection: Optional[SelectionInfo] = None
    openFiles: list[str] = Field(default_factory=list)


# ---------- 插件 → 服务 payload ----------


class ConnectPayload(BaseModel):
    apiKey: Optional[str] = None
    model: Optional[str] = None
    workspaceRoot: Optional[str] = None
    terminalSupport: Optional[bool] = None
    allowedCommands: Optional[list[str]] = None


class SetAllowedPayload(BaseModel):
    commands: list[str] = Field(default_factory=list)


class ChatRequestPayload(BaseModel):
    content: str
    editorContext: EditorContext = Field(default_factory=EditorContext)


class PermissionResponsePayload(BaseModel):
    requestId: str
    allowed: bool


class ApplyDiffPayload(BaseModel):
    requestId: str
    accepted: bool


class TerminalExecResultPayload(BaseModel):
    requestId: str
    ok: bool
    output: str
    exitCode: Optional[int] = None
    stillRunning: Optional[bool] = None


class ListSessionsPayload(BaseModel):
    requestId: Optional[str] = None


class ResumeSessionPayload(BaseModel):
    requestId: Optional[str] = None
    sessionId: str


class SetModePayload(BaseModel):
    mode: str  # manual | auto | plan


class SetModelPayload(BaseModel):
    model: str


class SetEffortPayload(BaseModel):
    effort: str  # none | low | high | max


class GetMemoryPayload(BaseModel):
    requestId: Optional[str] = None


class MemoryAddPayload(BaseModel):
    requestId: Optional[str] = None
    text: str


class InitMemoryPayload(BaseModel):
    requestId: Optional[str] = None


class MemoryContentPayload(BaseModel):
    requestId: Optional[str] = None
    content: str


class SessionItem(BaseModel):
    id: str
    created_at: float
    preview: str


class SessionListPayload(BaseModel):
    requestId: Optional[str] = None
    sessions: list[SessionItem] = Field(default_factory=list)


class SessionResumedPayload(BaseModel):
    requestId: Optional[str] = None
    sessionId: str
    messages: list[dict] = Field(default_factory=list)


# ---------- 服务 → 插件 payload ----------


class ReadyPayload(BaseModel):
    ok: bool
    message: Optional[str] = None
    model: Optional[str] = None
    mode: Optional[str] = None


class SessionStartedPayload(BaseModel):
    sessionId: str


class TextDeltaPayload(BaseModel):
    delta: str


class ToolCallPayload(BaseModel):
    requestId: str
    tool: str
    arguments: dict = Field(default_factory=dict)


class ToolResultPayload(BaseModel):
    requestId: str
    tool: str
    ok: bool
    output: str
    error: Optional[str] = None
    path: Optional[str] = None  # 写工具带目标文件路径


class PermissionRequestPayload(BaseModel):
    requestId: str
    tool: str
    description: str
    arguments: dict = Field(default_factory=dict)


class ErrorPayload(BaseModel):
    message: str


class ReasoningDeltaPayload(BaseModel):
    delta: str


class SubagentLogPayload(BaseModel):
    tool: str
    brief: str
    ok: Optional[bool] = None  # None=调用中，True/False=结果


class DiffPreviewPayload(BaseModel):
    requestId: str
    path: str
    originalContent: str
    newContent: str


class TerminalExecPayload(BaseModel):
    requestId: str
    command: str
    cwd: Optional[str] = None


class DonePayload(BaseModel):
    usage: Optional[dict] = None


# 各消息类型的 payload 模型
PAYLOAD_MODELS: dict[str, type[BaseModel]] = {
    "connect": ConnectPayload,
    "chat_request": ChatRequestPayload,
    "permission_response": PermissionResponsePayload,
    "apply_diff": ApplyDiffPayload,
    "terminal_exec_result": TerminalExecResultPayload,
    "list_sessions": ListSessionsPayload,
    "resume_session": ResumeSessionPayload,
    "set_mode": SetModePayload,
    "set_model": SetModelPayload,
    "set_effort": SetEffortPayload,
    "get_memory": GetMemoryPayload,
    "memory_add": MemoryAddPayload,
    "init_memory": InitMemoryPayload,
    "set_allowed": SetAllowedPayload,
    "memory_content": MemoryContentPayload,
    "subagent_log": SubagentLogPayload,
    "ready": ReadyPayload,
    "session_started": SessionStartedPayload,
    "session_list": SessionListPayload,
    "session_resumed": SessionResumedPayload,
    "text_delta": TextDeltaPayload,
    "tool_call": ToolCallPayload,
    "tool_result": ToolResultPayload,
    "permission_request": PermissionRequestPayload,
    "diff_preview": DiffPreviewPayload,
    "terminal_exec": TerminalExecPayload,
    "reasoning_delta": ReasoningDeltaPayload,
    "error": ErrorPayload,
    "done": DonePayload,
}
