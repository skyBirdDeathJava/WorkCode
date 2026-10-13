# WorkCode（v0.1 MVP）

VS Code 插件形态的编程 Agent，对标 Claude Code。插件只做 UI 与编辑器适配，
Agent 逻辑跑在独立的本地 Python 服务里。

架构：`extension`（TypeScript 插件）— WebSocket — `agent-service`（Python/FastAPI）— DeepSeek API。
协议类型由 `shared/` 定义，两端镜像。详见 [docs/](docs/)。

## 目录

```
extension/         VS Code 插件（聊天面板、服务生命周期、编辑器上下文）
agent-service/     Python Agent 服务（FastAPI + LiteLLM + 只读工具 + 权限闸门）
shared/            通信协议（TS 类型；Python 侧用 pydantic 镜像）
docs/              需求文档、技术方案、实施计划
```

## 快速开始

前置：Node 20+ / pnpm / Python 3.10+ / VS Code。

```bash
# 1. 安装 TS 依赖
pnpm install

# 2. 构建插件（含 shared）
pnpm run build

# 3. 安装 Python 依赖（复用项目 .venv）
.venv/bin/pip install -r agent-service/requirements-dev.txt

# 4. 配置 API Key（任选其一）
#    a) VS Code 命令面板 → “WorkCode: 设置 API Key”（存入 SecretStorage，推荐）
#    b) 设置项 workcode.apiKey
#    c) 环境变量 DEEPSEEK_API_KEY / agent-service/.env

# 5. 在 extension/ 下按 F5 启动「扩展开发宿主」，侧边栏出现 WorkCode 图标
```

插件启动时会自动检测并拉起本地 Python 服务（优先用项目 `.venv`，
其次 `python3`；也可用设置项 `workcode.pythonPath` / `workcode.serviceDir` 覆盖）。

## 验证链路（无需 API Key）

**Mock 模式**：设置项 `workcode.mock` 勾选，或先跑服务时加 `WORKCODE_MOCK=1`。
Mock 模型会流式回显，且输入 `!tool read_file <路径>` 会触发一次真实文件读取，用于验证工具路径。

```bash
.venv/bin/python agent-service/scripts/smoke_test.py     # 全链路冒烟（Mock）
.venv/bin/pytest agent-service/tests                     # 工具/权限闸门单测
```

**真实模型冒烟**（需 Key）：

```bash
WORKCODE_MOCK=0 DEEPSEEK_API_KEY=sk-... .venv/bin/python agent-service/scripts/smoke_test.py
```

## 手动起服务（调试）

```bash
cd agent-service
../.venv/bin/python -m workcode        # 默认 127.0.0.1:4877
curl http://127.0.0.1:4877/healthz     # 健康检查
```

## 配置项

| 设置 | 默认 | 说明 |
|------|------|------|
| `workcode.apiKey` | `""` | DeepSeek Key（明文，建议用命令存入 SecretStorage） |
| `workcode.model` | `deepseek/deepseek-chat` | 模型标识（LiteLLM 格式） |
| `workcode.servicePort` | `4877` | 本地服务端口 |
| `workcode.pythonPath` | `""` | 留空自动检测 |
| `workcode.serviceDir` | `""` | agent-service 目录，留空按插件路径推导 |
| `workcode.mock` | `false` | Mock 模式，免 Key 验证链路 |

## 里程碑状态

- [x] M1 能对话（Mock 全链路已通）
- [x] M2 能读代码（当前文件/选区上下文注入 + 只读工具）
- [x] M3 能改代码（write_file / edit_file，真实 DeepSeek 已验证改文件）
- [x] M4 Diff 预览 + 确认应用（写操作先暂存，Diff 视图确认后才落盘，接受/拒绝均已验证）
- [x] M5 运行命令 + 危险命令拦截（run_command：危险 deny / 安全白名单 allow / 其他弹窗确认）
- [ ] M6 MVP 完成（真实任务调试 + 演示）

会话持久化（对齐 Claude Code 的 transcript 机制）：
- 每个会话一个 **JSONL 文件**（`~/.workcode/sessions/<id>.jsonl`），逐条追加，崩溃/中断只丢当前未写完的消息
- **恢复历史会话**：命令面板 →「WorkCode: 恢复历史会话」，选择器列出各会话（首条消息 + 时间），选中即恢复，历史消息在聊天面板重新渲染
- **自动压缩上下文**：历史超限时用模型把较早轮次压缩成摘要，摘要作为 `summary` 行落盘；长会话不失忆

安全与交互：
- 只读工具默认 `allow`；路径在工作区外直接 `deny`。
- 写文件（`write_file` / `edit_file`）：Agent 提议修改后**不直接落盘**，先在 VS Code
  里以 Diff 对比视图展示，通知栏「接受修改 / 拒绝」，接受后才写盘，拒绝则告诉 Agent。
- `run_command` 三层决策：危险命令（`rm -rf /`、`sudo`、`mkfs`、`git push --force` 等）
  直接 `deny`；安全只读/构建/测试命令（`ls`、`cat`、`go build`、`npm test` 等）自动
  `allow`；其他弹「允许 / 拒绝」确认。工具内还有第二道黑名单校验兜底。
- **在 VS Code 集成终端里执行**（M5 扩展）：插件连接时声明 `terminalSupport`，
  `run_command` 会在 VS Code 的**集成终端**里执行（自动创建/复用「WorkCode」终端，
  输出实时可见）。长驻进程（`npm run dev`）不会超时报错——超时后报告「仍在运行」，
  终端继续留给用户。
- **pty 回退**：带 `input` 的交互命令、或服务端 standalone 运行时，走服务端 pty
  终端（`cwd`/环境变量跨命令保持、`isatty()` 为真、交互式程序可读 stdin）。
  输出剥离 ANSI 转义序列，`TERM=dumb` 关闭 readline 重绘噪音。

## 测试

```bash
# 工具/权限/循环单测（10 个）
cd agent-service && ../.venv/bin/python -m pytest tests/ -q

# 全链路冒烟（Mock，免 Key；含权限弹窗流程）
../.venv/bin/python scripts/smoke_test.py

# 真实模型验证（M3 改文件 / M5 跑命令，自动允许权限）
../.venv/bin/python scripts/test_m3_real.py
../.venv/bin/python scripts/test_m5_real.py
```
