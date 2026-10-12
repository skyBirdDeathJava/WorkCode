# WorkCode 插件版 — 技术方案（Python 版）

版本：v0.1（MVP）
本方案在「整体三层结构不变」的前提下，将本地 Agent 服务由 Node.js 改为 Python。

## 1. 整体架构

系统分为三层：

**第一层：VS Code 插件（TypeScript）**
负责 UI 和编辑器适配。包含聊天面板、命令注册、编辑器事件监听、Diff 展示、应用修改。

**第二层：本地 Agent 服务（Python）**
独立进程，负责真正的 Agent 逻辑。包含 Agent 循环、工具集、权限闸门、上下文管理、模型路由、会话持久化。

**第三层：远程模型 API**
OpenAI / Claude / DeepSeek 等，提供推理能力。

**通信方式**：
- 插件与服务之间：本地 WebSocket
- 服务与模型 API 之间：HTTPS

| 层 | 原方案 | Python 方案 |
|----|--------|-------------|
| 插件端 | TypeScript | TypeScript（不变） |
| 本地 Agent 服务 | Node.js | **Python** |
| 远程模型 API | HTTPS | HTTPS（不变） |

**架构原则**：
- 插件只做 UI 和 VS Code 适配
- Agent 逻辑全在本地服务
- 服务独立进程，便于未来迁移到 Fork 桌面版
- 插件和服务共享协议定义，避免跨语言不一致

## 2. 目录结构

采用 monorepo 管理，分三个包：

```
nexode/
├── extension/        # VS Code 插件（TypeScript）
│   ├── src/          # 激活、命令、Diff、编辑器适配、WebSocket 客户端
│   └── webview/      # 聊天面板 UI
├── agent-service/    # 本地 Agent 服务（Python）
│   ├── agent/        # Agent 循环、上下文管理、模型路由
│   ├── tools/        # 工具集
│   ├── gateway/      # 权限闸门、危险命令黑名单
│   ├── server/       # FastAPI 服务、WebSocket 端点
│   └── session/      # 会话持久化
├── shared/           # 跨语言协议定义（JSON Schema）
│   └── schema/       # 消息、工具、权限、Diff 的 JSON Schema
└── docs/             # 需求与技术方案
```

- 用 pnpm workspace 管理 TypeScript 侧；agent-service 用 Python（pyproject.toml + uv 或 pip）。
- `shared/` 是协议的唯一事实来源：用 JSON Schema 描述，两端分别生成类型（TypeScript 用 `json-schema-to-typescript`，Python 用 pydantic + `datamodel-code-generator`）。

## 3. 插件端设计（TypeScript，不变）

### 3.1 激活入口
插件激活时做四件事：检测并拉起本地服务、注册聊天视图、注册命令、监听编辑器事件。

### 3.2 聊天面板
用 `WebviewViewProvider` 实现侧边栏视图。Webview 负责渲染聊天 UI，通过消息机制与插件主进程双向通信。插件再把用户输入转发给本地服务，把服务返回的内容推回 UI。

### 3.3 编辑器集成
通过 VS Code API 获取当前文件、选中代码、文件路径、工作区根目录，并监听文件变化。

### 3.4 Diff 展示
用 `TextDocumentContentProvider` 提供虚拟文档（原始内容和修改后内容），再调用 VS Code 原生的 Diff 命令打开并排对比视图。

### 3.5 应用修改
用 `WorkspaceEdit` API 把修改写回真实文件并保存。应用前需校验文件是否被用户改过。

### 3.6 命令注册
提供打开聊天、解释选中代码、修改选中代码、新建会话等命令。

### 3.7 服务生命周期管理
启动服务时自动检测 / 拉起 Python 进程（详见 6.1），并做健康检查（HTTP 或 WebSocket ping），失败时向用户提示。

## 4. 本地 Agent 服务设计（Python）

### 4.1 技术选型

| 组件 | 推荐选型 |
|------|----------|
| WebSocket 服务 | FastAPI + uvicorn（推荐），或纯 `websockets` 库 |
| 模型调用 | LiteLLM（统一多模型接口），或直接用官方 SDK |
| 流式输出 | 各 SDK 的 `stream` 接口 + WebSocket 推送 |
| 文件操作 | `pathlib`、`os` |
| 命令执行 | `subprocess`、`asyncio.create_subprocess` |
| 配置管理 | pydantic + 环境变量 |
| 会话持久化 | JSON 文件，或 SQLite |
| 类型校验 | pydantic |
| 测试 | pytest + pytest-asyncio |

