import * as path from 'path';
import * as vscode from 'vscode';
import type { DiffPreviewPayload } from '@workcode/shared';

const SCHEME_ORIG = 'workcode-orig';
const SCHEME_NEW = 'workcode-new';

interface DiffEntry {
  original: string;
  newContent: string;
  path: string;
}

/**
 * Diff 预览：用 TextDocumentContentProvider 提供「原始 / 修改后」两个虚拟文档，
 * 调用 VS Code 原生 vscode.diff 打开并排对比。
 *
 * 「接受 / 拒绝」的确认由聊天面板里的按钮完成（不在这里弹通知——
 * 通知几秒就消失，用户来不及决定）。
 */
export class DiffManager implements vscode.Disposable {
  private readonly diffs = new Map<string, DiffEntry>();
  private readonly emitter = new vscode.EventEmitter<vscode.Uri>();

  private readonly provider: vscode.TextDocumentContentProvider = {
    onDidChange: this.emitter.event,
    provideTextDocumentContent: (uri) => this.provideContent(uri),
  };

  constructor() {
    vscode.workspace.registerTextDocumentContentProvider(SCHEME_ORIG, this.provider);
    vscode.workspace.registerTextDocumentContentProvider(SCHEME_NEW, this.provider);
  }

  /** 处理服务发来的 diff_preview：打开 Diff 编辑器。确认在聊天面板完成。 */
  async showDiff(payload: DiffPreviewPayload): Promise<void> {
    this.diffs.set(payload.requestId, {
      original: payload.originalContent,
      newContent: payload.newContent,
      path: payload.path,
    });

    const left = this.makeUri(SCHEME_ORIG, payload);
    const right = this.makeUri(SCHEME_NEW, payload);
    const title = `WorkCode 修改建议：${path.basename(payload.path)}`;

    await vscode.commands.executeCommand('vscode.diff', left, right, title);
  }

  private makeUri(scheme: string, payload: DiffPreviewPayload): vscode.Uri {
    // path 保留真实扩展名，便于语法高亮；query 里带 requestId 定位内容
    const p = payload.path.startsWith('/') ? payload.path : '/' + payload.path;
    return vscode.Uri.from({
      scheme,
      path: p,
      query: 'id=' + encodeURIComponent(payload.requestId),
    });
  }

  private provideContent(uri: vscode.Uri): string {
    const id = new URLSearchParams(uri.query).get('id') ?? '';
    const entry = this.diffs.get(id);
    if (!entry) return '';
    return uri.scheme === SCHEME_ORIG ? entry.original : entry.newContent;
  }

  dispose(): void {
    this.emitter.dispose();
    this.diffs.clear();
  }
}
