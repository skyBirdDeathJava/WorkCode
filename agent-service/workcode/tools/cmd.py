"""命令执行工具 run_command（默认 ask，危险命令 deny）。

两种执行方式：
- `run_command()`：一次性子进程（独立的 shell，用于测试与回退）
- `TerminalSession`：基于 pty 的长驻终端，cwd / 环境变量跨命令保持，
  且程序拿到真实 tty（颜色、isatty 判断、交互式读取都可用）

双层防护：网关在放行前做 allow/deny/ask 决策；工具内再校验一次
危险命令黑名单（纵深防御），命中直接拒绝执行。
"""

from __future__ import annotations

import asyncio
import os
import pty
import re
import shlex
import signal
import termios
import uuid

from ..gateway import DANGEROUS_CMD_PATTERNS

MAX_OUTPUT_CHARS = 8000

# ANSI 转义序列（CSI / OSC），剥离后工具结果更可读
ANSI_CSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
ANSI_OSC = re.compile(r"\x1b\][^\x07]*(?:\x07|\x1b\\)")


def _strip_ansi(s: str) -> str:
    s = ANSI_OSC.sub("", s)
    s = ANSI_CSI.sub("", s)
    s = re.sub(r"\x1b.", "", s)  # 两字符转义（\x1b=、\x1b> 等）
    return s.replace("\x1b", "")  # 兜底：剥离任何残留 ESC


async def run_command(
    command: str,
    cwd: str | None = None,
    timeout: float = 30.0,
    input: str | None = None,
) -> tuple[bool, str]:
    if not command or not command.strip():
        return (False, "命令为空")
    # 纵深防御：黑名单命令即使绕过网关也不执行
    if any(re.search(p, command) for p in DANGEROUS_CMD_PATTERNS):
        return (False, f"命令被拦截（危险操作）：{command[:120]}")

    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.PIPE if input is not None else None,
            cwd=cwd or None,
            env={**os.environ},
        )
    except OSError as e:
        return (False, f"无法启动命令: {e}")

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(input=input), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.wait()
        except ProcessLookupError:
            pass
        return (False, f"命令执行超时（>{timeout:.0f}s）：{command[:120]}")

    output = (stdout.decode(errors="replace") + stderr.decode(errors="replace")).strip()
    if len(output) > MAX_OUTPUT_CHARS:
        output = output[:MAX_OUTPUT_CHARS] + "\n...(输出过长，已截断)"
    if not output:
        output = f"(无输出，退出码 {proc.returncode})"

    if proc.returncode != 0:
        return (False, f"退出码 {proc.returncode}：{output[:4000]}")
    return (True, output)


