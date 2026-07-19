# Loop Engineering Orchestrator

这是一个可独立部署、本机运行、文件化且严格受控的 Loop Engineering 控制面。它可以登记多个位于任意绝对路径的 Git 项目，为每个任务创建隔离 worktree，调用 Codex 生成和修复代码，执行项目自己的固定验证，再经过人工审查后提交任务分支。

仓库只包含 Orchestrator。Knowledge-Base 和本地 MCP Server 是可选的外部组件，不在本仓库复制或维护。

```text
Loop-Engineering-Template/
├── backend/                       FastAPI 控制面
├── codex_loop/                    编排、隔离、验证与交付状态机
├── frontend/                      Vue 3 管理界面
├── tests/                         Orchestrator 核心测试
├── start.sh                       本地统一启动脚本
└── task.example.json              单任务示例

被管理项目/
├── .git/
├── .codex-orchestrator/
│   ├── worktrees/<task-id>/       codex/<task-id> 专用工作区
│   ├── runs/<task-id>/            单任务状态、事件和审计产物
│   └── queues/<queue-id>/         长任务与嵌套子任务记录
└── 项目源码
```

## 任务链路

```text
任务输入 → 冻结 generation Context → Codex Generator
   ↓
项目级固定验证 → 冻结 evaluation Context
   ↓
独立规范/架构评估 → 同一 Generator thread 修复（最多三轮）
   ↓
人工审查精确 Diff 与 commit subject
   ↓
任务分支单次 commit → 本地归档/中期记忆 → 可选 KB outbox
```

长任务由人工或 Planner 给出严格顺序。每个子任务机器验证通过并经人工批准后，累计 Diff 才会成为下一 worktree 的 index 基线。

## 安装

项目使用 Conda `loop-engineering` 环境：

```bash
conda env update -n loop-engineering -f environment.yml
conda run -n loop-engineering python -c "import mcp, openai_codex"
npm ci
npm ci --prefix frontend
```

`mcp>=1.27,<2` 和 `openai-codex==0.1.0b3` 安装在 `loop-engineering` 环境中。Python 的 `mcp` SDK 是 Orchestrator 的运行依赖，不等于外部 MCP Server 仓库。

## 本地配置

默认配置可以直接启动并管理本仓库，增强 Harness 默认关闭。需要连接外部项目、Knowledge-Base 或 MCP 时：

```bash
cp backend/config/app.local.example.yaml \
  backend/config/app.local.yaml
```

然后在忽略提交的 `app.local.yaml` 中填写：

- 外部 Knowledge-Base 根目录；
- 外部 MCP `registry.json`；
- 每个被管理项目的绝对路径和知识身份；
- 项目自己的验证命令、测试目录和依赖目录。

验证命令是控制面可信配置，不接受模型动态生成；它们以参数数组保存，通过 `shell=False` 在原有外部沙箱中执行。

`agent.harness_enabled=true` 时，必须配置外部 Knowledge-Base 和 MCP registry。两者不随本仓库分发，路径不存在时服务会明确拒绝启动。MCP 使用本机 `stdio`，read/archive 模式严格分离并禁用网络；Generator 只接收冻结后的来源与哈希，不直接调用 MCP。

## 启动

```bash
./start.sh
```

- 页面和 API：<http://127.0.0.1:8100>
- FastAPI 内部端口：`18100`

## CLI

CLI 默认操作配置中的默认项目。选择其他项目时，将 `--project-id` 放在子命令前：

```bash
conda run -n loop-engineering python -m codex_loop \
  --project-id my-project start \
  --task-file task.example.json

conda run -n loop-engineering python -m codex_loop \
  --project-id my-project resume --task-id <task-id>

conda run -n loop-engineering python -m codex_loop \
  --project-id my-project queue-start \
  --task-file queue.example.json

conda run -n loop-engineering python -m codex_loop \
  --project-id my-project queue-show --queue-id <queue-id>
```