### 4.2 Agent 循环
核心是一个持续循环：组装上下文 → 调用模型（流式）→ 解析输出 → 如果是工具调用则走权限闸门并执行 → 把结果加入上下文 → 继续循环；如果是文本回复则结束。

用 `asyncio` 实现流式与并发的 Agent 循环，每次工具调用都有循环次数上限，超限提示用户。

### 4.3 工具集
MVP 阶段提供 6 个工具：

| 工具 | 作用 | 默认权限 |
|------|------|----------|
| `read_file` | 读文件 | allow |
| `list_directory` | 列目录 | allow |
| `search_files` | 按文件名/内容搜索 | allow |
| `write_file` | 写文件 | ask |
| `edit_file` | 精确替换 | ask |
| `run_command` | 执行命令 | ask（危险命令 deny） |

每个工具用 JSON Schema 定义参数，让模型知道怎么调用。工具实现用 `dataclass` / pydantic 模型统一签名，注册表实现「工具可插拔」。

### 4.4 权限闸门
对每次工具调用做三档决策：allow / deny / ask。

- 只读工具默认 allow
- 写操作默认 ask，路径必须在工作区内
- 危险命令（`rm -rf /`、`mkfs`、`sudo`、`git push --force` 等）直接 deny
- 安全的只读命令（`ls`、`cat`、`go build`、`npm test` 等）自动 allow
- 其他走 ask，弹窗让用户确认

权限决策异步化：`ask` 时通过 WebSocket 把权限请求发给插件，插件弹确认框，用户响应再回到 Agent 循环继续执行。

### 4.5 上下文管理
维护对话历史，按需注入文件内容，避免一次塞太多。用 token 计数控制上下文窗口，超限时压缩历史（MVP 阶段做基础压缩：丢弃最早的非关键消息 / 摘要）。

### 4.6 模型路由
统一接口（LiteLLM 天然支持），MVP 阶段只接一个模型（推荐 DeepSeek 或 Claude）。支持流式响应、失败重试、超时处理。API Key 由插件端通过 VS Code SecretStorage 读取并传入服务进程环境变量，服务侧不落盘。

### 4.7 会话持久化
会话存本地文件，每个会话一个 JSON，含消息历史、上下文快照、创建时间。MVP 阶段不做云同步。文件规模变大后可迁移到 SQLite。

## 5. 通信协议（跨语言）

插件（TypeScript）与服务（Python）之间用 WebSocket，**两边对消息格式的理解必须完全一致**。

**消息分类**：

插件 → 服务：
- 聊天请求（含消息和编辑器上下文）
- 权限响应（允许 / 拒绝）
- 取消请求

服务 → 插件：
- 文本增量
- 工具调用
- 工具结果
- 权限请求
- Diff 内容
- 错误
- 完成信号

**编辑器上下文**包含：工作区根目录、当前文件（路径和内容）、选中代码、已打开文件列表。

**协议固化方式**：`shared/` 中定义 JSON Schema，两端各自生成强类型（TypeScript interface / pydantic model），运行时按 schema 校验。避免手写对不上。

## 6. 打包与启动（Python 服务）

用户装插件时，不该让他手动装 Python 环境。两种方案：

| 方案 | 做法 | 优缺点 |
|------|------|--------|
| A. 打包单文件 | 用 PyInstaller 把服务打成单文件可执行，插件直接调用 | 用户零依赖；每次改服务要重新打包，体积大 |
| B. 要求预装 Python | 插件检测本机 Python，缺则提示；满足则拉起服务 | MVP 开发快；但要求用户有 Python |

**MVP 阶段先用方案 B**：插件启动时检测 `python3`，验证版本（≥ 3.10），`uv` 或 pip 安装依赖后以子进程拉起 uvicorn；后期切换方案 A。

## 7. 安全设计

| 层次 | 措施 |
|------|------|
| 权限闸门 | allow / deny / ask 三档 |
| 危险命令 | 正则黑名单 + 硬性拒绝 |
| 路径校验 | 写操作必须在工作区内 |
| 用户确认 | 写文件、跑命令前弹窗 |
| 审计日志 | 所有工具调用记录到本地日志 |
| API Key | 存 VS Code SecretStorage，不明文，经环境变量传给服务进程 |

## 8. 会话持久化

存储在插件全局存储目录下，每个会话一个文件。内容包含消息历史、上下文快照、创建时间。MVP 阶段只做本地存储。
