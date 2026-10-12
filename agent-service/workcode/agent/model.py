"""模型路由：统一接口 + 两个实现（DeepSeek / Mock）。

DeepSeekModel 用 LiteLLM 的 acompletion 流式接口，兼容 OpenAI 风格
function calling 的增量解析（tool_calls delta 按 index 累积）。
MockModel 用于无 API Key 时验证闭环。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

OnDelta = Callable[[str], Awaitable[None]]


@dataclass
class StepResult:
    text: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)


class ModelProvider:
    name: str = "abstract"

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        on_delta: OnDelta,
    ) -> StepResult:
        raise NotImplementedError


class MockModel(ModelProvider):
    """不回真模型。发 !tool read_file <path> 会触发一次工具调用。"""

    name = "mock"

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        on_delta: OnDelta,
    ) -> StepResult:
        last = messages[-1] if messages else {"role": "user", "content": ""}

        if last.get("role") == "tool":
            # 工具执行完后，回复一句确认（测试工具路径用）
            text = f"[Mock] 工具结果已收到（{len(last.get('content', ''))} 字符）。"
            await _emit_deltas(text, on_delta)
            return StepResult(text=text)

        content = last.get("content", "")
        if content.startswith("!tool read_file"):
            # 只取指令后的第一个 token 作为路径
            rest = content[len("!tool read_file") :].strip()
            target = rest.split()[0] if rest else "."
            return StepResult(
                tool_calls=[
                    {"id": "mock-read-1", "name": "read_file", "arguments": {"path": target}}
                ]
            )
        if content.startswith("!tool write_file"):
            # 写文件（走 Diff 确认路径）
            rest = content[len("!tool write_file") :].strip()
            target = rest.split()[0] if rest else "."
            return StepResult(
                tool_calls=[
                    {
                        "id": "mock-write-1",
                        "name": "write_file",
                        "arguments": {"path": target, "content": "hello from WorkCode mock\n"},
                    }
                ]
            )
        if content.startswith("!tool run_command"):
            # 执行命令（非白名单命令走权限弹窗）；只取指令后第一行作为命令
            rest = content[len("!tool run_command") :].strip()
            command = rest.splitlines()[0] if rest else "echo hi"
            return StepResult(
                tool_calls=[
                    {"id": "mock-cmd-1", "name": "run_command", "arguments": {"command": command}}
                ]
            )

        text = f"[Mock 回复] 收到：{content[:120]}。当前处于 Mock 模式，未调用真实模型。"
        await _emit_deltas(text, on_delta)
        return StepResult(text=text)


async def _emit_deltas(text: str, on_delta: OnDelta) -> None:
    for i in range(0, len(text), 5):
        await on_delta(text[i : i + 5])
        await asyncio.sleep(0.005)


class DeepSeekModel(ModelProvider):
    name = "deepseek"

    def __init__(self, api_key: str, model: str = "deepseek/deepseek-chat") -> None:
        self.api_key = api_key
        self.model = model

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        on_delta: OnDelta,
    ) -> StepResult:
        import litellm  # 延迟导入：未装 litellm 时服务仍可启动（Mock 模式可用）

        if not self.api_key:
            raise RuntimeError("未配置 DeepSeek API Key（环境变量 DEEPSEEK_API_KEY 或握手时传入）")

        step = StepResult()
        tool_acc: dict[int, dict[str, str]] = {}

        response = await litellm.acompletion(
            model=self.model,
            messages=messages,
            tools=tools or None,
            stream=True,
            api_key=self.api_key,
            temperature=0.2,
        )

        async for chunk in response:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta is None:
                continue
            if getattr(delta, "content", None):
                step.text += delta.content
                await on_delta(delta.content)
            tool_calls = getattr(delta, "tool_calls", None) or []
            for tc in tool_calls:
                idx = int(getattr(tc, "index", len(tool_acc)))
                acc = tool_acc.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                if getattr(tc, "id", None):
                    acc["id"] = tc.id
                fn = getattr(tc, "function", None)
                if fn:
                    if getattr(fn, "name", None):
                        acc["name"] = fn.name
                    if getattr(fn, "arguments", None):
                        acc["arguments"] += fn.arguments

        for idx in sorted(tool_acc):
            acc = tool_acc[idx]
            if not acc["name"]:
                continue
            step.tool_calls.append(
                {
                    "id": acc["id"] or f"call_{idx}",
                    "name": acc["name"],
                    "arguments": acc["arguments"],
                }
            )

        return step
