import * as vscode from 'vscode';
import { ChatPanelProvider } from './chatPanel';
import { registerCommands } from './commands';
import { DiffManager } from './diffManager';
import { ServiceClient } from './serviceClient';
import { TerminalRunner } from './terminalRunner';

export function activate(context: vscode.ExtensionContext): void {
  const client = new ServiceClient(context);
  const diffs = new DiffManager();
  const terminalRunner = new TerminalRunner(client);
  const provider = new ChatPanelProvider(client, diffs, terminalRunner);

  context.subscriptions.push(
    vscode.window.registerWebviewViewProvider('workcode.chat', provider, {
      webviewOptions: { retainContextWhenHidden: true },
    })
  );

  registerCommands(context, client, provider);

  // 当前文件/选区变化时，实时更新聊天面板顶部上下文栏
  context.subscriptions.push(
    vscode.window.onDidChangeActiveTextEditor(() => provider.pushContext()),
    vscode.window.onDidChangeTextEditorSelection(() => provider.pushContext())
  );

  // 启动本地服务（后台进行，不阻塞激活）
  void client.ensureRunning().catch((err: unknown) => {
    void vscode.window.showErrorMessage(
      `WorkCode 服务启动失败：${err instanceof Error ? err.message : String(err)}`
    );
  });

  context.subscriptions.push(client, diffs, terminalRunner);
}

export function deactivate(): void {
  // ServiceClient 在 dispose 时杀掉服务子进程
}
