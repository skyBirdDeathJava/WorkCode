"""工具注册表：统一工具定义、模型 schema 与执行入口。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Callable, Coroutine

Handler = Callable[..., Coroutine[Any, Any, tuple[bool, str]] | tuple[bool, str]]


@dataclass
class ToolDef:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Handler


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolDef] = {}

    def register(self, tool: ToolDef) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> ToolDef | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools.keys())

    def schema(self) -> list[dict[str, Any]]:
        """给模型的 tools 参数（OpenAI 风格 function calling）。"""
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
            }
            for t in self._tools.values()
        ]

    async def execute(self, name: str, args: dict[str, Any]) -> tuple[bool, str]:
        tool = self._tools.get(name)
        if not tool:
            return (False, f"未知工具: {name}")
        try:
            result = tool.handler(**args)
            if asyncio.iscoroutine(result):
                return await result
            return result
        except TypeError as e:
            return (False, f"工具参数错误: {e}")
        except Exception as e:
            return (False, f"工具执行失败: {e}")
