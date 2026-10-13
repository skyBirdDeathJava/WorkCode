import * as child_process from 'child_process';
import * as fs from 'fs';
import * as path from 'path';
import * as vscode from 'vscode';
import { WebSocket } from 'ws';
import type { EditorContext } from '@workcode/shared';

export interface ServerEvent {
  type: string;
  sessionId?: string;
  payload: Record<string, unknown>;
}

const WS_OPEN = 1;

/**
 * 与本地 Agent 服务（Python）通信的 WebSocket 客户端。
 * 负责：连接探测 → 拉起服务 → 握手（携带 API Key）→ 消息转发 → 断线重连。
 */
export class ServiceClient extends vscode.Disposable {
  private ws: WebSocket | null = null;
  private child: child_process.ChildProcess | null = null;
  private started = false;
  private connecting = false;
  private reconnecting = false;
  private readonly port: number;
  private readonly eventHandlers: Array<(e: ServerEvent) => void> = [];
  private readonly pendingRequests = new Map<string, (payload: Record<string, unknown>) => void>();

  constructor(private readonly ctx: vscode.ExtensionContext) {
    super(() => this.dispose());
    this.port = vscode.workspace
      .getConfiguration('workcode')
      .get<number>('servicePort', 4877);
  }

  private get url(): string {
    return `ws://127.0.0.1:${this.port}/ws`;
  }

  /** 确保服务在线：可重试。已连接直接返回；否则探活/拉起进程并等待。 */
  async ensureRunning(): Promise<void> {
    if (this.ws && this.ws.readyState === WS_OPEN) return;
    if (this.connecting) return;
    this.connecting = true;
    try {
      // 服务可能已在运行（上次启动慢、或别的窗口拉起了）
      if (await this.tryConnect()) return;
      if (!this.started) {
        this.started = true;
        await this.spawnService();
      }
      // 首次冷启动可能较慢（导入 litellm 等），给足时间
      await this.waitForConnect(20_000);
    } finally {
      this.connecting = false;
    }
  }

  sendChat(content: string, editorContext: EditorContext): void {
    this.send('chat_request', { content, editorContext });
  }

  cancel(): void {
    this.send('cancel', {});
  }

  newSession(): void {
    this.send('new_session', {});
  }

  /** 请求-响应：带 requestId 的往返，服务端在响应里回显 requestId。请求前先确保连接。 */
  async request(type: string, payload: Record<string, unknown>): Promise<Record<string, unknown> | undefined> {
    try {
      await this.ensureRunning();
    } catch {
      // 连不上时仍走 send（会提示），让请求自行超时
    }
    return new Promise((resolve) => {
      const requestId = 'req_' + Date.now() + '_' + Math.random().toString(36).slice(2, 8);
      this.pendingRequests.set(requestId, resolve);
      this.send(type, { ...payload, requestId });
      setTimeout(() => {
        if (this.pendingRequests.delete(requestId)) resolve(undefined);
      }, 10_000);
    });
  }

  async listSessions(): Promise<Array<{ id: string; created_at: number; preview: string }> | undefined> {
    const resp = await this.request('list_sessions', {});
    const sessions = resp?.sessions;
    return Array.isArray(sessions) ? (sessions as Array<{ id: string; created_at: number; preview: string }>) : undefined;
  }

  async resumeSession(sessionId: string): Promise<void> {
    await this.request('resume_session', { sessionId });
  }

  /** 回复服务的权限请求（允许 / 拒绝）。 */
  respondPermission(requestId: string, allowed: boolean): void {
    this.send('permission_response', { requestId, allowed });
  }

  /** 回复服务的 Diff 确认（接受 / 拒绝该修改建议）。 */
  respondApplyDiff(requestId: string, accepted: boolean): void {
    this.send('apply_diff', { requestId, accepted });
  }

  /** 回复服务的终端命令执行结果。 */
  respondTerminalExecResult(
    requestId: string,
    result: { ok: boolean; output: string; exitCode?: number; stillRunning?: boolean }
  ): void {
    this.send('terminal_exec_result', { requestId, ...result });
  }

  onEvent(handler: (e: ServerEvent) => void): void {
    this.eventHandlers.push(handler);
  }

  // ---------- 连接 ----------

  private async tryConnect(): Promise<boolean> {
    try {
      const ws = await this.connect();
      await this.attach(ws);
      return true;
    } catch {
      return false;
    }
  }

  private connect(): Promise<WebSocket> {
    return new Promise((resolve, reject) => {
      const ws = new WebSocket(this.url);
      const timer = setTimeout(() => {
        ws.terminate();
        reject(new Error('连接超时'));
      }, 3_000);
      ws.once('open', () => {
        clearTimeout(timer);
        resolve(ws);
      });
      ws.once('error', (err) => {
        clearTimeout(timer);
        reject(err instanceof Error ? err : new Error(String(err)));
      });
    });
  }

