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
  private reconnecting = false;
  private readonly port: number;
  private readonly eventHandlers: Array<(e: ServerEvent) => void> = [];

  constructor(private readonly ctx: vscode.ExtensionContext) {
    super(() => this.dispose());
    this.port = vscode.workspace
      .getConfiguration('workcode')
      .get<number>('servicePort', 4877);
  }

  private get url(): string {
    return `ws://127.0.0.1:${this.port}/ws`;
  }

  /** 确保服务在线：先探活，失败则拉起 Python 进程并等待连接。 */
  async ensureRunning(): Promise<void> {
    if (this.started) return;
    this.started = true;
    if (await this.tryConnect()) return;
    await this.spawnService();
    await this.waitForConnect(8_000);
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
      void vscode.window.showErrorMessage('WorkCode 服务未连接，请稍后重试。');
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
