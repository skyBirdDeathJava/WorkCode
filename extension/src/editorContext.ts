import * as vscode from 'vscode';
import type { EditorContext } from '@workcode/shared';

const MAX_FILE_CHARS = 60_000;

/** 采集编辑器上下文：工作区根目录、当前文件、选中代码、已打开文件列表。 */
export function buildEditorContext(): EditorContext {
  const workspaceRoot = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ?? '';

  const editor = vscode.window.activeTextEditor;
  let currentFile: EditorContext['currentFile'] = null;
  let selection: EditorContext['selection'] = null;

  if (editor) {
    const doc = editor.document;
    const text = doc.getText();
    const content =
      text.length > MAX_FILE_CHARS
        ? text.slice(0, MAX_FILE_CHARS) + '\n//...(文件过大，已截断)'
        : text;
    currentFile = { path: doc.uri.fsPath, content, languageId: doc.languageId };

    if (!editor.selection.isEmpty) {
      selection = {
        text: doc.getText(editor.selection),
        startLine: editor.selection.start.line + 1,
        endLine: editor.selection.end.line + 1,
      };
    }
  }

  const openFiles = vscode.workspace.textDocuments
    .map((d) => d.uri.fsPath)
    .filter((p): p is string => Boolean(p))
    .slice(0, 15);

  return { workspaceRoot, currentFile, selection, openFiles };
}

/** 只含文件路径与选区行号的轻量上下文，用于聊天面板顶部的上下文栏（不含文件内容）。 */
export interface ContextSummary {
  filePath: string | null;
  languageId?: string;
  selection: { startLine: number; endLine: number } | null;
}

export function buildContextSummary(): ContextSummary {
  const editor = vscode.window.activeTextEditor;
  if (!editor) {
    return { filePath: null, selection: null };
  }
  const sel = editor.selection;
  const selection = sel.isEmpty
    ? null
    : { startLine: sel.start.line + 1, endLine: sel.end.line + 1 };
  return {
    filePath: editor.document.uri.fsPath,
    languageId: editor.document.languageId,
    selection,
  };
}