  private async attach(ws: WebSocket): Promise<void> {
    this.ws = ws;
    this.sendHandshake(ws);

    ws.on('message', (data) => {
      try {
        const raw = Buffer.isBuffer(data) ? data.toString() : String(data);
        const msg = JSON.parse(raw) as ServerEvent;
        this.dispatch(msg);
      } catch {
        // 忽略无法解析的消息
      }
    });
    ws.on('close', () => {
      if (this.ws === ws) this.ws = null;
      this.scheduleReconnect();
    });
    ws.on('error', () => {
      // close 事件会兜底处理
    });
  }

  private async sendHandshake(ws: WebSocket): Promise<void> {
    const cfg = vscode.workspace.getConfiguration('workcode');
    const apiKey = await this.getApiKey();
    if (ws.readyState !== WS_OPEN) return;
    ws.send(
      JSON.stringify({
        type: 'connect',
        payload: {
          apiKey,
          model: cfg.get<string>('model', 'deepseek/deepseek-chat'),
          workspaceRoot: vscode.workspace.workspaceFolders?.[0]?.uri.fsPath,
          terminalSupport: true,
        },
      })
    );
  }

  private scheduleReconnect(): void {
    if (this.reconnecting || !this.started) return;
    this.reconnecting = true;
    setTimeout(async () => {
      this.reconnecting = false;
      if (!(await this.tryConnect())) {
        void vscode.window.showWarningMessage('WorkCode 服务连接断开，未能自动重连。');
      }
    }, 3_000);
  }

  private async waitForConnect(totalMs: number): Promise<void> {
    const start = Date.now();
    while (Date.now() - start < totalMs) {
      if (this.ws && this.ws.readyState === WS_OPEN) return;
      if (await this.tryConnect()) return;
      await new Promise((r) => setTimeout(r, 500));
    }
    throw new Error(`无法连接本地服务 ws://127.0.0.1:${this.port}`);
  }

  private dispatch(msg: ServerEvent): void {
    // 请求-响应：响应里回显 requestId，先结算 pending
    const rid = (msg.payload as Record<string, unknown>)?.requestId as string | undefined;
    if (rid && this.pendingRequests.has(rid)) {
      const resolve = this.pendingRequests.get(rid)!;
      this.pendingRequests.delete(rid);
      resolve(msg.payload);
    }
    for (const handler of this.eventHandlers) {
      try {
        handler(msg);
      } catch {
        // 单个 handler 异常不影响其余
      }
    }
  }

  // ---------- 服务拉起 ----------

  private async spawnService(): Promise<void> {
    const cfg = vscode.workspace.getConfiguration('workcode');
    const serviceDir =
      cfg.get<string>('serviceDir') ||
      path.join(this.ctx.extensionPath, '..', 'agent-service');
    const python = this.resolvePython(serviceDir);
    const apiKey = await this.getApiKey();

    const env: Record<string, string | undefined> = { ...(process.env as Record<string, string>) };
    if (apiKey) env['DEEPSEEK_API_KEY'] = apiKey;
    if (cfg.get<boolean>('mock', false)) env['WORKCODE_MOCK'] = '1';
    env['WORKCODE_PORT'] = String(this.port);

    const child = child_process.spawn(python, ['-m', 'workcode'], {
      cwd: serviceDir,
      env,
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    child.stderr?.on('data', (d) => {
      const text = String(d).trim();
      if (!text) return;
      // 只把警告/错误转发到控制台；INFO/访问日志归入 debug，避免刷屏标红
      if (/ERROR|WARNING|Traceback|Exception|Error|raise|ERR/i.test(text)) {
        console.error('[workcode]', text);
      } else {
        console.debug('[workcode]', text);
      }
    });
    child.on('exit', (code, signal) => {
      console.error(`[workcode] 服务退出 code=${code} signal=${signal}`);
      this.child = null;
    });
    this.child = child;
  }

  private resolvePython(serviceDir: string): string {
    const configured = vscode.workspace.getConfiguration('workcode').get<string>('pythonPath', '');
    if (configured) return configured;
    const venv = path.join(serviceDir, '..', '.venv', 'bin', 'python');
    return fs.existsSync(venv) ? venv : 'python3';
  }

  private async getApiKey(): Promise<string> {
    const fromCfg = vscode.workspace.getConfiguration('workcode').get<string>('apiKey', '');
    if (fromCfg) return fromCfg;
    try {
      const fromSecrets = await this.ctx.secrets.get('workcode.apiKey');
      if (fromSecrets) return fromSecrets;
    } catch {
      // SecretStorage 不可用时忽略
    }
    return process.env['DEEPSEEK_API_KEY'] ?? '';
  }

  // ---------- 发送 / 清理 ----------

  private send(type: string, payload: Record<string, unknown>): void {
    if (!this.ws || this.ws.readyState !== WS_OPEN) {
      // 触发重连（可重试），并给友好提示而不是报错
      void this.ensureRunning().catch(() => {
        void vscode.window.showInformationMessage('WorkCode 服务连接中，请稍后再试。');
      });
      return;
    }
    this.ws.send(JSON.stringify({ type, payload }));
  }

  public dispose(): void {
    this.started = false;
    this.ws?.terminate();
    this.ws = null;
    this.child?.kill();
    this.child = null;
  }
}
