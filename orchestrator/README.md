# Codex Orchestrator

这是一个本机、文件化、严格受控的 Loop Engineering Harness。它把被管理项目与控制面分开：项目可以位于任意已登记路径，任务状态与 worktree 保存在对应项目中；本目录只提供通用编排、Web/API、Codex runtime 和安全策略。

```text
被管理项目
├── .git/
├── .codex-orchestrator/
│   ├── worktrees/<task-id>/       codex/<task-id> 专用工作区
│   ├── runs/<task-id>/            单任务状态、事件和审计产物
│   └── queues/<queue-id>/         长任务与嵌套子任务记录
└── 项目源码

Loop-Engineering-Template
└── orchestrator/                  独立控制面
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

## 依赖与启动

```bash
conda activate loop-engineering
python -m pip install -r orchestrator/requirements.txt
python -m pip install -r orchestrator/backend/requirements.txt
npm ci --prefix orchestrator
npm ci --prefix orchestrator/frontend
./orchestrator/start.sh
```

`mcp>=1.27,<2` 和 `openai-codex==0.1.0b3` 安装在 `loop-engineering` 中。前者是官方 Python SDK，不包含外部 MCP Server 或 Knowledge-Base。

页面和 API 统一从 `http://127.0.0.1:8100` 访问，Vite 将 `/api` 转发到本机后端端口 `18100`。

## 配置外部项目与知识能力

默认配置位于 `backend/config/app.yaml`。机器专属配置使用忽略提交的 `backend/config/app.local.yaml`：

```bash
cp orchestrator/backend/config/app.local.example.yaml \
  orchestrator/backend/config/app.local.yaml
```

每个 `agent.projects[]` 项目包含自己的：

- `repo_root` 和 `project_id`；
- 可选知识消费身份；
- 验证所需路径、依赖目录、运行环境探针；
- 变更测试规则与完整验证命令。

这些命令是控制面可信配置，不接受模型动态生成；仍以参数数组和 `shell=False` 在验证沙箱内执行。

`agent.harness_enabled=true` 时，还必须配置外部 Knowledge-Base 和 MCP registry。两者不随本仓库分发，路径不存在时服务失败关闭。MCP 使用本机 `stdio`，read/archive 模式严格分离并禁用网络；Generator 只接收冻结后的来源与哈希，不直接调用 MCP。

## CLI

CLI 默认操作配置中的默认项目。选择其他项目时，将 `--project-id` 放在子命令前：

```bash
python -m orchestrator.codex_loop --project-id my-project \
  start --task-file orchestrator/task.example.json
python -m orchestrator.codex_loop --project-id my-project \
  resume --task-id <task-id>
python -m orchestrator.codex_loop --project-id my-project \
  queue-start --task-file orchestrator/queue.example.json
python -m orchestrator.codex_loop --project-id my-project \
  queue-show --queue-id <queue-id>
```

人工审查与 Diff SHA-256 绑定。`decision` 支持 `approved`、`changes_requested`、`rejected`。批准后只暂存已审查文件，并在任务分支创建一次快照提交。

## 状态含义

- `machine_status=success`：固定自动验证通过，不代表人工接受。
- `machine_status=manual_review`：三轮验证仍失败，需要人工判断。
- `machine_status=infrastructure_error`：隔离、权限、SDK 或本地工具故障。
- `review_status=pending`：机器流程结束，但尚无人工结论。
- `delivery_status`：独立记录 commit 与 archive 的可恢复检查点。
- 长任务只有当前子任务 `approved` 后才进入下一项。

`active.lock` 只表示进程当前占用执行权。进程异常退出后可以替换过期锁，但必须恢复未完成任务，不能用新任务覆盖。

## 权限边界

- Codex 当前目录和唯一可写根是任务 worktree。
- 审批模式为 deny-all；网络、Web Search、Apps、插件、多 Agent 和技能脚本关闭。
- 本地 MCP 只属于控制面；拒绝路径逃逸和跨模式工具。
- 密钥、Token、数据库 URL、云与 SSH/Kubernetes 信息不传入任务。
- Git 变更命令、生产数据库客户端和部署工具被阻断并审计。
- App Server 的实际权限无法证明不宽于请求范围时，首个 Prompt 前停止。

## 运行记录

单任务保存在 `.codex-orchestrator/runs/<task-id>/`，包括 immutable task、manifest、权限、状态、事件、Prompt/回复、验证日志、最终 Diff、Context、评估、交付、归档、结果和人工报告。

长任务保存在 `.codex-orchestrator/queues/<queue-id>/`；子任务完整记录位于 `subtasks/<task-id>/`，累计批准结果保存在 `changes/cumulative.diff`。没有 `schema_version` 的旧记录仅只读展示，不伪造新产物。

## 核心模块

| 模块 | 职责 |
|---|---|
| `workspace.py` | 创建并核验任务 branch/worktree |
| `policy.py` | 沙箱、环境 allowlist、依赖映射和权限核验 |
| `codex_client.py` | 固定 SDK/runtime、thread 与恢复核对 |
| `validation_profile.py` | 解析每个项目的可信验证配置 |
| `validation_runner.py` | 发现变更测试并运行项目级分层验证 |
| `workflow.py`、`queue_workflow.py` | 单任务与严格串行长任务状态机 |
| `context.py`、`evaluation.py` | 冻结 Context 和独立评估 |
| `git_delivery.py`、`archiver.py` | 审查绑定的 commit 与本地优先归档 |
| `state.py` | 原子持久化、锁、队列目录和脱敏 |

## 测试

```bash
conda run -n loop-engineering pytest -q orchestrator/tests
conda run -n loop-engineering pytest -q orchestrator/backend/tests
npm --prefix orchestrator/frontend test
npm --prefix orchestrator/frontend run build
```

外部 MCP Server 有自己的测试与仓库，不属于本仓库验证范围。