class TerminalSession:
    """基于 pty 的长驻终端会话。

    同一个 bash 进程持续执行命令，cwd、环境变量在命令之间保持；程序拿到真实 tty。
    用「命令 + 唯一标记 + $?」读取每条命令的输出与退出码：
    - 关闭 ECHO 避免输入（含标记）被回显；置空 PS1/PS2 消除交互提示符噪音
    - 启动时预热，吃掉 macOS bash 3.2 的「default interactive shell」通知
    - `input` 参数在标记前写入，供交互式程序读取（read、菜单等）
    - 超时 / 命令退出 shell：杀掉整个进程组并重启
    """

    def __init__(self, shell: str = "/bin/bash", cwd: str | None = None) -> None:
        self.shell = shell
        self.initial_cwd = cwd
        self._master_fd: int | None = None
        self._child_pid: int | None = None
        self._buf = bytearray()
        self._lock = asyncio.Lock()
        self._positioned = False
        self._dead = False
        self._data_event = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None

    async def start(self) -> None:
        if self._master_fd is not None and not self._dead:
            return
        await self.close()  # 清理旧会话
        self._loop = asyncio.get_running_loop()
        pid, master_fd = pty.fork()
        if pid == 0:
            # 子进程：exec bash（pty.fork 已设置会话与控制终端）
            # TERM=dumb 关闭 readline 花哨重绘，输出更干净；ANSI 输出由 _strip_ansi 兜底
            env = {
                **os.environ,
                "PS1": "",
                "PS2": "",
                "PROMPT_COMMAND": "",
                "TERM": "dumb",
            }
            try:
                if self.initial_cwd:
                    os.chdir(self.initial_cwd)
                os.execve(self.shell, ["bash", "--norc"], env)
            except Exception:  # noqa: BLE001
                os._exit(1)
        self._child_pid = pid
        self._master_fd = master_fd
        # 关闭 echo：避免输入（含标记）被回显，干扰标记检测
        try:
            attrs = termios.tcgetattr(master_fd)
            attrs[3] &= ~termios.ECHO
            termios.tcsetattr(master_fd, termios.TCSANOW, attrs)
        except (termios.error, OSError):
            pass
        self._dead = False
        self._positioned = False
        self._loop.add_reader(master_fd, self._on_readable)
        await self._warmup()

    def _on_readable(self) -> None:
        try:
            data = os.read(self._master_fd, 4096)
        except OSError:
            data = b""
        if not data:
            # 终端关闭（子进程退出）
            self._dead = True
            self._data_event.set()
            return
        self._buf.extend(data)
        self._data_event.set()

    async def _warmup(self) -> None:
        """吃掉 bash 启动时的系统通知/提示，避免污染后续输出。"""
        if self._master_fd is None:
            return
        marker = f"__WC_{uuid.uuid4().hex[:8]}__"
        try:
            os.write(self._master_fd, f"printf '\\n{marker}\\n'\n".encode())
        except OSError:
            return
        deadline = self._loop.time() + 2.0
        while f"{marker}".encode() not in self._buf:
            if self._dead:
                return
            remaining = deadline - self._loop.time()
            if remaining <= 0:
                break
            self._data_event.clear()
            try:
                await asyncio.wait_for(self._data_event.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                break
        idx = self._buf.find(f"{marker}".encode())
        if idx >= 0:
            del self._buf[: idx + len(marker)]
        else:
            self._buf.clear()

    async def run(
        self,
        command: str,
        cwd: str | None = None,
        timeout: float = 30.0,
        input: str | None = None,
    ) -> tuple[bool, str]:
        await self.start()
        if self._master_fd is None or self._dead:
            return (False, "终端会话不可用")

        async with self._lock:
            # 首次运行时切到指定目录（参数 cwd 优先，其次初始 cwd）；之后保持 shell 自身状态
            target_cwd = cwd or self.initial_cwd
            prefix = ""
            if target_cwd and not self._positioned:
                prefix = f"cd {shlex.quote(target_cwd)}\n"
                self._positioned = True

            marker = f"__WC_{uuid.uuid4().hex[:8]}__"
            lines = [prefix + command]
            if input is not None:
                lines.append(input)  # 交互输入：在标记之前写入
            lines.append(f"printf \"\\n{marker}:$?\\n\"")
            try:
                os.write(self._master_fd, ("\n".join(lines) + "\n").encode())
            except OSError:
                return (False, "终端会话已退出")

            needle = f"{marker}:".encode()
            deadline = self._loop.time() + timeout
            while needle not in self._buf:
                if self._dead:
                    rc = self._shell_exit_code()
                    return (rc == 0, f"(终端退出，退出码 {rc})")
                remaining = deadline - self._loop.time()
                if remaining <= 0:
                    await self.reset()
                    return (False, f"命令执行超时（>{timeout:.0f}s）：{command[:120]}")
                self._data_event.clear()
                try:
                    await asyncio.wait_for(self._data_event.wait(), timeout=remaining)
                except asyncio.TimeoutError:
                    await self.reset()
                    return (False, f"命令执行超时（>{timeout:.0f}s）：{command[:120]}")

            # 提取输出（到标记为止）
            idx = self._buf.find(needle)
            out_bytes = bytes(self._buf[:idx])
            del self._buf[: idx + len(needle)]
            # 读退出码（标记后的下一行）
            nl = self._buf.find(b"\n")
            if nl >= 0:
                rc_line = bytes(self._buf[:nl]).decode(errors="replace").strip()
                del self._buf[: nl + 1]
                rc = int(rc_line) if rc_line.isdigit() else 0
            else:
                rc = 0

            output = out_bytes.decode(errors="replace")
            output = output.replace("\r\n", "\n").replace("\r", "\n")
            output = _strip_ansi(output).strip()
            if not output:
                output = f"(无输出，退出码 {rc})"
            if len(output) > MAX_OUTPUT_CHARS:
                output = output[:MAX_OUTPUT_CHARS] + "\n...(输出过长，已截断)"
            return (rc == 0, output)

    def _shell_exit_code(self) -> int:
        """命令退出 shell 后，取 shell 进程的退出码（如 `exit 3`）。"""
        if not self._child_pid:
            return 0
        try:
            pid, status = os.waitpid(self._child_pid, os.WNOHANG)
            if pid == 0:
                return 0
        except ChildProcessError:
            return 0
        if os.WIFEXITED(status):
            return os.WEXITSTATUS(status)
        if os.WIFSIGNALED(status):
            return 128 + os.WTERMSIG(status)
        return 0

    async def reset(self) -> None:
        """杀掉并重启 shell（清空状态）。"""
        await self.close()

    async def close(self) -> None:
        if self._master_fd is not None:
            if self._loop is not None:
                try:
                    self._loop.remove_reader(self._master_fd)
                except Exception:  # noqa: BLE001
                    pass
            try:
                os.close(self._master_fd)
            except OSError:
                pass
            self._master_fd = None
        if self._child_pid:
            try:
                os.killpg(os.getpgid(self._child_pid), signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
            try:
                os.waitpid(self._child_pid, os.WNOHANG)
            except (ChildProcessError, OSError):
                pass
            self._child_pid = None
            self._dead = False
