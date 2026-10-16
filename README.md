# WorkCode（v0.1 MVP）

VS Code 插件形态的编程 Agent，对标 Claude Code。插件只做 UI 与编辑器适配，
Agent 逻辑跑在独立的本地 Python 服务里。

架构：`extension`（TypeScript 插件）— WebSocket — `agent-service`（Python/FastAPI）— 模型 API（DeepSeek）。
协议类型由 `shared/` 定义，两端镜像。

> **📘 完整使用说明见 [使用说明文档.md](使用说明文档.md)**（界面、各功能操作、配置、FAQ）。

## 目录

```
extension/         VS Code 插件（聊天面板、终端桥、Diff、服务生命周期）
agent-service/     Python Agent 服务（FastAPI + LiteLLM + 工具 + 权限闸门 + 会话）
shared/            跨语言通信协议（TS / pydantic 镜像）
docs/              需求文档、技术方案、实施计划
```

## 功能特性

| 能力 | 说明 |
|------|------|
| AI 对话 | 结构化 Markdown 输出，上下文栏显示当前文件 + 选中行号 |
| 读代码 | read_file / list_directory / search_files，解释选中代码 |
| 改代码 | write_file / edit_file / **apply_patch（多文件批量）**，Diff 预览 → 确认 → 落盘，可选 git 自动 commit |
| 跑命令 | run_command 在 **VS Code 集成终端**执行，长驻进程（npm run dev）不超时 |
| Web | web_search（Bing→DDG→Wikipedia 回退）+ web_fetch（SSRF 防护） |
| 权限模式 | **Manual / Auto / Plan**，底部工具栏切换；危险命令硬拦 |
| 模型切换 | 底部选模型（LiteLLM 标识），每轮显示 token 用量 |
| @ 引用文件 | 输入 `@` 弹出文件列表，选中注入文件内容 |
| 项目记忆 | 自动读取工作区 `WorkCode.md` / `CLAUDE.md` 注入上下文 |
| git 感知 | 每轮注入分支、工作区变更、最近提交 |
| 历史会话 | JSONL 持久化 + 🕘 图标恢复 + 上下文自动压缩摘要 |
| 实时上下文 | 编辑器事件驱动，当前文件/选区实时推给面板 |

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

插件启动时自动检测并拉起本地 Python 服务（优先用项目 `.venv`，
其次 `python3`；可用 `workcode.pythonPath` / `workcode.serviceDir` 覆盖）。

## 验证链路（无需 API Key）

**Mock 模式**：设置项 `workcode.mock` 勾选，或服务加 `WORKCODE_MOCK=1`。

```bash
.venv/bin/python agent-service/scripts/smoke_test.py   # 全链路冒烟（Mock）
cd agent-service && ../.venv/bin/python -m pytest tests/ -q   # 单测
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
| `workcode.apiKey` | `""` | 模型 Key（建议用命令存入 SecretStorage） |
| `workcode.model` | `deepseek/deepseek-chat` | 模型标识（LiteLLM 格式），面板可切换 |
| `workcode.servicePort` | `4877` | 本地服务端口 |
| `workcode.pythonPath` | `""` | 留空自动检测 |
| `workcode.serviceDir` | `""` | agent-service 目录，留空按插件路径推导 |
| `workcode.mock` | `false` | Mock 模式，免 Key 验证链路 |

## 环境变量（Python 服务）

| 变量 | 默认 | 说明 |
|------|------|------|
| `DEEPSEEK_API_KEY` | `""` | 模型 API Key |
| `WORKCODE_PORT` | `4877` | 服务端口 |
| `WORKCODE_MODEL` | `deepseek/deepseek-flash` | 默认模型（flash / v4-pro） |
| `WORKCODE_EFFORT` | `low` | 思考程度（none/low/high/max，none=关思考） |
| `WORKCODE_MOCK` | `0` | Mock 模式 |
| `WORKCODE_MAX_STEPS` | `10000` | 单轮工具调用步数上限（实际无限；真死循环由重复调用检测兜底） |
| `WORKCODE_MAX_CONTEXT_TOKENS` | `16000` | 上下文预算，超限自动压缩 |
| `WORKCODE_SESSION_DIR` | `~/.workcode/sessions` | 会话存储目录 |
| `WORKCODE_MEMORY_FILE` | `~/.workcode/memory.md` | 跨项目用户记忆文件 |
| `WORKCODE_AUTO_COMMIT` | `0` | 编辑落盘后自动 `git add`+`commit`（只提交被改文件） |

## 里程碑状态

- [x] M1 能对话
- [x] M2 能读代码（当前文件/选区上下文注入 + 只读工具）
- [x] M3 能改代码（write_file / edit_file，真实模型验证改文件）
- [x] M4 Diff 预览 + 确认应用（暂存 → Diff 视图 → 接受/拒绝 → 落盘）
- [x] M5 运行命令（VS Code 终端桥 + 危险命令拦截 + pty 回退）
- [x] 会话持久化（JSONL + 历史恢复 + 自动压缩）
- [x] 记忆与引用（WorkCode.md + @ 文件引用）
- [x] 权限模式（Manual/Auto/Plan）+ 模型切换 + 用量显示
- [x] Web 工具 + git 上下文感知

## 安全与交互

- 只读工具（读/列/搜/网页搜索）默认 `allow`；路径在工作区外 `deny`。
- 写文件：**Diff 预览 → 面板确认 → 落盘**，接受才写。
- 命令：危险命令（`rm -rf /`、`sudo`、`mkfs`、`git push --force` 等）硬拦；
  安全命令（`ls`、`cat`、`go build`、`npm test` 等）免确认；其他按权限模式确认。
- 网页抓取：需确认，拒绝内网/回环（SSRF 防护）。
- API Key 存 VS Code SecretStorage，`.env` 已 gitignore。

## 测试

```bash
# 单测（40 个：工具/权限/循环/会话/压缩/Web/git）
cd agent-service && ../.venv/bin/python -m pytest tests/ -q

# 全链路冒烟（Mock，免 Key）
../.venv/bin/python scripts/smoke_test.py

# 真实模型验证（改文件 / 跑命令，自动允许权限）
../.venv/bin/python scripts/test_m3_real.py
../.venv/bin/python scripts/test_m5_real.py
```
