import * as path from 'path';
import * as vscode from 'vscode';
import type { DiffPreviewPayload, TerminalExecPayload } from '@workcode/shared';
import { buildContextSummary, buildEditorContext } from './editorContext';
import { DiffManager } from './diffManager';
import { ServiceClient, type ServerEvent } from './serviceClient';
import { TerminalRunner } from './terminalRunner';

/** 侧边栏聊天面板：WebviewViewProvider，负责 UI 与服务之间的消息路由。 */
export class ChatPanelProvider implements vscode.WebviewViewProvider {
  private view: vscode.WebviewView | undefined;
  private fileCache: string[] | null = null;
  private currentMode = 'manual';
  private currentEffort = 'low';
  private currentModel = vscode.workspace.getConfiguration('workcode').get<string>('model', 'deepseek/deepseek-flash');

  constructor(
    private readonly client: ServiceClient,
    private readonly diffs: DiffManager,
    private readonly terminalRunner: TerminalRunner
  ) {}

  resolveWebviewView(view: vscode.WebviewView): void {
    this.view = view;
    view.webview.options = { enableScripts: true, localResourceRoots: [] };
    view.webview.html = getWebviewHtml();

    view.webview.onDidReceiveMessage((msg) => {
      switch (msg.type) {
        case 'send':
          void this.sendMessage(msg.content as string);
          break;
        case 'cancel':
          this.client.cancel();
          break;
        case 'newSession':
          this.client.newSession();
          break;
        case 'permission':
          this.client.respondPermission(String(msg.requestId), Boolean(msg.allowed));
          break;
        case 'applyDiff':
          this.client.respondApplyDiff(String(msg.requestId), Boolean(msg.accepted));
          break;
        case 'listSessions':
          void this.sendSessionList();
          break;
        case 'resumeSession':
          void this.client.resumeSession(String(msg.sessionId));
          break;
        case 'queryFiles':
          void this.sendFileList(String(msg.query ?? ''));
          break;
        case 'toggleMode':
          this.toggleMode();
          break;
        case 'toggleEffort':
          this.toggleEffort();
          break;
        case 'setMode':
          this.applyMode(String(msg.mode ?? ''));
          break;
        case 'setEffort':
          this.applyEffort(String(msg.effort ?? ''));
          break;
        case 'memoryGet':
          void this.sendMemory();
          break;
        case 'memoryAdd':
          void this.addMemory(String(msg.text ?? ''));
          break;
        case 'pickModel':
          void this.pickModel();
          break;
        case 'setModel':
          void this.applyModel(String(msg.model ?? ''));
          break;
        case 'ready':
          this.pushContext();
          break;
        case 'refreshContext':
          this.pushContext();
          break;
        case 'openSettings':
          void vscode.commands.executeCommand('workbench.action.openSettings', 'workcode');
          break;
      }
    });

    this.client.onEvent((evt) => {
      if (evt.type === 'diff_preview') {
        void this.diffs.showDiff(evt.payload as unknown as DiffPreviewPayload);
      } else if (evt.type === 'terminal_exec') {
        void this.terminalRunner.run(evt.payload as unknown as TerminalExecPayload);
      } else if (evt.type === 'session_resumed') {
        void this.view?.webview.postMessage({
          type: 'resume',
          sessionId: String(evt.payload.sessionId),
          messages: Array.isArray(evt.payload.messages) ? evt.payload.messages : [],
        });
      }
      this.postEvent(evt);
    });
    void this.client.ensureRunning().catch((err) => {
      void vscode.window.showErrorMessage(
        `WorkCode 服务启动失败：${err instanceof Error ? err.message : String(err)}`
      );
    });
  }

  private async sendMessage(content: string): Promise<void> {
    if (!content.trim()) return;
    try {
      await this.client.ensureRunning();
    } catch (err) {
      void vscode.window.showErrorMessage(
        `WorkCode 服务不可用：${err instanceof Error ? err.message : String(err)}`
      );
      return;
    }
    this.client.sendChat(content, buildEditorContext());
  }

  /** “解释选中代码”命令：把当前文件/选区上下文直接发给 Agent。 */
  async explainSelection(): Promise<void> {
    try {
      await this.client.ensureRunning();
    } catch (err) {
      void vscode.window.showErrorMessage(
        `WorkCode 服务不可用：${err instanceof Error ? err.message : String(err)}`
      );
      return;
    }
    this.client.sendChat(
      '请解释当前选中的代码；如果没有选中代码，则解释当前文件的关键部分。',
      buildEditorContext()
    );
  }

  /** 把当前文件 + 选区行号推给 Webview 顶部上下文栏。 */
  pushContext(): void {
    void this.view?.webview.postMessage({ type: 'context', payload: buildContextSummary() });
  }

  /** 权限模式循环切换：Manual → Auto → Plan → Manual。服务端回 ready 更新 chips。 */
  private toggleMode(): void {
    const next =
      this.currentMode === 'manual' ? 'auto' : this.currentMode === 'auto' ? 'plan' : 'manual';
    this.currentMode = next;
    this.client.setMode(next);
  }

  /** 思考程度循环切换：none → low → high → max → none。 */
  private toggleEffort(): void {
    const order = ['none', 'low', 'high', 'max'];
    const idx = order.indexOf(this.currentEffort);
    this.currentEffort = order[(idx + 1) % order.length];
    this.client.setEffort(this.currentEffort);
  }

  /** 设置指定权限模式（slash 命令用）。 */
  private applyMode(mode: string): void {
    if (mode === 'manual' || mode === 'auto' || mode === 'plan') {
      this.currentMode = mode;
      this.client.setMode(mode);
    }
  }

  /** 设置指定思考程度（slash 命令用）。 */
  private applyEffort(effort: string): void {
    if (effort === 'none' || effort === 'low' || effort === 'high' || effort === 'max') {
      this.currentEffort = effort;
      this.client.setEffort(effort);
    }
  }

  /** 自定义模型：输入 LiteLLM 标识后写入配置并通知服务端。 */
  private async pickModel(): Promise<void> {
    const model = await vscode.window.showInputBox({
      prompt: '输入模型标识（LiteLLM 格式）',
      value: this.currentModel || 'deepseek/deepseek-flash',
      ignoreFocusOut: true,
    });
    if (model) await this.applyModel(model);
  }

  /** 读取用户记忆并回推给 Webview。 */
  private async sendMemory(): Promise<void> {
    const content = (await this.client.getMemory()) || '';
    void this.view?.webview.postMessage({ type: 'memoryContent', content });
  }

  /** 追加一条用户记忆并回推更新后的内容。 */
  private async addMemory(text: string): Promise<void> {
    const content = (await this.client.addMemory(text)) || '';
    void this.view?.webview.postMessage({ type: 'memoryContent', content });
  }

  /** 把选中的模型写入配置，并让服务端切换。 */
  private async applyModel(model: string): Promise<void> {
    const value = model.trim();
    if (!value) return;
    this.currentModel = value;
    await vscode.workspace.getConfiguration('workcode').update('model', value, true);
    this.client.setModel(value);
  }

  /** 按 @ 后面的关键字返回工作区文件列表，推给 Webview 浮层。 */
  private async sendFileList(query: string): Promise<void> {
    if (!this.fileCache) {
      this.fileCache = await getWorkspaceFiles();
    }
    const q = query.toLowerCase();
    const files = q
      ? this.fileCache.filter((f) => path.basename(f).toLowerCase().includes(q))
      : this.fileCache;
    void this.view?.webview.postMessage({ type: 'fileList', files: files.slice(0, 20) });
  }

  /** 拉取历史会话列表并推给 Webview 渲染。 */
  private async sendSessionList(): Promise<void> {
    console.log('[workcode] 宿主收到 listSessions，开始拉取');
    try {
      const sessions = await this.client.listSessions();
      console.log('[workcode] 拉到会话数:', sessions?.length ?? 0);
      void this.view?.webview.postMessage({
        type: 'sessionList',
        sessions: sessions ?? [],
      });
    } catch (err) {
      console.error('[workcode] 拉取会话失败:', err);
      void this.view?.webview.postMessage({ type: 'sessionList', sessions: [] });
    }
  }

  private postEvent(evt: ServerEvent): void {
    void this.view?.webview.postMessage({ type: 'event', event: evt });
  }
}

/** 列出工作区里的文件（跳过 node_modules/.venv/dist 等）。 */
async function getWorkspaceFiles(): Promise<string[]> {
  const folders = vscode.workspace.workspaceFolders;
  if (!folders || folders.length === 0) return [];
  const uris = await vscode.workspace.findFiles(
    '**/*',
    '**/{node_modules,.venv,dist,out,build,.git,.idea,.workcode}/**',
    500
  );
  return uris.map((u) => u.fsPath);
}

