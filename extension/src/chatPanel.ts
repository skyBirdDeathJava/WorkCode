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
        case 'pickFile':
          void this.pickFileForReference();
          break;
        case 'ready':
          this.pushContext();
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

  /** 选择工作区文件，把路径作为 @引用 插入聊天输入框。 */
  private async pickFileForReference(): Promise<void> {
    const files = await getWorkspaceFiles();
    if (files.length === 0) {
      void vscode.window.showInformationMessage('工作区里没有可引用的文件。');
      return;
    }
    const picked = await vscode.window.showQuickPick(
      files.map((f) => ({ label: path.basename(f), description: f })),
      { placeHolder: '选择要 @引用的文件' }
    );
    if (picked) {
      void this.view?.webview.postMessage({ type: 'insertFile', path: picked.description });
    }
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
    background: var(--vscode-sideBar-background, #1e1e1e);
    color: var(--vscode-sideBar-foreground, #cccccc);
    font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
    font-size: 13px;
  }
  #status {
    padding: 6px 10px; font-size: 11px;
    color: var(--vscode-descriptionForeground, #9d9d9d);
    border-bottom: 1px solid var(--vscode-sideBarSectionHeader-border, #3c3c3c);
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  #context {
    padding: 4px 10px; font-size: 11px; font-family: "SF Mono", Menlo, monospace;
    color: var(--vscode-descriptionForeground, #9d9d9d);
    border-bottom: 1px solid var(--vscode-sideBarSectionHeader-border, #3c3c3c);
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  #context .sel { color: var(--vscode-focusBorder, #3794ff); }
  #messages { flex: 1; overflow-y: auto; padding: 10px; }
  .msg { margin-bottom: 10px; }
  .role { font-size: 11px; color: var(--vscode-descriptionForeground, #9d9d9d); margin-bottom: 3px; }
  .bubble {
    background: var(--vscode-editorWidget-background, #252526);
    border-radius: 6px; padding: 8px 10px;
    white-space: pre-wrap; word-break: break-word; line-height: 1.5;
  }
  .bubble.user { background: #0e639c33; }
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
  #composer { border-top: 1px solid var(--vscode-sideBarSectionHeader-border, #3c3c3c); padding: 8px; display: flex; flex-direction: column; gap: 6px; }
  textarea {
    width: 100%; resize: none;
    background: var(--vscode-input-background, #1e1e1e);
    color: var(--vscode-input-foreground, #cccccc);
    border: 1px solid var(--vscode-input-border, #3c3c3c);
    border-radius: 4px; padding: 8px; min-height: 60px; font: inherit;
  }
  textarea:focus { outline: none; border-color: var(--vscode-focusBorder, #3794ff); }
  .btns { display: flex; gap: 6px; }
  button {
    background: var(--vscode-button-secondaryBackground, #3a3a3a);
    color: var(--vscode-button-secondaryForeground, #cccccc);
    border: none; border-radius: 4px; padding: 5px 12px; cursor: pointer; font: inherit;
  }
  button.primary { background: var(--vscode-button-background, #0e639c); color: var(--vscode-button-foreground, #fff); }
  button:hover { filter: brightness(1.1); }
  .hint { font-size: 11px; color: var(--vscode-descriptionForeground, #9d9d9d); text-align: right; }
  .hidden { display: none !important; }
  .history { flex: 1; display: flex; flex-direction: column; min-height: 0; }
  .history-head {
    padding: 8px 10px; font-weight: 600;
    display: flex; justify-content: space-between; align-items: center;
    border-bottom: 1px solid var(--vscode-sideBarSectionHeader-border, #3c3c3c);
  }
  .history-list { flex: 1; overflow-y: auto; padding: 8px; }
  .history-item {
    background: var(--vscode-editorWidget-background, #252526);
    border-radius: 6px; padding: 8px 10px; margin-bottom: 6px; cursor: pointer;
  }
  .history-item:hover { background: #3a3a3a; }
  .history-item .preview { font-size: 12px; white-space: pre-wrap; word-break: break-word; }
  .history-item .time { font-size: 11px; color: var(--vscode-descriptionForeground, #9d9d9d); margin-top: 4px; }
  .history-item.muted { cursor: default; color: var(--vscode-descriptionForeground, #9d9d9d); }
</style>
</head>
<body>
  <div id="status">WorkCode · 启动中…</div>
  <div id="context">…</div>
  <div id="messages"></div>
  <div id="history" class="history hidden">
    <div class="history-head">历史会话 <button id="historyClose">×</button></div>
    <div id="historyList" class="history-list"></div>
  </div>
  <div id="composer">
    <textarea id="input" placeholder="输入问题，Enter 发送 / Shift+Enter 换行"></textarea>
    <div class="btns">
      <button id="send" class="primary">发送</button>
      <button id="atBtn" title="引用文件 (@)">@</button>
      <button id="historyBtn" title="历史会话">🕘</button>
      <button id="cancel">停止</button>
      <button id="new">新会话</button>
    </div>
    <div class="hint">选中代码后，命令面板执行 “WorkCode: 解释选中代码”</div>
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
  const tools = {};

  function setStatus(text) {
    document.getElementById('status').textContent = 'WorkCode · ' + text;
  }
  function renderContext(payload) {
    const el = document.getElementById('context');
    if (!payload.filePath) {
      el.textContent = '未打开文件';
      return;
    }
    let s = payload.filePath;
    if (payload.selection) {
      s += ' · 选中 ' + payload.selection.startLine + '-' + payload.selection.endLine + ' 行';
    }
    el.textContent = s;
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
    wrap.className = 'msg';
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
    return b;
  }
  function addToolLine(payload) {
    const el = document.createElement('div');
    el.className = 'tool-line';
    el.textContent = '🔧 ' + payload.tool + ' ' + JSON.stringify(payload.arguments);
    messages.appendChild(el);
    tools[payload.requestId] = el;
    messages.scrollTop = messages.scrollHeight;
  }
  function finishTool(payload) {
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
    messages.scrollTop = messages.scrollHeight;
    setStatus('已打开 Diff 预览，等待确认…');
  }
  function send() {
    const content = input.value.trim();
    if (!content) return;
    addBubble('user', content);
    assistant = null;
    assistantText = '';
    setStatus('思考中…');
    input.value = '';
    vscode.postMessage({ type: 'send', content });
  }

  function insertFileRef(path) {
    const input = document.getElementById('input');
    const cursor = input.selectionStart ?? input.value.length;
    const at = cursor > 0 && input.value[cursor - 1] !== '@' ? '@' : '';
    input.value = input.value.slice(0, cursor) + at + path + ' ' + input.value.slice(cursor);
    input.focus();
    input.selectionStart = input.selectionEnd = cursor + at.length + path.length;
  }
  function showHistory() {
    console.log('[workcode] showHistory 被点击');
    messages.classList.add('hidden');
    document.getElementById('composer').classList.add('hidden');
    document.getElementById('history').classList.remove('hidden');
    document.getElementById('historyList').innerHTML =
      '<div class="history-item muted">加载中…</div>';
    vscode.postMessage({ type: 'listSessions' });
  }
  function closeHistory() {
    document.getElementById('history').classList.add('hidden');
    messages.classList.remove('hidden');
    document.getElementById('composer').classList.remove('hidden');
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
    for (const m of data.messages || []) {
      if (m.role === 'user' && m.content) {
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
    if (msg.type === 'insertFile') {
      insertFileRef(msg.path || '');
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
        break;
      case 'session_started':
        setStatus('会话 ' + String(evt.payload.sessionId).slice(0, 8));
        break;
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
        break;
      case 'done':
        setStatus('✓ 完成');
        assistant = null;
        assistantText = '';
        break;
    }
  });

  document.getElementById('send').addEventListener('click', send);
  document.getElementById('atBtn').addEventListener('click', () => vscode.postMessage({ type: 'pickFile' }));
  document.getElementById('historyBtn').addEventListener('click', showHistory);
  document.getElementById('historyClose').addEventListener('click', closeHistory);
  document.getElementById('cancel').addEventListener('click', () => vscode.postMessage({ type: 'cancel' }));
  document.getElementById('new').addEventListener('click', () => {
    messages.innerHTML = '';
    assistant = null;
    assistantText = '';
    vscode.postMessage({ type: 'newSession' });
  });
  input.addEventListener('keydown', (e) => {
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
