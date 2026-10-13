import * as vscode from 'vscode';
import { ChatPanelProvider } from './chatPanel';
import { ServiceClient } from './serviceClient';

export function registerCommands(
  context: vscode.ExtensionContext,
  client: ServiceClient,
  provider: ChatPanelProvider
): void {
  context.subscriptions.push(
    vscode.commands.registerCommand('workcode.chat', () => {
      void vscode.commands.executeCommand('workcode.chat.focus');
    }),

    vscode.commands.registerCommand('workcode.explain', () => {
      void vscode.commands.executeCommand('workcode.chat.focus');
      void provider.explainSelection();
    }),

    vscode.commands.registerCommand('workcode.newSession', () => {
      client.newSession();
    }),

    vscode.commands.registerCommand('workcode.resume', async () => {
      try {
        await client.ensureRunning();
      } catch (err) {
        void vscode.window.showErrorMessage(
          `WorkCode 服务不可用：${err instanceof Error ? err.message : String(err)}`
        );
        return;
      }
      const sessions = await client.listSessions();
      if (!sessions || sessions.length === 0) {
        void vscode.window.showInformationMessage('没有可恢复的历史会话。');
        return;
      }
      const items = sessions.map((s) => ({
        label: s.preview,
        description: new Date(s.created_at * 1000).toLocaleString(),
        detail: s.id,
      }));
      const picked = await vscode.window.showQuickPick(items, {
        placeHolder: '选择要恢复的历史会话',
      });
      if (picked && picked.detail) {
        await client.resumeSession(picked.detail);
        await vscode.commands.executeCommand('workcode.chat.focus');
      }
    }),

    vscode.commands.registerCommand('workcode.setApiKey', async () => {
      const value = await vscode.window.showInputBox({
        prompt: '输入 DeepSeek API Key（将安全存储到 SecretStorage）',
        password: true,
        ignoreFocusOut: true,
        placeHolder: 'sk-…',
      });
      if (value && value.trim()) {
        await context.secrets.store('workcode.apiKey', value.trim());
        void vscode.window.showInformationMessage('WorkCode API Key 已保存。');
        // 重新连接让服务拿到新 Key
        await client.ensureRunning();
      }
    })
  );
}