function getWebviewHtml(): string {
  return /* html */ `<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<style>
  * { box-sizing: border-box; }
  html, body { height: 100%; margin: 0; }
  body {
    display: flex; flex-direction: column;
    background: var(--vscode-sideBar-background, #1c1c1c);
    color: var(--vscode-foreground, #d4d4d4);
    font-family: -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
    font-size: 13px;
  }
  button { font: inherit; color: inherit; background: transparent; border: none; padding: 0; cursor: pointer; }
  button:focus-visible { outline: 1px solid #3b82f6; outline-offset: 1px; }
  .hidden { display: none !important; }
  .topbar {
    height: 40px; flex-shrink: 0;
    display: flex; align-items: center; gap: 8px;
    padding: 0 6px 0 14px;
  }
  .session-title { display: flex; align-items: center; gap: 6px; min-width: 0; flex: 1; color: #e8e8e8; }
  #sessionTitle { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 13px; }
  .session-title .pencil { color: #8d8d8d; flex-shrink: 0; }
  .top-actions { display: flex; align-items: center; flex-shrink: 0; }
  .icon-btn {
    width: 28px; height: 28px; border-radius: 6px;
    display: grid; place-items: center; color: #bdbdbd;
  }
  .icon-btn:hover { background: rgba(255,255,255,.06); color: #f2f2f2; }
  .search-bar { flex-shrink: 0; padding: 0 12px 8px; }
  .search-bar input {
    width: 100%; background: #2a2a2a; color: inherit;
    border: 1px solid #3a3a3a; border-radius: 8px;
    padding: 6px 10px; font: inherit; font-size: 12px;
  }
  .search-bar input:focus { outline: none; border-color: #3b82f6; }
  .stage { flex: 1; min-height: 0; position: relative; }
  #messages { height: 100%; overflow-y: auto; padding: 8px 14px 12px; }
  .empty {
    position: absolute; inset: 0;
    display: flex; flex-direction: column; align-items: center; justify-content: center;
    pointer-events: none; gap: 16px;
  }
  .logo-wrap { position: relative; width: 78px; height: 64px; }
  .logo { width: 62px; height: 62px; display: block; }
  .ver {
    position: absolute; left: 48px; top: 16px;
    background: #3c2d66; color: #f4efff;
    border: 1px solid #6a5494; border-radius: 9px;
    font-size: 11px; line-height: 1; padding: 3px 7px; white-space: nowrap;
  }
  .hero-text { color: #8a8a8a; font-size: 14px; letter-spacing: .2px; }
  .toast {
    align-self: center; flex-shrink: 0;
    max-width: calc(100% - 24px); margin: 2px 12px 8px;
    background: #2a2a2a; border: 1px solid #3a3a3a; border-radius: 999px;
    padding: 3px 10px; font-size: 11px; color: #c8c8c8;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .msg { margin-bottom: 14px; }
  .msg.user { display: flex; flex-direction: column; align-items: flex-end; }
  .role { display: none; }
  .bubble {
    max-width: 100%; padding: 2px 0;
    white-space: pre-wrap; word-break: break-word; line-height: 1.55;
  }
  .reasoning {
    font-size: 11px; color: var(--vscode-descriptionForeground, #9d9d9d);
    background: #3a3a3a22; border-left: 3px solid #9d9d9d66;
    padding: 4px 8px; border-radius: 4px; margin-bottom: 6px;
    white-space: pre-wrap; word-break: break-word; max-height: 160px; overflow-y: auto;
  }
  .bubble.user {
    background: #2a2d31; border-radius: 12px;
    padding: 8px 12px; max-width: 88%;
  }
  .bubble.error { color: var(--vscode-errorForeground, #f14c4c); }
  .bubble.md { white-space: normal; }
  .bubble.md h1, .bubble.md h2, .bubble.md h3, .bubble.md h4 { margin: 8px 0 4px; line-height: 1.3; font-weight: 600; }
  .bubble.md h1 { font-size: 15px; } .bubble.md h2 { font-size: 14px; } .bubble.md h3 { font-size: 13px; } .bubble.md h4 { font-size: 12px; }
  .bubble.md p { margin: 4px 0; }
  .bubble.md pre {
    background: var(--vscode-textCodeBlock-background, #161616);
    border: 1px solid var(--vscode-sideBarSectionHeader-border, #3c3c3c);
    border-radius: 4px; padding: 8px; overflow-x: auto;
    font-size: 12px; line-height: 1.4;
  }
  .bubble.md code { font-family: "SF Mono", Menlo, Consolas, monospace; }
  .bubble.md :not(pre) > code { background: #3a3a3a55; border-radius: 3px; padding: 1px 4px; font-size: 12px; }
  .bubble.md ul, .bubble.md ol { margin: 4px 0; padding-left: 20px; }
  .bubble.md li { margin: 2px 0; }
  .bubble.md table { border-collapse: collapse; margin: 6px 0; font-size: 12px; width: 100%; }
  .bubble.md th, .bubble.md td { border: 1px solid var(--vscode-sideBarSectionHeader-border, #3c3c3c); padding: 3px 6px; text-align: left; }
  .bubble.md th { background: #3a3a3a55; font-weight: 600; }
  .bubble.md blockquote { margin: 4px 0; padding-left: 10px; border-left: 3px solid var(--vscode-focusBorder, #3794ff); color: var(--vscode-descriptionForeground, #9d9d9d); }
  .bubble.md a { color: var(--vscode-textLink-foreground, #3794ff); text-decoration: none; }
  .bubble.md hr { border: none; border-top: 1px solid var(--vscode-sideBarSectionHeader-border, #3c3c3c); margin: 8px 0; }
  .tool-line { font-size: 12px; color: var(--vscode-descriptionForeground, #9d9d9d); margin: 4px 0; font-family: monospace; white-space: pre-wrap; }
  .tool-line.ok { color: #89d185; }
  .tool-line.err { color: var(--vscode-errorForeground, #f14c4c); }
  .perm {
    background: #1e3a5f55; border: 1px solid var(--vscode-focusBorder, #3794ff);
    border-radius: 6px; padding: 8px 10px; margin-bottom: 10px;
  }
  .perm-head { font-weight: 600; margin-bottom: 6px; }
  .perm-detail { font-size: 11px; font-family: monospace; color: var(--vscode-descriptionForeground, #9d9d9d); margin-bottom: 8px; white-space: pre-wrap; word-break: break-all; max-height: 120px; overflow-y: auto; }
  .dock { flex-shrink: 0; position: relative; }
  .tabs {
    display: flex; align-items: stretch;
    border-top: 1px solid rgba(255,255,255,.06);
  }
  .tab {
    flex: 1; height: 36px;
    display: flex; align-items: center; justify-content: center; gap: 6px;
    color: #9a9a9a; font-size: 13px;
  }
  .tab:hover { color: #d0d0d0; }
  .tab.active { color: #f3f3f3; box-shadow: inset 0 -2px 0 #e8e8e8; }
  .tab-panel {
    max-height: 132px; overflow-y: auto;
    padding: 8px 12px; font-size: 12px; color: #c8c8c8;
    border-top: 1px solid rgba(255,255,255,.06);
  }
  .tab-empty { color: #7a7a7a; text-align: center; padding: 10px 0; }
  .tab-row { padding: 4px 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .tab-row.ok { color: #89d185; }
  .tab-row.err { color: var(--vscode-errorForeground, #f14c4c); }
  .composer-wrap { position: relative; }
  .at-popup {
    position: absolute; left: 10px; right: 10px; bottom: calc(100% + 6px);
    background: #252526; border: 1px solid #3c3c3c;
    border-radius: 8px; max-height: 180px; overflow-y: auto; z-index: 20;
    box-shadow: 0 8px 24px rgba(0,0,0,.35);
  }
  .at-item { padding: 6px 10px; cursor: pointer; font-size: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .at-item.active { background: #0e639c; color: #fff; }
  .at-item.muted { color: #9d9d9d; cursor: default; }
  .slash-popup {
    position: absolute; left: 8px; right: 8px; z-index: 25;
    background: #2a2a2a; border: 1px solid #3a3a3a;
    border-radius: 10px; max-height: 280px; overflow-y: auto;
    padding: 4px; box-shadow: 0 10px 28px rgba(0,0,0,.45);
  }
  .slash-item {
    display: flex; align-items: center; gap: 8px;
    padding: 7px 8px; border-radius: 6px; cursor: pointer;
    font-size: 13px; color: #f3f3f3;
  }
  .slash-item.active { background: #3a3a3a; }
  .slash-ico { color: #8a8a8a; width: 14px; flex-shrink: 0; font-size: 14px; line-height: 1; }
  .slash-cmd { font-weight: 600; flex-shrink: 0; }
  .slash-desc {
    margin-left: auto; min-width: 0; padding-left: 12px;
    color: #8d8d8d; font-size: 12px; font-weight: 400;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .slash-item.muted { color: #8d8d8d; cursor: default; justify-content: center; }
  .composer {
    margin: 8px 10px 10px;
    border: 1px solid #3b82f6; border-radius: 12px;
    background: #1a1a1a;
    display: flex; flex-direction: column;
    overflow: hidden;
  }
  .composer:focus-within { border-color: #60a5fa; }
  .banner {
    display: flex; align-items: center; gap: 8px;
    padding: 7px 8px 7px 12px;
    background: #1a4f8c; color: #f4f8fd; font-size: 12px;
  }
  .banner-text { flex: 1; min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .banner-x { width: 22px; height: 22px; color: #fff; font-size: 16px; line-height: 1; border-radius: 4px; }
  .banner-x:hover { background: rgba(255,255,255,.12); }
  .ctx-row {
    display: flex; align-items: center; justify-content: space-between;
    padding: 6px 4px 0 8px; color: #9a9a9a; font-size: 12px; gap: 6px;
  }
  .ctx-btn {
    display: flex; align-items: center; gap: 6px;
    min-width: 0; flex: 1; text-align: left; color: #b0b0b0;
    padding: 2px 4px; border-radius: 6px;
  }
  .ctx-btn:hover { background: rgba(255,255,255,.04); }
  #context { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .ctx-tip {
    margin: 2px 12px 0; font-size: 11px; color: #8eb4ff;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  textarea {
    width: 100%; resize: none; border: none; background: transparent;
    color: var(--vscode-input-foreground, #e6e6e6);
    padding: 8px 12px 2px; min-height: 52px; max-height: 140px; font: inherit;
  }
  textarea:focus { outline: none; }
  textarea::placeholder { color: #6f6f6f; }
  .composer-bar {
    display: flex; align-items: center; justify-content: space-between;
    gap: 6px; padding: 2px 8px 8px;
  }
  .bar-left { display: flex; align-items: center; gap: 2px; min-width: 0; flex: 1; }
  .mini-logo { width: 16px; height: 16px; flex-shrink: 0; }
  .mode-chip, .model-chip {
    display: inline-flex; align-items: center; gap: 4px;
    max-width: 46%; padding: 3px 6px; border-radius: 6px;
    font-size: 12px; color: #d0d0d0;
  }
  .mode-chip.is-auto { color: #e8c547; }
  .mode-chip.is-plan { color: #8eb4ff; }
  .model-chip { color: #8eb4ff; }
  .mode-chip:hover, .model-chip:hover { background: rgba(255,255,255,.06); }
  .model-menu {
    position: absolute; z-index: 30;
    min-width: 248px; padding: 6px;
    background: #2a2a2a; border: 1px solid #3a3a3a; border-radius: 10px;
    box-shadow: 0 12px 32px rgba(0,0,0,.5);
  }
  .model-item {
    display: flex; align-items: flex-start; gap: 10px;
    width: 100%; padding: 8px 10px; border-radius: 6px;
    font-size: 13px; color: #f2f2f2; text-align: left;
  }
  .model-item:hover { background: rgba(255,255,255,.06); }
  .model-item.active { background: #1a56b0; color: #fff; }
  .model-item.active:hover { background: #1d5fbe; }
  .model-ico { width: 16px; height: 16px; margin-top: 1px; flex-shrink: 0; }
  .model-text { display: flex; flex-direction: column; gap: 2px; min-width: 0; flex: 1; }
  .model-name { font-size: 13px; font-weight: 600; line-height: 1.25; }
  .model-desc { font-size: 11px; line-height: 1.35; color: #9a9a9a; white-space: normal; }
  .model-item.active .model-desc { color: rgba(255,255,255,.78); }
  .model-check { margin-left: 8px; margin-top: 2px; color: #3dd68c; flex-shrink: 0; font-size: 14px; }
  .model-item.custom { align-items: center; color: #e8e8e8; font-size: 13px; }
  .model-plus { width: 16px; text-align: center; color: #d0d0d0; flex-shrink: 0; }
  .model-divider { height: 1px; margin: 4px 8px; background: rgba(255,255,255,.08); }
  #modeLabel, #modelLabel { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .caret { flex-shrink: 0; opacity: .8; }
  .send-slot { flex-shrink: 0; }
  .send-btn {
    width: 28px; height: 28px; border-radius: 50%;
    border: 1px solid #3f3f3f; color: #8a8a8a;
    display: grid; place-items: center;
  }
  .send-btn.ready { color: #eee; border-color: #6a6a6a; background: #2a2a2a; }
  .send-btn.stop { color: #f2f2f2; border-color: #777; }
  .btns { display: flex; gap: 6px; }
  .perm button, .history-head button.ghost {
    background: #3a3a3a; color: #ddd; border-radius: 6px; padding: 5px 12px;
  }
  .perm button.primary { background: #0e639c; color: #fff; }
  .perm button:hover, .history-head button.ghost:hover { filter: brightness(1.08); }
  .history {
    flex: 1; min-height: 0; display: flex; flex-direction: column;
    background: var(--vscode-sideBar-background, #1c1c1c);
  }
  .history-head {
    padding: 8px 8px 8px 14px; font-weight: 600;
    display: flex; justify-content: space-between; align-items: center;
  }
  .history-list { flex: 1; overflow-y: auto; padding: 4px 10px 10px; }
  .history-item {
    border-radius: 8px; padding: 8px 10px; margin-bottom: 4px; cursor: pointer;
  }
  .history-item:hover { background: rgba(255,255,255,.06); }
  .history-item .preview { font-size: 12px; white-space: pre-wrap; word-break: break-word; }
  .history-item .time { font-size: 11px; color: #8d8d8d; margin-top: 4px; }
  .history-item.muted { cursor: default; color: #8d8d8d; }
  .history-item.muted:hover { background: transparent; }
</style>
</head>
<body>
  <header class="topbar">
    <div class="session-title">
      <span id="sessionTitle">新会话</span>
      <svg class="pencil" width="14" height="14" viewBox="0 0 16 16" aria-hidden="true"><path d="M11.2 2.2a1.4 1.4 0 0 1 2 2L6.2 11.2 3.5 12l.8-2.7 7-7z" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/></svg>
    </div>
    <div class="top-actions">
      <button id="searchBtn" class="icon-btn" title="搜索消息" type="button">
        <svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true"><circle cx="7" cy="7" r="4.2" fill="none" stroke="currentColor" stroke-width="1.4"/><path d="M10.2 10.2 13 13" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/></svg>
      </button>
      <button id="new" class="icon-btn" title="新会话" type="button">
        <svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true"><path d="M8 3.2v9.6M3.2 8h9.6" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/></svg>
      </button>
      <button id="editBtn" class="icon-btn" title="编辑" type="button">
        <svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true"><rect x="2.2" y="2.6" width="11.6" height="10.8" rx="1.4" fill="none" stroke="currentColor" stroke-width="1.3"/><path d="M6.2 2.6v10.8" stroke="currentColor" stroke-width="1.3"/></svg>
      </button>
      <button id="historyBtn" class="icon-btn" title="历史会话" type="button">
        <svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="5.3" fill="none" stroke="currentColor" stroke-width="1.3"/><path d="M8 5.1V8.2l2 1.3" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/></svg>
      </button>
      <button id="settingsBtn" class="icon-btn" title="设置" type="button">
        <svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="2" fill="none" stroke="currentColor" stroke-width="1.3"/><path d="M8 1.8v1.6M8 12.6v1.6M1.8 8h1.6M12.6 8h1.6M3.4 3.4l1.1 1.1M11.5 11.5l1.1 1.1M12.6 3.4l-1.1 1.1M4.5 11.5l-1.1 1.1" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/></svg>
      </button>
    </div>
  </header>
  <div id="searchBar" class="search-bar hidden">
    <input id="searchInput" type="text" placeholder="搜索当前对话" />
  </div>
  <div id="status" class="toast hidden"><span id="statusText"></span></div>
  <div id="stage" class="stage">
    <div id="empty" class="empty">
      <div class="logo-wrap">
        <svg class="logo" viewBox="0 0 64 64" aria-hidden="true">
          <g fill="#e3947c" transform="translate(32 32)">
            <rect x="-3.1" y="-26" width="6.2" height="17" rx="3.1"/>
            <rect x="-3.1" y="-26" width="6.2" height="17" rx="3.1" transform="rotate(30)"/>
            <rect x="-3.1" y="-26" width="6.2" height="17" rx="3.1" transform="rotate(60)"/>
            <rect x="-3.1" y="-26" width="6.2" height="17" rx="3.1" transform="rotate(90)"/>
            <rect x="-3.1" y="-26" width="6.2" height="17" rx="3.1" transform="rotate(120)"/>
            <rect x="-3.1" y="-26" width="6.2" height="17" rx="3.1" transform="rotate(150)"/>
            <rect x="-3.1" y="-26" width="6.2" height="17" rx="3.1" transform="rotate(180)"/>
            <rect x="-3.1" y="-26" width="6.2" height="17" rx="3.1" transform="rotate(210)"/>
            <rect x="-3.1" y="-26" width="6.2" height="17" rx="3.1" transform="rotate(240)"/>
            <rect x="-3.1" y="-26" width="6.2" height="17" rx="3.1" transform="rotate(270)"/>
            <rect x="-3.1" y="-26" width="6.2" height="17" rx="3.1" transform="rotate(300)"/>
            <rect x="-3.1" y="-26" width="6.2" height="17" rx="3.1" transform="rotate(330)"/>
          </g>
        </svg>
        <span class="ver">v0.1.0</span>
      </div>
      <div class="hero-text">给 WorkCode 发送消息</div>
    </div>
    <div id="messages"></div>
  </div>
  <div id="history" class="history hidden">
    <div class="history-head">历史会话 <button id="historyClose" class="icon-btn" title="关闭" type="button">×</button></div>
    <div id="historyList" class="history-list"></div>
  </div>
  <div id="dock" class="dock">
    <div class="tabs">
      <button class="tab" data-tab="tasks" type="button">
        <svg width="15" height="15" viewBox="0 0 16 16" aria-hidden="true"><path d="M3 4.2h10M3 8h10M3 11.8h10" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/></svg>
        任务
      </button>
      <button class="tab" data-tab="agents" type="button">
        <svg width="15" height="15" viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="5.2" r="2.1" fill="none" stroke="currentColor" stroke-width="1.3"/><path d="M3.6 12.6c.7-2 2.3-3 4.4-3s3.7 1 4.4 3" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/></svg>
        子代理
      </button>
      <button class="tab" data-tab="edits" type="button">
        <svg width="15" height="15" viewBox="0 0 16 16" aria-hidden="true"><path d="M11.2 2.3a1.3 1.3 0 0 1 1.9 1.9L6 11.3 3.4 12l.7-2.6 7.1-7.1z" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/></svg>
        编辑
      </button>
    </div>
    <div id="tabPanel" class="tab-panel hidden"></div>
    <div class="composer-wrap">
    <div id="modelMenu" class="model-menu hidden" role="listbox"></div>
    <div id="atPopup" class="at-popup hidden"></div>
    <div id="slashPopup" class="slash-popup hidden"></div>
    <div id="composer" class="composer">
      <div id="banner" class="banner">
        <span class="banner-text">本地运行的编程助手，用 @ 引用文件，Enter 发送</span>
        <button id="bannerClose" class="banner-x" title="关闭" type="button">×</button>
      </div>
      <div class="ctx-row">
        <button id="ctxBtn" class="ctx-btn" type="button" title="当前文件">
          <svg width="14" height="14" viewBox="0 0 16 16" aria-hidden="true"><path d="M11 2.4a1.2 1.2 0 0 1 1.7 1.7L6.2 10.6 3.6 11.3l.7-2.6L11 2.4z" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linejoin="round"/></svg>
          <svg class="ring" width="14" height="14" viewBox="0 0 16 16" aria-hidden="true">
            <circle cx="8" cy="8" r="6" fill="none" stroke="#3a3a3a" stroke-width="1.6"/>
            <circle id="usageRing" cx="8" cy="8" r="6" fill="none" stroke="#8eb4ff" stroke-width="1.6" stroke-linecap="round" transform="rotate(-90 8 8)" stroke-dasharray="0 37.7"/>
          </svg>
          <span id="usageText">0%</span>
          <svg width="14" height="14" viewBox="0 0 16 16" aria-hidden="true"><path d="M4 2.2h5.2L12.8 6v7.6a.8.8 0 0 1-.8.8H4a.8.8 0 0 1-.8-.8V3a.8.8 0 0 1 .8-.8z" fill="none" stroke="currentColor" stroke-width="1.2"/><path d="M9 2.4V6h3.6" fill="none" stroke="currentColor" stroke-width="1.2"/></svg>
          <span id="context">文件上下文</span>
          <svg width="12" height="12" viewBox="0 0 16 16" aria-hidden="true"><path d="M4 6.2 8 10l4-3.8" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg>
        </button>
        <button id="refreshCtx" class="icon-btn" title="刷新文件上下文" type="button">
          <svg width="14" height="14" viewBox="0 0 16 16" aria-hidden="true"><path d="M13 8a5 5 0 1 1-1.4-3.4" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/><path d="M13 2.8V5.4H10.4" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/></svg>
        </button>
      </div>
      <div id="ctxTip" class="ctx-tip hidden"></div>
      <textarea id="input" placeholder="@引用文件，Enter 发送 / Shift+Enter 换行"></textarea>
      <div class="composer-bar">
        <div class="bar-left">
          <svg class="mini-logo" viewBox="0 0 64 64" aria-hidden="true">
            <g fill="#e3947c" transform="translate(32 32)">
              <rect x="-5" y="-26" width="10" height="16" rx="5"/>
              <rect x="-5" y="-26" width="10" height="16" rx="5" transform="rotate(45)"/>
              <rect x="-5" y="-26" width="10" height="16" rx="5" transform="rotate(90)"/>
              <rect x="-5" y="-26" width="10" height="16" rx="5" transform="rotate(135)"/>
              <rect x="-5" y="-26" width="10" height="16" rx="5" transform="rotate(180)"/>
              <rect x="-5" y="-26" width="10" height="16" rx="5" transform="rotate(225)"/>
              <rect x="-5" y="-26" width="10" height="16" rx="5" transform="rotate(270)"/>
              <rect x="-5" y="-26" width="10" height="16" rx="5" transform="rotate(315)"/>
            </g>
          </svg>
          <button id="modeChip" class="mode-chip" type="button" title="点击切换权限模式（手动 / 全自动 / 计划）">
            <svg width="13" height="13" viewBox="0 0 16 16" aria-hidden="true"><path d="M9 1.5 4 9h4l-1 5.5L12 7H8l1-5.5z" fill="currentColor"/></svg>
            <span id="modeLabel">手动</span>
            <svg class="caret" width="10" height="10" viewBox="0 0 16 16" aria-hidden="true"><path d="M4 6.2 8 10l4-3.8" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>
          </button>
          <button id="modelChip" class="model-chip" type="button" title="点击切换模型">
            <svg width="12" height="12" viewBox="0 0 16 16" aria-hidden="true"><path d="M8 1.6 14 8 8 14.4 2 8 8 1.6z" fill="currentColor"/></svg>
            <span id="modelLabel">DeepSeek Chat</span>
            <svg class="caret" width="10" height="10" viewBox="0 0 16 16" aria-hidden="true"><path d="M4 6.2 8 10l4-3.8" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>
          </button>
          <button id="effortChip" class="model-chip" type="button" title="点击切换思考程度（none/low/high/max）">
            <svg width="12" height="12" viewBox="0 0 16 16" aria-hidden="true"><path d="M8 2a1 1 0 0 1 1 1v4h4a1 1 0 0 1 0 2H9v4a1 1 0 0 1-2 0V9H3a1 1 0 0 1 0-2h4V3a1 1 0 0 1 1-1z" fill="currentColor"/></svg>
            <span id="effortLabel">思考: 低</span>
            <svg class="caret" width="10" height="10" viewBox="0 0 16 16" aria-hidden="true"><path d="M4 6.2 8 10l4-3.8" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>
          </button>
        </div>
        <div class="send-slot">
          <button id="send" class="send-btn" title="发送" type="button">
            <svg width="14" height="14" viewBox="0 0 16 16" aria-hidden="true"><path d="M2.4 8.2 13.2 2.6 8.6 13.4 7.2 8.8 2.4 8.2z" fill="currentColor"/></svg>
          </button>
          <button id="cancel" class="send-btn stop hidden" title="停止" type="button">
            <svg width="10" height="10" viewBox="0 0 16 16" aria-hidden="true"><rect x="3" y="3" width="10" height="10" rx="1.5" fill="currentColor"/></svg>
          </button>
        </div>
      </div>
    </div>
    </div>
  </div>
<script>
  const vscode = acquireVsCodeApi();
  // 版本标记：用来确认扩展加载的是否最新构建
  console.log('[workcode] webview v3 — Markdown 渲染已启用');
  // 渲染自检：确认 mdToHtml 在本环境可用（长度应 > 0）
  try {
    const selfTestLen = mdToHtml('## 测试\\n\\n**加粗**').length;
    console.log('[workcode] 渲染自检 mdToHtml 长度 =', selfTestLen);
  } catch (err) {
    console.error('[workcode] 渲染自检失败:', err);
  }
  const messages = document.getElementById('messages');
  const input = document.getElementById('input');
  let assistant = null;
  let assistantText = '';
  let reasoningEl = null;
  let reasoningText = '';
  const tools = {};
  const tasks = [];
  const editItems = [];
  let activeTab = '';
  let ctxPath = '';
  let currentModelId = 'deepseek/deepseek-flash';
  const MODEL_PRESETS = [
    { id: 'deepseek/deepseek-flash', name: 'DeepSeek Flash', desc: '快速推理（v4-flash），适合日常写代码', color: '#e3947c' },
    { id: 'deepseek/deepseek-v4-pro', name: 'DeepSeek V4 Pro', desc: '强推理（v4-pro），适合复杂问题', color: '#7eb6ff' },
  ];
  let currentEffort = 'low';

  const savedUi = vscode.getState() || {};
  if (savedUi.hideBanner) document.getElementById('banner').classList.add('hidden');

  function syncEmpty() {
    document.getElementById('empty').classList.toggle('hidden', messages.childElementCount > 0);
  }
  function setBusy(busy) {
    document.getElementById('send').classList.toggle('hidden', !!busy);
    document.getElementById('cancel').classList.toggle('hidden', !busy);
  }
  function syncSend() {
    document.getElementById('send').classList.toggle('ready', input.value.trim().length > 0);
  }
  function setUsagePct(pct, title) {
    const n = Math.max(0, Math.min(100, pct || 0));
    const usage = document.getElementById('usageText');
    usage.textContent = n + '%';
    usage.title = title || '';
    const ring = document.getElementById('usageRing');
    if (ring) {
      const len = 37.7;
      ring.setAttribute('stroke-dasharray', (len * n / 100) + ' ' + len);
    }
  }
  function touchTitle(text) {
    const el = document.getElementById('sessionTitle');
    if (!el || el.dataset.locked === '1') return;
    const t = String(text || '').replace(/\\s+/g, ' ').trim();
    if (!t) return;
    el.textContent = t.length > 18 ? t.slice(0, 18) + '…' : t;
    el.dataset.locked = '1';
  }
  function resetTitle() {
    const el = document.getElementById('sessionTitle');
    el.textContent = '新会话';
    delete el.dataset.locked;
  }
  function renderTabPanel() {
    const panel = document.getElementById('tabPanel');
    if (!activeTab) {
      panel.classList.add('hidden');
      panel.innerHTML = '';
      return;
    }
    panel.classList.remove('hidden');
    let html = '';
    if (activeTab === 'tasks') {
      html = tasks.length
        ? tasks.map(function (t) {
            const cls = t.ok === true ? 'ok' : t.ok === false ? 'err' : '';
            return '<div class="tab-row ' + cls + '">' + escapeHtml(t.tool + ' ' + t.brief) + '</div>';
          }).join('')
        : '<div class="tab-empty">暂无任务</div>';
    } else if (activeTab === 'agents') {
      html = '<div class="tab-empty">暂无子代理</div>';
    } else {
      html = editItems.length
        ? editItems.map(function (t) {
            return '<div class="tab-row">' + escapeHtml(t) + '</div>';
          }).join('')
        : '<div class="tab-empty">暂无编辑</div>';
    }
    panel.innerHTML = html;
  }
  function selectTab(name) {
    activeTab = activeTab === name ? '' : name;
    document.querySelectorAll('.tab').forEach(function (b) {
      b.classList.toggle('active', b.getAttribute('data-tab') === activeTab);
    });
    renderTabPanel();
  }
  function setStatus(text) {
    const quiet = !text || text.indexOf('就绪') >= 0 || text.indexOf('完成') >= 0 || text.indexOf('会话 ') === 0;
    document.getElementById('statusText').textContent = text || '';
    document.getElementById('status').classList.toggle('hidden', quiet);
  }
  function renderState(model, mode) {
    const label = mode === 'plan' ? '计划' : mode === 'auto' ? '全自动' : '手动';
    document.getElementById('modeLabel').textContent = label;
    const chip = document.getElementById('modeChip');
    chip.classList.toggle('is-auto', mode === 'auto');
    chip.classList.toggle('is-plan', mode === 'plan');
    if (model) currentModelId = String(model);
    const meta = modelMeta(currentModelId);
    document.getElementById('modelLabel').textContent = meta.name;
    document.getElementById('modelChip').title = currentModelId ? ('点击切换模型：' + meta.name) : '点击切换模型';
  }
  function renderEffort() {
    const label = currentEffort === 'none' ? '思考: 关' : currentEffort === 'low' ? '思考: 低' : currentEffort === 'high' ? '思考: 高' : '思考: 最大';
    document.getElementById('effortLabel').textContent = label;
  }
  function modelMeta(id) {
    for (let i = 0; i < MODEL_PRESETS.length; i++) {
      if (MODEL_PRESETS[i].id === id) return MODEL_PRESETS[i];
    }
    const name = String(id || '').replace(/^[^/]+\\//, '') || '选择模型';
    return { id: id, name: name, desc: '自定义模型', color: '#c8c8c8' };
  }
  function modelMark(color) {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('class', 'model-ico');
    svg.setAttribute('viewBox', '0 0 16 16');
    svg.setAttribute('aria-hidden', 'true');
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', 'M8 1.2 9.1 6 14 6.2 10.2 9.1 11.6 14 8 11.2 4.4 14 5.8 9.1 2 6.2 6.9 6Z');
    path.setAttribute('fill', color || '#e3947c');
    svg.appendChild(path);
    return svg;
  }
  function hideModelMenu() {
    document.getElementById('modelMenu').classList.add('hidden');
  }
  function renderModelMenu() {
    const menu = document.getElementById('modelMenu');
    const items = MODEL_PRESETS.slice();
    if (currentModelId && !items.some(function (m) { return m.id === currentModelId; })) {
      items.unshift(modelMeta(currentModelId));
    }
    menu.innerHTML = '';
    items.forEach(function (m) {
      const btn = document.createElement('button');
      btn.type = 'button';
      const active = m.id === currentModelId;
      btn.className = 'model-item' + (active ? ' active' : '');
      btn.setAttribute('role', 'option');
      btn.appendChild(modelMark(m.color));
      const text = document.createElement('span');
      text.className = 'model-text';
      const name = document.createElement('span');
      name.className = 'model-name';
      name.textContent = m.name;
      const desc = document.createElement('span');
      desc.className = 'model-desc';
      desc.textContent = m.desc;
      text.appendChild(name);
      text.appendChild(desc);
      btn.appendChild(text);
      if (active) {
        const mark = document.createElement('span');
        mark.className = 'model-check';
        mark.textContent = '✓';
        btn.appendChild(mark);
      }
      btn.addEventListener('click', function (e) {
        e.stopPropagation();
        chooseModel(m.id);
      });
      menu.appendChild(btn);
    });
    const divider = document.createElement('div');
    divider.className = 'model-divider';
    menu.appendChild(divider);
    const custom = document.createElement('button');
    custom.type = 'button';
    custom.className = 'model-item custom';
    const plus = document.createElement('span');
    plus.className = 'model-plus';
    plus.textContent = '+';
    const plusLabel = document.createElement('span');
    plusLabel.textContent = '添加模型';
    custom.appendChild(plus);
    custom.appendChild(plusLabel);
    custom.addEventListener('click', function (e) {
      e.stopPropagation();
      hideModelMenu();
      vscode.postMessage({ type: 'pickModel' });
    });
    menu.appendChild(custom);
  }
  function toggleModelMenu() {
    const menu = document.getElementById('modelMenu');
    if (!menu.classList.contains('hidden')) {
      hideModelMenu();
      return;
    }
    renderModelMenu();
    const chip = document.getElementById('modelChip');
    const wrap = document.querySelector('.composer-wrap');
    const chipRect = chip.getBoundingClientRect();
    const wrapRect = wrap.getBoundingClientRect();
    menu.classList.remove('hidden');
    const menuW = menu.offsetWidth;
    let left = chipRect.left - wrapRect.left;
    if (left + menuW > wrapRect.width - 8) left = Math.max(8, wrapRect.width - menuW - 8);
    menu.style.left = left + 'px';
    menu.style.bottom = (wrapRect.bottom - chipRect.top + 6) + 'px';
  }
  function chooseModel(id) {
    hideModelMenu();
    currentModelId = id;
    document.getElementById('modelLabel').textContent = modelMeta(id).name;
    document.getElementById('modelChip').title = '点击切换模型：' + modelMeta(id).name;
    vscode.postMessage({ type: 'setModel', model: id });
  }
  function renderContext(payload) {
    const el = document.getElementById('context');
    const tip = document.getElementById('ctxTip');
    if (!payload || !payload.filePath) {
      ctxPath = '';
      el.textContent = '文件上下文';
      el.title = '未打开文件';
      if (tip && !tip.classList.contains('hidden')) tip.textContent = '未打开文件';
      return;
    }
    ctxPath = String(payload.filePath);
    const name = ctxPath.split(/[\\\\/]/).pop() || '文件上下文';
    let s = name;
    if (payload.selection) s += ' ' + payload.selection.startLine + '-' + payload.selection.endLine;
    el.textContent = s;
    el.title = ctxPath;
    if (tip && !tip.classList.contains('hidden')) tip.textContent = ctxPath;
  }
  function escapeHtml(s) {
    return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }
  function inlineMd(s) {
    s = s.replace(/\`([^\`]+)\`/g, '<code>$1</code>');
    s = s.replace(/\\*\\*([^*\\n]+)\\*\\*/g, '<strong>$1</strong>');
    s = s.replace(/\\*([^*\\n]+)\\*/g, '<em>$1</em>');
    s = s.replace(/\\[([^\\]]+)\\]\\(([^)\\s]+)\\)/g, '<a href="$2">$1</a>');
    return s;
  }
  function tableRow(line) {
    const cells = line.split('|');
    if (cells[0] === '') cells.shift();
    if (cells[cells.length - 1] === '') cells.pop();
    return cells.map((c) => inlineMd(escapeHtml(c.trim())));
  }
  function mdToHtml(src) {
    // 先按原文分块检测（> 引用等不能被转义破坏），内容在生成每个元素时再转义
    const lines = src.split('\\n');
    const out = [];
    let i = 0;
    let inCode = false;
    let codeBuf = [];
    let list = null;
    function closeList() { if (list) { out.push('</' + list + '>'); list = null; } }
    while (i < lines.length) {
      const line = lines[i];
      if (line.trim().startsWith('\`\`\`')) {
        closeList();
        if (inCode) { out.push('<pre><code>' + escapeHtml(codeBuf.join('\\n')) + '</code></pre>'); codeBuf = []; inCode = false; }
        else inCode = true;
        i++; continue;
      }
      if (inCode) { codeBuf.push(line); i++; continue; }
      const h = line.match(/^(#{1,4})\\s+(.*)$/);
      if (h) { closeList(); const lvl = h[1].length; out.push('<h' + lvl + '>' + inlineMd(escapeHtml(h[2])) + '</h' + lvl + '>'); i++; continue; }
      if (/^\\s*---+\\s*$/.test(line)) { closeList(); out.push('<hr>'); i++; continue; }
      if (line.trim().startsWith('|')) {
        closeList();
        const rows = [];
        let j = i;
        while (j < lines.length && lines[j].trim().startsWith('|')) { rows.push(lines[j].trim()); j++; }
        if (rows.length >= 2 && /^\\|[\\s:|-]+\\|$/.test(rows[1])) {
          const header = tableRow(rows[0]);
          const body = rows.slice(2).map(tableRow);
          let html = '<table><thead><tr>';
          header.forEach((c) => { html += '<th>' + c + '</th>'; });
          html += '</tr></thead><tbody>';
          body.forEach((r) => { html += '<tr>'; r.forEach((c) => { html += '<td>' + c + '</td>'; }); html += '</tr>'; });
          html += '</tbody></table>';
          out.push(html);
          i = j;
          continue;
        }
        // 不是表格则回退到普通段落
      }
      if (/^\\s*[-*]\\s+/.test(line)) {
        if (list !== 'ul') { closeList(); out.push('<ul>'); list = 'ul'; }
        out.push('<li>' + inlineMd(escapeHtml(line.replace(/^\\s*[-*]\\s+/, ''))) + '</li>');
        i++; continue;
      }
      if (/^\\s*\\d+\\.\\s+/.test(line)) {
        if (list !== 'ol') { closeList(); out.push('<ol>'); list = 'ol'; }
        out.push('<li>' + inlineMd(escapeHtml(line.replace(/^\\s*\\d+\\.\\s+/, ''))) + '</li>');
        i++; continue;
      }
      if (/^\\s*>\\s?/.test(line)) { closeList(); out.push('<blockquote>' + inlineMd(escapeHtml(line.replace(/^\\s*>\\s?/, ''))) + '</blockquote>'); i++; continue; }
      closeList();
      const para = [];
      while (i < lines.length && lines[i].trim() !== '' && !lines[i].trim().startsWith('\`\`\`')
        && !/^(#{1,4})\\s/.test(lines[i]) && !/^\\s*[-*]\\s+/.test(lines[i])
        && !/^\\s*\\d+\\.\\s+/.test(lines[i]) && !/^\\s*>\\s?/.test(lines[i]) && !/^\\s*---+\\s*$/.test(lines[i])) {
        para.push(inlineMd(escapeHtml(lines[i]))); i++;
      }
      if (para.length) out.push('<p>' + para.join('<br>') + '</p>');
      else i++;
    }
    closeList();
    if (inCode) out.push('<pre><code>' + escapeHtml(codeBuf.join('\\n')) + '</code></pre>');
    return out.join('\\n');
  }
  function addBubble(role, text) {
    const wrap = document.createElement('div');
    wrap.className = 'msg' + (role === 'user' ? ' user' : '');
    const r = document.createElement('div');
    r.className = 'role';
    r.textContent = role === 'user' ? '你' : 'WorkCode';
    const b = document.createElement('div');
    b.className = 'bubble ' + role;
    b.textContent = text;
    wrap.appendChild(r);
    wrap.appendChild(b);
    messages.appendChild(wrap);
    messages.scrollTop = messages.scrollHeight;
    syncEmpty();
    return b;
  }
  function addToolLine(payload) {
    const el = document.createElement('div');
    el.className = 'tool-line';
    el.textContent = '🔧 ' + payload.tool + ' ' + JSON.stringify(payload.arguments);
    messages.appendChild(el);
    tools[payload.requestId] = el;
    messages.scrollTop = messages.scrollHeight;
    syncEmpty();
  }
  function finishTool(payload) {
    const task = tasks.filter(function (t) { return t.id === payload.requestId; })[0];
    if (task) {
      task.ok = !!payload.ok;
      if (activeTab === 'tasks') renderTabPanel();
    }
    const el = tools[payload.requestId];
    if (!el) return;
    el.classList.add(payload.ok ? 'ok' : 'err');
    const brief = payload.output.length > 300 ? payload.output.slice(0, 300) + '…' : payload.output;
    const firstLines = brief.split('\\n').slice(0, 6).join('\\n    ');
    el.textContent += '\\n    ' + (payload.ok ? '✓' : '✗') + ' ' + firstLines;
    if (pendingPermission) { pendingPermission.remove(); pendingPermission = null; }
    if (pendingDiff) { pendingDiff.el.remove(); pendingDiff = null; }
  }
  let pendingPermission = null;
  let pendingDiff = null;
  function renderPermission(payload) {
    if (pendingPermission) pendingPermission.remove();
    const box = document.createElement('div');
    box.className = 'perm';
    const head = document.createElement('div');
    head.className = 'perm-head';
    head.textContent = '🔒 ' + payload.description;
    const detail = document.createElement('div');
    detail.className = 'perm-detail';
    try { detail.textContent = JSON.stringify(payload.arguments || {}, null, 2); } catch (e) {}
    const btns = document.createElement('div');
    btns.className = 'btns';
    const allowBtn = document.createElement('button');
    allowBtn.className = 'primary';
    allowBtn.textContent = '允许';
    allowBtn.addEventListener('click', () => {
      vscode.postMessage({ type: 'permission', requestId: payload.requestId, allowed: true });
      box.remove();
      pendingPermission = null;
      setStatus('执行中…');
    });
    const denyBtn = document.createElement('button');
    denyBtn.textContent = '拒绝';
    denyBtn.addEventListener('click', () => {
      vscode.postMessage({ type: 'permission', requestId: payload.requestId, allowed: false });
      box.remove();
      pendingPermission = null;
      setStatus('已拒绝');
    });
    btns.appendChild(allowBtn);
    btns.appendChild(denyBtn);
    box.appendChild(head);
    box.appendChild(detail);
    box.appendChild(btns);
    messages.appendChild(box);
    pendingPermission = box;
    messages.scrollTop = messages.scrollHeight;
    syncEmpty();
    setStatus('等待授权：' + payload.description);
  }
  function renderDiffConfirm(payload) {
    // 新的 Diff 到来时，把上一个仍待确认的按拒绝回掉，避免服务端一直等
    if (pendingDiff) {
      vscode.postMessage({ type: 'applyDiff', requestId: pendingDiff.requestId, accepted: false });
      pendingDiff.el.remove();
    }
    const box = document.createElement('div');
    box.className = 'perm';
    const head = document.createElement('div');
    head.className = 'perm-head';
    head.textContent = '📄 Diff 修改建议：' + payload.path;
    const detail = document.createElement('div');
    detail.className = 'perm-detail';
    detail.textContent = '已在上方编辑器打开对比视图，查看后决定是否应用。';
    const btns = document.createElement('div');
    btns.className = 'btns';
    const acceptBtn = document.createElement('button');
    acceptBtn.className = 'primary';
    acceptBtn.textContent = '接受修改';
    acceptBtn.addEventListener('click', () => {
      vscode.postMessage({ type: 'applyDiff', requestId: payload.requestId, accepted: true });
      box.remove();
      pendingDiff = null;
      setStatus('正在应用…');
    });
    const rejectBtn = document.createElement('button');
    rejectBtn.textContent = '拒绝';
    rejectBtn.addEventListener('click', () => {
      vscode.postMessage({ type: 'applyDiff', requestId: payload.requestId, accepted: false });
      box.remove();
      pendingDiff = null;
      setStatus('已拒绝');
    });
    btns.appendChild(acceptBtn);
    btns.appendChild(rejectBtn);
    box.appendChild(head);
    box.appendChild(detail);
    box.appendChild(btns);
    messages.appendChild(box);
    pendingDiff = { el: box, requestId: payload.requestId };
    const base = String(payload.path || '').split(/[\\\\/]/).pop() || String(payload.path || '');
    editItems.push(base);
    if (activeTab === 'edits') renderTabPanel();
    messages.scrollTop = messages.scrollHeight;
    syncEmpty();
    setStatus('已打开 Diff 预览，等待确认…');
  }
  function send() {
    const content = input.value.trim();
    if (!content) return;
    hideSlashPopup();
    // slash 命令：以 / 开头 → 本地处理，不发模型
    if (handleSlash(content)) {
      input.value = '';
      syncSend();
      return;
    }
    addBubble('user', content);
    touchTitle(content);
    assistant = null;
    assistantText = '';
    reasoningEl = null;
    reasoningText = '';
    setStatus('思考中…');
    setBusy(true);
    input.value = '';
    syncSend();
    vscode.postMessage({ type: 'send', content });
  }

  // ---------- slash 命令 ----------
  const SLASH_COMMANDS = [
    { cmd: '/help', desc: '查看可用命令' },
    { cmd: '/new', desc: '新建会话' },
    { cmd: '/resume', desc: '恢复历史会话' },
    { cmd: '/clear', desc: '清空当前对话' },
    { cmd: '/model', desc: '切换模型' },
    { cmd: '/mode', desc: '切换权限模式 manual / auto / plan', needsArg: true },
    { cmd: '/effort', desc: '切换思考程度 none / low / high / max', needsArg: true },
    { cmd: '/memory', desc: '查看 / 追加跨项目用户记忆，如 /memory 我喜欢用单引号' },
    { cmd: '/cost', desc: '查看本次会话 token 用量' },
  ];
  const SLASH_HELP = SLASH_COMMANDS.map(function (c) {
    return c.cmd + '  ' + c.desc;
  }).join('\\n');
  let sessionTokens = 0;

  function handleSlash(content) {
    const t = content.trim();
    if (!t.startsWith('/')) return false;
    const parts = t.split(/\\s+/);
    const cmd = parts[0].toLowerCase();
    const arg = parts.slice(1).join(' ');

    if (cmd === '/help') {
      addBubble('assistant', '### 可用命令\\n\`\`\`\\n' + SLASH_HELP + '\\n\`\`\`');
    } else if (cmd === '/new') {
      vscode.postMessage({ type: 'newSession' });
    } else if (cmd === '/resume') {
      showHistory();
    } else if (cmd === '/clear') {
      messages.innerHTML = '';
      assistant = null; assistantText = '';
      reasoningEl = null; reasoningText = '';
      syncEmpty();
    } else if (cmd === '/model') {
      vscode.postMessage({ type: 'pickModel' });
    } else if (cmd === '/mode') {
      const m = arg.toLowerCase();
      if (m === 'manual' || m === 'auto' || m === 'plan') {
        vscode.postMessage({ type: 'setMode', mode: m });
      } else {
        addBubble('assistant', '用法：/mode manual | auto | plan');
      }
    } else if (cmd === '/effort') {
      const e = arg.toLowerCase();
      if (e === 'none' || e === 'low' || e === 'high' || e === 'max') {
        currentEffort = e;
        renderEffort();
        vscode.postMessage({ type: 'setEffort', effort: e });
      } else {
        addBubble('assistant', '用法：/effort none | low | high | max');
      }
    } else if (cmd === '/memory') {
      const text = arg.trim();
      if (text) {
        vscode.postMessage({ type: 'memoryAdd', text });
      } else {
        vscode.postMessage({ type: 'memoryGet' });
      }
    } else if (cmd === '/cost') {
      addBubble('assistant', '本次会话累计约 ' + sessionTokens + ' tokens');
    } else {
      addBubble('assistant', '未知命令：' + cmd + '。输入 /help 查看可用命令');
    }
    return true;
  }

  // ---------- @ 引用文件 / slash 命令（输入框触发） ----------
  const atPopup = document.getElementById('atPopup');
  const slashPopup = document.getElementById('slashPopup');
  let atState = null; // { start, token, items, index }
  let atDebounce = null;
  let slashState = null; // { start, token, items, index }

  function updateInputPopups() {
    const pos = input.selectionStart ?? input.value.length;
    const before = input.value.slice(0, pos);
    const slash = /(?:^|\\s)(\\/[^\\s]*)$/.exec(before);
    if (slash) {
      hideAtPopup();
      const token = slash[1];
      const start = pos - token.length;
      const q = token.slice(1).toLowerCase();
      const items = SLASH_COMMANDS.filter(function (c) {
        return !q || c.cmd.slice(1).toLowerCase().indexOf(q) === 0;
      });
      slashState = { start: start, token: token, items: items, index: 0 };
      renderSlashList();
      return;
    }
    hideSlashPopup();
    updateAtPopup();
  }

  function renderSlashList() {
    if (!slashState) return;
    slashPopup.innerHTML = '';
    if (!slashState.items.length) {
      slashPopup.innerHTML = '<div class="slash-item muted">无匹配命令</div>';
    } else {
      slashState.items.forEach(function (c, i) {
        const it = document.createElement('div');
        it.className = 'slash-item' + (i === slashState.index ? ' active' : '');
        const ico = document.createElement('span');
        ico.className = 'slash-ico';
        ico.textContent = '›';
        const name = document.createElement('span');
        name.className = 'slash-cmd';
        name.textContent = c.cmd;
        const desc = document.createElement('span');
        desc.className = 'slash-desc';
        desc.textContent = c.desc;
        it.appendChild(ico);
        it.appendChild(name);
        it.appendChild(desc);
        it.addEventListener('mousedown', function (e) {
          e.preventDefault();
          selectSlash(i);
        });
        slashPopup.appendChild(it);
      });
    }
    const wrap = document.querySelector('.composer-wrap');
    const inputRect = input.getBoundingClientRect();
    const wrapRect = wrap.getBoundingClientRect();
    slashPopup.style.bottom = (wrapRect.bottom - inputRect.top + 4) + 'px';
    slashPopup.classList.remove('hidden');
    const active = slashPopup.querySelector('.slash-item.active');
    if (active && active.scrollIntoView) active.scrollIntoView({ block: 'nearest' });
  }

  function highlightSlash() {
    const items = slashPopup.querySelectorAll('.slash-item');
    for (let i = 0; i < items.length; i++) {
      items[i].className = 'slash-item' + (i === slashState.index ? ' active' : '');
    }
  }

  function selectSlash(i) {
    if (!slashState || !slashState.items.length) return;
    const item = slashState.items[i];
    const end = slashState.start + slashState.token.length;
    if (item.needsArg) {
      const next = input.value.slice(0, slashState.start) + item.cmd + ' ' + input.value.slice(end);
      input.value = next;
      const caret = slashState.start + item.cmd.length + 1;
      input.focus();
      input.selectionStart = input.selectionEnd = caret;
      hideSlashPopup();
      syncSend();
      return;
    }
    const rest = input.value.slice(end);
    input.value = rest.trim() ? item.cmd + ' ' + rest.replace(/^\\s+/, '') : '';
    hideSlashPopup();
    handleSlash(item.cmd);
    syncSend();
    input.focus();
  }

  function hideSlashPopup() {
    slashState = null;
    slashPopup.classList.add('hidden');
    slashPopup.innerHTML = '';
  }

  function updateAtPopup() {
    const input = document.getElementById('input');
    const pos = input.selectionStart ?? input.value.length;
    const before = input.value.slice(0, pos);
    const m = /@([^\s]*)$/.exec(before);
    if (!m) {
      hideAtPopup();
      return;
    }
    const start = pos - m[0].length; // @ 的位置
    atState = { start, token: m[1], items: [], index: 0 };
    clearTimeout(atDebounce);
    atDebounce = setTimeout(() => {
      vscode.postMessage({ type: 'queryFiles', query: atState ? atState.token : '' });
    }, 150);
  }

  function renderAtList(files) {
    if (!atState) return;
    atState.items = files || [];
    atState.index = 0;
    atPopup.innerHTML = '';
    if (!atState.items.length) {
      atPopup.innerHTML = '<div class="at-item muted">无匹配文件</div>';
    } else {
      atState.items.forEach((f, i) => {
        const it = document.createElement('div');
        it.className = 'at-item' + (i === 0 ? ' active' : '');
        it.textContent = f;
        it.addEventListener('click', () => selectAt(i));
        atPopup.appendChild(it);
      });
    }
    atPopup.classList.remove('hidden');
  }

  function highlightAt() {
    const items = atPopup.children;
    for (let i = 0; i < items.length; i++) {
      items[i].className = 'at-item' + (i === atState.index ? ' active' : '');
    }
  }

  function selectAt(i) {
    if (!atState || !atState.items.length) return;
    const input = document.getElementById('input');
    const end = atState.start + 1 + atState.token.length; // @ + token 之后
    const path = atState.items[i];
    input.value =
      input.value.slice(0, atState.start) + '@' + path + ' ' + input.value.slice(end);
    input.focus();
    const caret = atState.start + 1 + path.length;
    input.selectionStart = input.selectionEnd = caret;
    hideAtPopup();
    syncSend();
  }

  function hideAtPopup() {
    atState = null;
    clearTimeout(atDebounce);
    atPopup.classList.add('hidden');
    atPopup.innerHTML = '';
  }
  function showHistory() {
    hideModelMenu();
    console.log('[workcode] showHistory 被点击');
    document.getElementById('stage').classList.add('hidden');
    document.getElementById('dock').classList.add('hidden');
    document.getElementById('searchBar').classList.add('hidden');
    document.getElementById('history').classList.remove('hidden');
    document.getElementById('historyList').innerHTML =
      '<div class="history-item muted">加载中…</div>';
    vscode.postMessage({ type: 'listSessions' });
  }
  function closeHistory() {
    document.getElementById('history').classList.add('hidden');
    document.getElementById('stage').classList.remove('hidden');
    document.getElementById('dock').classList.remove('hidden');
    syncEmpty();
  }
  function renderSessionList(sessions) {
    console.log('[workcode] 收到会话列表，数量', sessions ? sessions.length : 0);
    const list = document.getElementById('historyList');
    if (!sessions || sessions.length === 0) {
      list.innerHTML = '<div class="history-item muted">暂无历史会话</div>';
      return;
    }
    list.innerHTML = '';
    for (const s of sessions) {
      const item = document.createElement('div');
      item.className = 'history-item';
      const p = document.createElement('div');
      p.className = 'preview';
      p.textContent = s.preview;
      const t = document.createElement('div');
      t.className = 'time';
      t.textContent = new Date(s.created_at * 1000).toLocaleString();
      item.appendChild(p);
      item.appendChild(t);
      item.addEventListener('click', () => {
        vscode.postMessage({ type: 'resumeSession', sessionId: s.id });
      });
      list.appendChild(item);
    }
  }
  function renderResumed(data) {
    messages.innerHTML = '';
    assistant = null;
    assistantText = '';
    tasks.length = 0;
    editItems.length = 0;
    resetTitle();
    let titled = false;
    for (const m of data.messages || []) {
      if (m.role === 'user' && m.content) {
        if (!titled) { touchTitle(String(m.content)); titled = true; }
        addBubble('user', String(m.content));
      } else if (m.role === 'assistant' && m.content) {
        const b = addBubble('assistant', '');
        b.classList.add('md');
        b.innerHTML = mdToHtml(String(m.content));
      } else if (m.role === 'system' && m.content && String(m.content).startsWith('（历史摘要）')) {
        addBubble('assistant', '📌 ' + String(m.content));
      }
      // tool 消息跳过（内部细节）
    }
    setStatus('会话 ' + String(data.sessionId).slice(0, 8));
  }
  window.addEventListener('message', (e) => {
    const msg = e.data;
    if (!msg) return;
    if (msg.type === 'resume') {
      renderResumed(msg);
      closeHistory();
      return;
    }
    if (msg.type === 'sessionList') {
      renderSessionList(msg.sessions || []);
      return;
    }
    if (msg.type === 'fileList') {
      renderAtList(msg.files || []);
      return;
    }
    if (msg.type === 'memoryContent') {
      const content = msg.content || '';
      addBubble('assistant', '### 用户记忆 (memory.md)\\n\`\`\`\\n' + (content || '(空)') + '\\n\`\`\`');
      return;
    }
    if (msg.type === 'context') {
      renderContext(msg.payload);
      return;
    }
    if (msg.type !== 'event') return;
    const evt = msg.event;
    switch (evt.type) {
      case 'ready':
        setStatus(evt.payload.message || '就绪');
        renderState(evt.payload.model, evt.payload.mode);
        break;
      case 'session_started':
        setStatus('会话 ' + String(evt.payload.sessionId).slice(0, 8));
        break;
      case 'reasoning_delta': {
        if (!reasoningEl) {
          reasoningEl = document.createElement('div');
          reasoningEl.className = 'reasoning';
          reasoningEl.textContent = '🤔 思考中…';
          messages.appendChild(reasoningEl);
          reasoningText = '';
        }
        reasoningText += evt.payload.delta;
        reasoningEl.textContent = '🤔 ' + reasoningText;
        messages.scrollTop = messages.scrollHeight;
        break;
      }
      case 'text_delta':
        if (!assistant) {
          assistant = addBubble('assistant', '');
          assistant.classList.add('md');
          assistantText = '';
        }
        assistantText += evt.payload.delta;
        try {
          assistant.innerHTML = mdToHtml(assistantText);
        } catch (err) {
          // 渲染兜底：万一有解析异常，回退为纯文本，保证输出始终可见；同时打印真实报错便于定位
          console.error('[workcode] 渲染失败，回退纯文本:', err);
          assistant.innerHTML = '';
          assistant.textContent = assistantText;
        }
        messages.scrollTop = messages.scrollHeight;
        break;
      case 'tool_call':
        // 工具调用后开新气泡：避免工具前的文本和最终答案粘连成一行，导致标题识别失败
        assistant = null;
        assistantText = '';
        reasoningEl = null;
        reasoningText = '';
        (function () {
          const args = evt.payload.arguments || {};
          let brief = '';
          try { brief = JSON.stringify(args); } catch (err) { brief = ''; }
          if (brief.length > 80) brief = brief.slice(0, 80) + '…';
          tasks.push({ id: evt.payload.requestId, tool: evt.payload.tool || 'tool', brief: brief, ok: null });
          if (activeTab === 'tasks') renderTabPanel();
        })();
        addToolLine(evt.payload);
        break;
      case 'tool_result':
        finishTool(evt.payload);
        break;
      case 'permission_request':
        renderPermission(evt.payload);
        break;
      case 'diff_preview':
        renderDiffConfirm(evt.payload);
        break;
      case 'error':
        addBubble('error', '⚠ ' + evt.payload.message);
        setBusy(false);
        break;
      case 'done': {
        const usage = evt.payload.usage || {};
        const prompt = usage.promptTokens || 0;
        const total = prompt + (usage.completionTokens || 0);
        sessionTokens += total;
        const pct = prompt ? Math.min(100, Math.round(prompt / 128000 * 100)) : 0;
        setUsagePct(pct, total ? ('本轮约 ' + total + ' tokens') : '');
        setBusy(false);
        setStatus('✓ 完成');
        assistant = null;
        assistantText = '';
        reasoningEl = null;
        reasoningText = '';
        break;
      }
    }
  });

  document.getElementById('send').addEventListener('click', send);
  document.getElementById('modeChip').addEventListener('click', () => vscode.postMessage({ type: 'toggleMode' }));
  document.getElementById('effortChip').addEventListener('click', () => {
    const order = ['none', 'low', 'high', 'max'];
    const idx = order.indexOf(currentEffort);
    currentEffort = order[(idx + 1) % order.length];
    renderEffort();
    vscode.postMessage({ type: 'toggleEffort' });
  });
  document.getElementById('modelChip').addEventListener('click', (e) => {
    e.stopPropagation();
    toggleModelMenu();
  });
  document.addEventListener('click', (e) => {
    const menu = document.getElementById('modelMenu');
    if (!menu.classList.contains('hidden') && !(e.target.closest && e.target.closest('#modelMenu'))) {
      hideModelMenu();
    }
    if (slashState && !(e.target.closest && (e.target.closest('#slashPopup') || e.target.closest('#input')))) {
      hideSlashPopup();
    }
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') hideModelMenu();
  });
  document.getElementById('historyBtn').addEventListener('click', showHistory);
  document.getElementById('historyClose').addEventListener('click', closeHistory);
  document.getElementById('cancel').addEventListener('click', () => {
    setBusy(false);
    vscode.postMessage({ type: 'cancel' });
  });
  document.getElementById('new').addEventListener('click', () => {
    messages.innerHTML = '';
    assistant = null;
    assistantText = '';
    tasks.length = 0;
    editItems.length = 0;
    activeTab = '';
    document.querySelectorAll('.tab').forEach(function (b) { b.classList.remove('active'); });
    renderTabPanel();
    resetTitle();
    setBusy(false);
    setStatus('');
    syncEmpty();
    closeHistory();
    vscode.postMessage({ type: 'newSession' });
  });
  document.getElementById('searchBtn').addEventListener('click', () => {
    const bar = document.getElementById('searchBar');
    bar.classList.toggle('hidden');
    if (!bar.classList.contains('hidden')) document.getElementById('searchInput').focus();
  });
  document.getElementById('searchInput').addEventListener('input', () => {
    const q = document.getElementById('searchInput').value.trim().toLowerCase();
    const historyOpen = !document.getElementById('history').classList.contains('hidden');
    const nodes = historyOpen
      ? document.querySelectorAll('#historyList .history-item')
      : document.querySelectorAll('#messages .msg, #messages .tool-line, #messages .perm');
    nodes.forEach(function (el) {
      el.style.display = !q || el.textContent.toLowerCase().indexOf(q) >= 0 ? '' : 'none';
    });
  });
  document.getElementById('settingsBtn').addEventListener('click', () => vscode.postMessage({ type: 'openSettings' }));
  document.getElementById('editBtn').addEventListener('click', () => selectTab('edits'));
  document.querySelectorAll('.tab').forEach(function (btn) {
    btn.addEventListener('click', function () { selectTab(btn.getAttribute('data-tab')); });
  });
  document.getElementById('bannerClose').addEventListener('click', () => {
    document.getElementById('banner').classList.add('hidden');
    const prev = vscode.getState() || {};
    prev.hideBanner = true;
    vscode.setState(prev);
  });
  document.getElementById('ctxBtn').addEventListener('click', () => {
    const tip = document.getElementById('ctxTip');
    tip.textContent = ctxPath || '未打开文件';
    tip.classList.toggle('hidden');
  });
  document.getElementById('refreshCtx').addEventListener('click', () => vscode.postMessage({ type: 'refreshContext' }));
  // 输入框：@ 触发文件引用浮层
  input.addEventListener('input', () => { updateInputPopups(); syncSend(); });
  input.addEventListener('keydown', (e) => {
    if (slashState && slashState.items.length) {
      if (e.key === 'ArrowDown') { e.preventDefault(); slashState.index = (slashState.index + 1) % slashState.items.length; highlightSlash(); return; }
      if (e.key === 'ArrowUp') { e.preventDefault(); slashState.index = (slashState.index - 1 + slashState.items.length) % slashState.items.length; highlightSlash(); return; }
      if (e.key === 'Enter' || e.key === 'Tab') { e.preventDefault(); selectSlash(slashState.index); return; }
      if (e.key === 'Escape') { e.preventDefault(); hideSlashPopup(); return; }
    }
    if (atState && atState.items.length) {
      if (e.key === 'ArrowDown') { e.preventDefault(); atState.index = (atState.index + 1) % atState.items.length; highlightAt(); return; }
      if (e.key === 'ArrowUp') { e.preventDefault(); atState.index = (atState.index - 1 + atState.items.length) % atState.items.length; highlightAt(); return; }
      if (e.key === 'Enter') { e.preventDefault(); selectAt(atState.index); return; }
      if (e.key === 'Escape') { e.preventDefault(); hideAtPopup(); return; }
    }
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  });
  // 通知宿主面板已加载，宿主会推送当前文件/选区上下文
  vscode.postMessage({ type: 'ready' });
</script>
</body>
</html>`;
}
