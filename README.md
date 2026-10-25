# VeriForge
> 测试驱动的代码自维护智能体 —— 可追踪、可审查、可归因

团队代码库智能维护平台。当前版本支持本地仓库配置、代码检索、方案与 Patch 人工确认、Git Worktree 隔离应用、白名单测试、SQLite 持久化、SSE 实时事件和历史回放；并支持把「需求分析 → 代码检索 → 方案生成 → Patch 生成 → 测试审查」这条链路沉淀为**可复用任务模板**，以及通过 **MCP** 把代码检索与白名单测试执行暴露给 Agent 自主调用。

## 本地环境准备

项目不依赖系统全局的 `node` / `python`。如果这两个命令不在 PATH 里，引导脚本会自动回落到 WorkBuddy 的托管运行时目录。

```bash
# Git Bash
./scripts/dev.sh setup     # 安装前后端依赖
./scripts/dev.sh verify    # 类型检查 + 构建 + 前端测试 + 后端测试

# PowerShell
.\scripts\dev.ps1 setup
.\scripts\dev.ps1 verify
```

手动方式（假设 `node` / `python` 已可用）：

后端：

```bash
cd backend
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt -r requirements-dev.txt
.venv/Scripts/python.exe -m pytest -q
```

前端（使用 pnpm，不要用 npm）：

```bash
cd frontend
pnpm install
pnpm build
pnpm test
pnpm dev
```

> 注意：在受管沙箱里执行时不要运行 `pip install --upgrade pip`，pip 自升级会因无法删除旧 exe 而失败并损坏虚拟环境。

后端测试请从 `backend` 目录运行。项目根目录也提供 `pytest.ini`，从根目录执行 `backend\.venv\Scripts\python.exe -m pytest -q` 时同样使用项目内受忽略的临时目录。

## 启动

后端：`cd backend && .venv/Scripts/python.exe -m uvicorn main:api --reload`

前端：`cd frontend && pnpm dev`

MCP 工具面（stdio）：`cd backend && .venv/Scripts/python.exe -m mcp_server`

## 核心 API

- `POST /api/repositories` 配置本地 Git 仓库
- `POST /api/tasks` 创建代码维护任务（可带可选 `template_id` 复用任务模板）
- `GET /api/tasks/{id}` 查询任务状态
- `GET /api/tasks/{id}/events` 回放历史事件
- `GET /api/tasks/{id}/stream` 订阅 SSE 事件
- `POST /api/templates` 新建任务模板；`GET /api/templates` 列出模板
- `POST /api/templates/from-task/{task_id}` 把一次已跑通的任务沉淀成模板

`POST /api/tasks/{id}/patches/{patch_id}/apply` 在隔离 Worktree 应用 Patch；`POST /api/tasks/{id}/patches/{patch_id}/tests` 执行白名单测试；`GET /api/tasks/{id}/test-runs` 回放结果。所有写入操作需人工确认，测试命令必须完整匹配配置且最长 5 分钟。

安全边界：原始仓库不会被 Patch 直接写入；仅允许配置源码目录和白名单命令；不提供任意 Shell、依赖自动安装或完整容器沙箱。

## 任务模板

把五步链路的可调参数沉淀成模板存进 SQLite，同类 Issue 下次直接调用，不必重新设计。模板由两部分组成：**参数包**（20 项，按五个阶段分组）与**锚点包**（常命中的文件、符号、验收标准）——锚点让第二次检索直接命中，这才是「不重复设计」的实感。

- `POST /api/templates` 建模板，`GET /api/templates` 列表，`GET /api/templates/{id}` 详情，`DELETE /api/templates/{id}` 删除
- `POST /api/templates/from-task/{task_id}?name=...` 从一次已跑通的任务沉淀模板（取任务上的参数快照）
- `POST /api/tasks` 带 `template_id`：创建时把与仓库边界收敛后的参数快照写进 `code_tasks.template_params_json`，历史任务因此可复现

**模板只能收窄边界，不能放宽**；越界一律 422，既不静默截断也不静默丢弃：

- `max_changed_lines ≤ 50`、`test_timeout_seconds ≤ 300`、`max_proposed_changes ≤ 5` 是硬上限
- 模板的 `test_command` 必须精确命中目标仓库白名单
- 检索范围永远取自仓库 `allowed_source_roots`，模板不带这个字段
- 不传 `template_id` 的任务，与引入模板之前逐字节一致（缺省即现有行为）

参数的实际生效值会落在事件里：`CODE_SEARCHED` 的 `strategies` 含一条 `template_applied`；`PATCH_GENERATION_STARTED` 的 payload 含一条 `template_patch`，记录 provider 分支（module / fake / llm）、model、重试次数与行数上限。

## MCP 工具面

`backend/mcp_server.py` 用 FastMCP 把「代码检索」与「白名单测试执行」封装成 Agent 可自主调用的能力单元，stdio 传输。接进 MCP 客户端时，用仓库内 `backend/.venv` 的解释器执行 `-m mcp_server`，工作目录指向 `backend/`（与 `uvicorn main:api` 同一个约束）。

- `list_repositories` 列出仓库 id 与它的安全边界
- `search_code` 在允许的源码目录内检索，返回带路径、符号、行号的片段
- `read_code` 按路径、行范围或符号读取源码片段
- `list_allowed_test_commands` 列出仓库白名单里的测试命令
- `run_allowed_test` 在任务的隔离 Worktree 内执行一条白名单命令

这一层不引入新的写入路径，也不把边界交给调用方：源码范围与可执行命令都来自仓库配置；`run_allowed_test` 只在 `.codeforge/worktrees/task-{id}` 内执行，Worktree 不存在（补丁还没应用）就拒绝，不会降级到仓库根目录。`list_repositories` 刻意不返回仓库绝对路径——否则配了文件系统工具的 Agent 就能绕开 `allowed_source_roots` 直接读整仓。越界返回结构化的 `{ok: false, error: "boundary_rejected", ...}` 而不是抛栈，Agent 才知道该改什么。

依赖 `fastmcp`（已写进 `requirements.txt`）；HTTP API 与后端测试都不依赖它。

## 开发约定

- **前端包管理器用 pnpm，不要用 npm。** 项目以 `pnpm-lock.yaml` 为准；若误生成 `package-lock.json` 直接删除、不要提交。安装：`pnpm install`，构建：`pnpm build`，测试：`pnpm test`，开发：`pnpm dev`。
- **前端按钮统一 Element Plus 风格（圆角胶囊）：**
  - `btn-round-primary`：蓝实心 `#409eff`，用于主操作（批准 / 应用 / 运行测试）。
  - `btn-round`：灰边，用于次级 / 取消。
  - `btn-round-danger`：红实心 `#f56c6c`，用于破坏性操作（驳回方案）。
  - 样式定义在 `frontend/src/index.css`，各按钮位于对应 Section 组件内。
- **暗色模式**：保留纯黑底，仅提亮前景色（方案 A），不在组件层改底色。
- **后端目录暂保持扁平**：`main.py` 用扁平 import（`from db import ...`），`uvicorn main:api` 依赖 cwd 在 `backend/`。重构为 `app/` 包（拆分 `api/`、`services/`、`core/`、`schemas/`、`agents/`）的计划已定，但**延后到前端开发完成、单独一个 commit 再做**，避免中途破坏正在运行的后端。
- **Git 回滚点**：`2ad6bc0`（前端 P0/P1 功能）、`452d3ab`（目录清理）。
