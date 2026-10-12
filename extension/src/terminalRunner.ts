import * as vscode from 'vscode';
import type { TerminalExecPayload, TerminalExecResultPayload } from '@workcode/shared';
import { ServiceClient } from './serviceClient';

/** 命令持续运行超过该时长仍未结束 → 判定为长驻进程，报告 stillRunning */
const CAPTURE_TIMEOUT_MS = 12_000;

/**
 * 在 VS Code 集成终端里执行 Agent 的命令。
 *
 * - 复用同一个终端（cwd / 环境变量跨命令保持）；上个命令仍占着终端时，新命令开新终端
 * - 用 shell 集成 executeCommand 执行；read() 流式收集输出，流结束即命令完成
 * - 长驻进程（npm run dev）：超过超时仍未结束 → 报告「仍在运行」，终端留给用户
 */
export class TerminalRunner implements vscode.Disposable {
  private terminal: vscode.Terminal | undefined;
  private busy = false;

  constructor(private readonly client: ServiceClient) {}

  async run(payload: TerminalExecPayload): Promise<void> {
    const { requestId, command } = payload;
    try {
      const terminal = this.getTerminal(payload.cwd);
      const shellIntegration = await this.getShellIntegration(terminal);

      if (!shellIntegration) {
        // shell 集成不可用（旧版/自定义 shell）：退化为 sendText，无法可靠捕获输出
        terminal.show(true);
        terminal.sendText(command);
        this.client.respondTerminalExecResult(requestId, {
          ok: true,
          output: '（命令已发送到 VS Code 终端；shell 集成不可用，无法捕获输出与退出码）',
        });
        return;
      }

      terminal.show(true);
      const execution = shellIntegration.executeCommand(command);
      const result = await this.capture(execution, command);
      this.busy = result.stillRunning === true;

      this.client.respondTerminalExecResult(requestId, result);
    } catch (err) {
      this.busy = false;
      this.client.respondTerminalExecResult(requestId, {
        ok: false,
        output: `终端执行失败：${err instanceof Error ? err.message : String(err)}`,
      });
    }
  }

  private getTerminal(cwd?: string): vscode.Terminal {
    if (!this.busy && this.terminal) {
      return this.terminal;
    }
    this.busy = false;
    this.terminal = vscode.window.createTerminal({
      name: 'WorkCode',
      cwd,
      env: { ...process.env },
    });
    return this.terminal;
  }

  private async getShellIntegration(
    terminal: vscode.Terminal
  ): Promise<vscode.TerminalShellIntegration | undefined> {
    for (let i = 0; i < 40; i++) {
      const si = terminal.shellIntegration;
      if (si) return si;
      await this.delay(250);
    }
    return undefined;
  }

  private async capture(
    execution: vscode.TerminalShellExecution,
    command: string
  ): Promise<Omit<TerminalExecResultPayload, 'requestId'>> {
    const startedAt = Date.now();
    let output = '';
    let done = false;
    let endSub: vscode.Disposable | undefined;

    // 退出码来自 window.onDidEndTerminalShellExecution 事件
    const exitCodePromise = new Promise<number | undefined>((resolve) => {
      endSub = vscode.window.onDidEndTerminalShellExecution((event) => {
        if (event.execution === execution) {
          endSub?.dispose();
          resolve(event.exitCode);
        }
      });
    });

    // 必须紧接 executeCommand 调用 read()，流式收集输出；流结束即命令完成
    const readTask = (async () => {
      try {
        for await (const chunk of execution.read()) {
          output += chunk;
        }
      } catch {
        // 命令被中断或终端关闭
      }
      done = true;
    })();

    while (!done && Date.now() - startedAt < CAPTURE_TIMEOUT_MS) {
      await this.delay(150);
    }

    output = output.trim();

    if (!done) {
      // 长驻进程：仍在运行，终端留给用户
      endSub?.dispose();
      return {
        ok: true,
        output: (output || `命令已启动：${command}`) + '\n（命令仍在运行，可查看 VS Code 终端）',
        stillRunning: true,
      };
    }

    // 命令已结束：从事件拿退出码（给事件一个微任务宽限期）
    const exitCode = await Promise.race([exitCodePromise, this.delay(500).then(() => undefined)]);
    return {
      ok: (exitCode ?? 0) === 0,
      output: output || `(无输出，退出码 ${exitCode ?? 0})`,
      exitCode,
    };
  }

  private delay(ms: number): Promise<void> {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  dispose(): void {
    // 终端留给用户关闭，不做额外清理
  }
}
