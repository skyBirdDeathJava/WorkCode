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
