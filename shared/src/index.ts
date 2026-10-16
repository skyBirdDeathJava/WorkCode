/**
 * WorkCode 通信协议（v1）
 *
 * 插件（TypeScript）与本地 Agent 服务（Python）之间通过 WebSocket 传输
 * JSON 消息。本文件是协议的 TypeScript 侧镜像；Python 侧在
 * agent-service/workcode/protocol.py 中用 pydantic 镜像同一份 JSON Schema
 * （见 docs/tech-design.md 第 5 节）。任何新增/修改必须两端同步。
 */

export const PROTOCOL_VERSION = 1 as const;

/** 插件 → 服务 */
export type ClientMessageType =
  | 'connect'
  | 'chat_request'
  | 'permission_response'
  | 'apply_diff'
  | 'terminal_exec_result'
  | 'list_sessions'
  | 'resume_session'
  | 'set_effort'
  | 'get_memory'
  | 'memory_add'
  | 'set_allowed'
  | 'cancel'
  | 'new_session';

/** 服务 → 插件 */
export type ServerMessageType =
  | 'ready'
  | 'session_started'
  | 'session_list'
  | 'session_resumed'
  | 'text_delta'
  | 'tool_call'
  | 'tool_result'
  | 'permission_request'
  | 'diff_preview'
  | 'terminal_exec'
  | 'reasoning_delta'
  | 'memory_content'
  | 'error'
  | 'done';

export interface FileContext {
  path: string;
  content: string;
  languageId?: string;
}

export interface SelectionInfo {
  text: string;
  /** 1-based，含起始行 */
  startLine: number;
  /** 1-based，含结束行 */
  endLine: number;
}

/** 编辑器上下文：每次聊天请求随消息一起发送给服务 */
export interface EditorContext {
  workspaceRoot: string;
  currentFile: FileContext | null;
  selection: SelectionInfo | null;
  openFiles: string[];
}

// ---------- 插件 → 服务 payload ----------

export interface ConnectPayload {
  apiKey?: string;
  model?: string;
  workspaceRoot?: string;
  /** 插件是否支持在 VS Code 集成终端里执行命令（run_command 桥） */
  terminalSupport?: boolean;
  /** 预批准命令前缀：匹配则不弹确认直接执行 */
  allowedCommands?: string[];
}

export interface ChatRequestPayload {
  content: string;
  editorContext: EditorContext;
}

export interface PermissionResponsePayload {
  requestId: string;
  allowed: boolean;
}

/** 插件 → 服务：用户接受/拒绝某次 Diff 修改建议 */
export interface ApplyDiffPayload {
  requestId: string;
  accepted: boolean;
}

// ---------- 服务 → 插件 payload ----------

export interface ReadyPayload {
  ok: boolean;
  message?: string;
}

export interface SessionStartedPayload {
  sessionId: string;
}

export interface TextDeltaPayload {
  delta: string;
}

export interface ToolCallPayload {
  requestId: string;
  tool: string;
  arguments: Record<string, unknown>;
}

export interface ToolResultPayload {
  requestId: string;
  tool: string;
  ok: boolean;
  output: string;
  error?: string;
  /** 写工具（write_file / edit_file）带目标文件路径 */
  path?: string;
}

export interface PermissionRequestPayload {
  requestId: string;
  tool: string;
  description: string;
  /** 工具参数，用于在确认弹窗里展示细节 */
  arguments?: Record<string, unknown>;
}

/** 服务 → 插件：写操作暂存后的 Diff 预览，等待用户接受/拒绝 */
export interface DiffPreviewPayload {
  requestId: string;
  path: string;
  originalContent: string;
  newContent: string;
}

/** 插件 → 服务：请求会话列表 / 恢复某个历史会话 */
export interface ListSessionsPayload {
  requestId?: string;
}

export interface ResumeSessionPayload {
  requestId?: string;
  sessionId: string;
}

export interface SessionItem {
  id: string;
  created_at: number;
  preview: string;
}

/** 服务 → 插件：会话列表（用于恢复选择器） */
export interface SessionListPayload {
  requestId?: string;
  sessions: SessionItem[];
}

/** 服务 → 插件：恢复的会话及其历史消息 */
export interface SessionResumedPayload {
  requestId?: string;
  sessionId: string;
  messages: Array<Record<string, unknown>>;
}

/** 服务 → 插件：请求在 VS Code 集成终端里执行命令 */
export interface TerminalExecPayload {
  requestId: string;
  command: string;
  cwd?: string;
}

/** 插件 → 服务：终端命令执行结果 */
export interface TerminalExecResultPayload {
  requestId: string;
  ok: boolean;
  output: string;
  exitCode?: number;
  /** 命令仍在运行（如 npm run dev 长驻进程） */
  stillRunning?: boolean;
}

export interface ErrorPayload {
  message: string;
}

export interface DonePayload {
  usage?: {
    promptTokens?: number;
    completionTokens?: number;
  };
}

/** 消息信封 */
export interface Envelope {
  type: string;
  sessionId?: string;
  payload: Record<string, unknown>;
}
