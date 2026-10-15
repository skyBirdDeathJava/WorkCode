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
    reasoning: str = ""  # 思考模式下的推理轨迹（reasoning_content）


class ModelProvider:
    name: str = "abstract"

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        on_delta: OnDelta,
        on_reasoning: OnDelta | None = None,
    ) -> StepResult:
        raise NotImplementedError

    async def summarize(self, text: str) -> str:
        """把一段对话历史压缩成简洁摘要。"""
        raise NotImplementedError


class MockModel(ModelProvider):
    """不回真模型。发 !tool read_file <path> 会触发一次工具调用。"""

    name = "mock"

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        on_delta: OnDelta,
        on_reasoning: OnDelta | None = None,
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

    async def summarize(self, text: str) -> str:
        return f"[Mock 摘要] 此前讨论了 {len(text)} 字符的内容。"


async def _emit_deltas(text: str, on_delta: OnDelta) -> None:
    for i in range(0, len(text), 5):
        await on_delta(text[i : i + 5])
        await asyncio.sleep(0.005)


class DeepSeekModel(ModelProvider):
    """基于 OpenAI SDK（DeepSeek 官方推荐，API 完全兼容）调用 deepseek-flash / deepseek-v4-pro。

    参数按 DeepSeek Chat Completions 文档：
    - model: 去掉 LiteLLM 风格前缀后传给 API（deepseek-flash / deepseek-v4-pro）
    - reasoning_effort: 顶层字段，none 关思考，low/high/max 开思考（默认 high）
    - 思考模式下响应带 reasoning_content（推理过程），此处不展示
    """

    name = "deepseek"
    BASE_URL = "https://api.deepseek.com"

    def __init__(self, api_key: str, model: str = "deepseek/deepseek-flash", effort: str = "low") -> None:
        self.api_key = api_key
        self.model = model
        self.effort = effort
        self._client = None  # 延迟创建，未装 openai 时服务仍能启动

    def _api_model(self) -> str:
        return self.model.split("/")[-1] or self.model

    def _client_sync(self):
        if self._client is None:
            from openai import AsyncOpenAI  # 延迟导入

            self._client = AsyncOpenAI(api_key=self.api_key, base_url=self.BASE_URL)
        return self._client

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        on_delta: OnDelta, #推送delta的回调函数
        on_reasoning: OnDelta | None = None,
    ) -> StepResult:
        if not self.api_key:
            raise RuntimeError("未配置 DeepSeek API Key（环境变量 DEEPSEEK_API_KEY 或握手时传入）")

        step = StepResult()
        tool_acc: dict[int, dict[str, str]] = {}

        # 按 DeepSeek 官方示例：reasoning_effort 作为顶层参数，thinking 用 extra_body 显式开关
        thinking_on = bool(self.effort) and self.effort != "none"
        kwargs: dict[str, Any] = {
            "model": self._api_model(),
            "messages": messages,
            "tools": tools or None,
            "stream": True,
            "temperature": 0.2,
        }
        if thinking_on:
            kwargs["reasoning_effort"] = self.effort  # none/low/high/max（none 即关思考，故只在开启时传）
        kwargs["extra_body"] = {"thinking": {"type": "enabled" if thinking_on else "disabled"}}

        response = await self._client_sync().chat.completions.create(**kwargs)

        async for chunk in response:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta is None:
                continue
            if getattr(delta, "reasoning_content", None): #getattr(obj, name, default) = 安全地取对象的某个属性——取得到就返回，取不到就返回默认值（而不是报错）。
                # 思考模式的推理轨迹（在最终答案之前）大模型的思考内容
                step.reasoning += delta.reasoning_content
                if on_reasoning: #如果传了输出推理内容的回调函数，就调用它
                    await on_reasoning(delta.reasoning_content)
            if getattr(delta, "content", None): #大模型的回答内容
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

    async def summarize(self, text: str) -> str:
        if not self.api_key:
            raise RuntimeError("未配置 DeepSeek API Key")
        response = await self._client_sync().chat.completions.create(
            model=self._api_model(),
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你是 WorkCode。请把下面的对话历史压缩成一段简洁的中文摘要，"
                        "保留关键事实、文件路径、结论和待办。只输出摘要本身。"
                    ),
                },
                {"role": "user", "content": text},
            ],
            temperature=0.2,
            max_tokens=500,
        )
        content = response.choices[0].message.content or ""
        return content.strip()
