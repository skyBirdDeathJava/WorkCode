"""服务配置：从环境变量 / 本地 .env 读取，pydantic 校验。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv  # type: ignore

# 允许从 agent-service/.env 读取本地配置（该文件已被 .gitignore 忽略）
load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def _env_bool(key: str, default: bool = False) -> bool:
    return os.environ.get(key, "1" if default else "0").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


@dataclass
class Settings:
    host: str = field(default_factory=lambda: os.environ.get("WORKCODE_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(os.environ.get("WORKCODE_PORT", "4877")))
    model: str = field(
        default_factory=lambda: os.environ.get("WORKCODE_MODEL", "deepseek/deepseek-flash")
    )
    effort: str = field(
        default_factory=lambda: os.environ.get("WORKCODE_EFFORT", "low")
    )
    api_key: str = field(default_factory=lambda: os.environ.get("DEEPSEEK_API_KEY", ""))
    mock: bool = field(
        default_factory=lambda: _env_bool("WORKCODE_MOCK") or os.environ.get("WORKCODE_MOCK") == "mock"
    )
    session_dir: str = field(
        default_factory=lambda: os.environ.get(
            "WORKCODE_SESSION_DIR",
            str(Path.home() / ".workcode" / "sessions"),
        )
    )
    memory_file: str = field(
        default_factory=lambda: os.environ.get(
            "WORKCODE_MEMORY_FILE",
            str(Path.home() / ".workcode" / "memory.md"),
        )
    )
    auto_commit: bool = field(
        default_factory=lambda: _env_bool("WORKCODE_AUTO_COMMIT")
    )
    task_timeout_seconds: float = 60.0  # 子 Agent（task 工具）超时
    # 单条消息内模型连续工具调用的步数上限即单轮对话的 Agent 循环次数（实际无限，默认 10000；真死循环由重复调用检测兜底）
    max_steps: int = field(
        default_factory=lambda: int(os.environ.get("WORKCODE_MAX_STEPS", "10000"))
    )
    # 上下文预算（默认 128K）：子 Agent 探索复杂任务需要大上下文；超限仍由自动压缩兜底
    max_context_tokens: int = field(
        default_factory=lambda: int(os.environ.get("WORKCODE_MAX_CONTEXT_TOKENS", "128000"))
    )
    model_timeout_seconds: float = 120.0
    permission_timeout_seconds: float = 30.0
    diff_timeout_seconds: float = 180.0
    # 等待扩展在 VS Code 终端执行命令的结果；超时则回退到服务端 pty
    terminal_bridge_timeout: float = 30.0


def load_settings() -> Settings:
    return Settings()