人工审查与 Diff SHA-256 绑定。`decision` 支持 `approved`、`changes_requested`、`rejected`。批准后只暂存已审查文件，并在任务分支创建一次快照提交。

## 项目级验证

每个项目可独立定义：

- `required_paths`：任务开始前必须存在的文件或目录；
- `dependency_paths`：需要映射进隔离 worktree 的本地依赖目录；
- `preflight`：运行环境探针；
- `test_groups`：变更测试的发现规则和定向验证命令；
- `full_commands`：每轮必须执行的完整验证。

示例见 `backend/config/app.local.example.yaml`。

## 状态含义

- `machine_status=success`：固定自动验证通过，不代表人工接受。
- `machine_status=manual_review`：三轮验证仍失败，需要人工判断。
- `machine_status=infrastructure_error`：隔离、权限、SDK 或本地工具故障。
- `review_status=pending`：机器流程结束，但尚无人工结论。
- `delivery_status`：独立记录 commit 与 archive 的可恢复检查点。
- 长任务只有当前子任务 `approved` 后才进入下一项。

`active.lock` 只表示进程当前占用执行权。进程异常退出后可以替换过期锁，但必须恢复未完成任务，不能用新任务覆盖。

## 权限与状态边界

- Codex 当前目录和唯一可写根是任务 worktree，不能写控制项目、运行记录或 Git 元数据。
- 审批模式为 deny-all；网络、Web Search、Apps、插件、多 Agent、技能脚本、生产命令和敏感环境变量默认关闭。
- 本地 MCP 只属于控制面；拒绝路径逃逸和跨模式工具。
- 密钥、Token、数据库 URL、云与 SSH/Kubernetes 信息不传入任务。
- Git 变更命令、生产数据库客户端和部署工具被阻断并审计。
- App Server 的实际权限无法证明不宽于请求范围时，首个 Prompt 前停止。
- `.codex-orchestrator/` 位于每个被管理项目中，不提交到本仓库。
- 批准只在 `codex/<task-id>` 任务分支创建一次提交；不会自动 merge、push、创建 PR 或部署。

## 运行记录

单任务保存在 `.codex-orchestrator/runs/<task-id>/`，包括 immutable task、manifest、权限、状态、事件、Prompt/回复、验证日志、最终 Diff、Context、评估、交付、归档、结果和人工报告。

长任务保存在 `.codex-orchestrator/queues/<queue-id>/`；子任务完整记录位于 `subtasks/<task-id>/`，累计批准结果保存在 `changes/cumulative.diff`。没有 `schema_version` 的旧记录仅只读展示，不伪造新产物。

## 核心模块

| 模块 | 职责 |
|---|---|
| `codex_loop/workspace.py` | 创建并核验任务 branch/worktree |
| `codex_loop/policy.py` | 沙箱、环境 allowlist、依赖映射和权限核验 |
| `codex_loop/codex_client.py` | 固定 SDK/runtime、thread 与恢复核对 |
| `codex_loop/validation_profile.py` | 解析每个项目的可信验证配置 |
| `codex_loop/validation_runner.py` | 发现变更测试并运行项目级分层验证 |
| `codex_loop/workflow.py`、`codex_loop/queue_workflow.py` | 单任务与严格串行长任务状态机 |
| `codex_loop/context.py`、`codex_loop/evaluation.py` | 冻结 Context 和独立评估 |
| `codex_loop/git_delivery.py`、`codex_loop/archiver.py` | 审查绑定的 commit 与本地优先归档 |
| `codex_loop/state.py` | 原子持久化、锁、队列目录和脱敏 |

## 验证本仓库

```bash
conda run -n loop-engineering pytest -q tests
conda run -n loop-engineering pytest -q backend/tests
npm --prefix frontend test
npm --prefix frontend run build
git diff --check
```

外部 MCP Server 有自己的测试与仓库，不属于本仓库验证范围。
