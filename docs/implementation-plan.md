# WorkCode 插件版 — 实施计划

版本：v0.1（MVP）

## 1. 两周排期

| 天 | 任务 | 对应能力 |
|----|------|----------|
| 1-2 | 插件脚手架 + 侧边栏 + Python 服务（FastAPI）+ WebSocket | 1、3 |
| 3-4 | 接模型（LiteLLM），流式对话，读当前文件/选区 | 2、3、4、5 |
| 5-7 | 工具集（读/写/搜索/编辑） | 5 |
| 8-10 | Diff 展示 + 确认应用 | 6、7 |
| 11-12 | 运行命令 + 权限闸门 | 8 |
| 13-14 | 真实任务调试 | 全部 |
| 15 | 文档 + 演示 | — |

## 2. 里程碑

| 里程碑 | 时间 | 验收标准 |
|--------|------|----------|
| M1：能对话 | 第 2 天 | 侧边栏提问，Agent 回复 |
| M2：能读代码 | 第 4 天 | Agent 能解释选中代码 |
| M3：能改代码 | 第 7 天 | Agent 生成修改并通过工具调用写入 |
| M4：能预览确认 | 第 10 天 | Diff 展示 + 用户确认应用 |
| M5：能跑验证 | 第 12 天 | Agent 跑测试并把结果反馈 |
| M6：MVP 完成 | 第 15 天 | 完整闭环 + 演示材料 |

## 3. 技术栈清单

| 组件 | 选型 |
|------|------|
| 插件语言 | TypeScript |
| 插件框架 | VS Code Extension API |
| 插件 UI | Webview |
| 本地服务 | **Python**（FastAPI + uvicorn） |
| 通信 | WebSocket |
| 模型调用 | LiteLLM（DeepSeek 或 Claude 二选一） |
| 类型校验 | TypeScript interface / pydantic（由 shared JSON Schema 生成） |
| 协议定义 | JSON Schema（`shared/`） |
| 包管理 | pnpm workspace（TS 侧）+ uv（Python 侧） |
| 构建 | esbuild 或 tsc（TS 侧） |
| 测试 | vitest（TS 侧）+ pytest / pytest-asyncio（Python 侧） |

## 4. 风险与对策

| 风险 | 对策 |
|------|------|
| 模型返回的工具调用格式不稳定 | JSON Schema 严格约束 + 解析容错 |
| 跨语言协议对不上（TS 与 Python） | 协议唯一事实来源放 `shared/`，两端自动生成类型，运行时校验 |
| Agent 跑偏、反复调用同一工具 | 循环次数上限，超限提示用户 |
| 上下文超 token 限制 | 按需注入 + 历史压缩 |
| Diff 应用冲突 | 应用前校验文件哈希 |
| 危险命令绕过黑名单 | 黑名单 + 白名单 + 用户确认三层 |
| 本地服务启动失败（无 Python / 依赖缺失） | 启动时检测 Python 版本、健康检查、错误提示 |

## 附录：和 Claude Code 的差距

| 维度 | Claude Code | WorkCode MVP |
|------|-------------|--------------|
| 工具集 | 完整（含 MCP） | 6 个基础工具 |
| 权限模式 | Manual/Auto/Plan | allow/deny/ask |
| 上下文管理 | 成熟压缩和检索 | 基础按需注入 |
| 模型 | Claude 专属 | 单模型（LiteLLM） |
| 生态 | MCP、Hooks、Skills | 无 |
| 形态 | CLI + 插件 + Web | 仅插件 |

MVP 目标不是追平 CC，而是跑通核心闭环：**对话 → 读代码 → 改代码 → Diff → 确认 → 应用 → 验证**。
