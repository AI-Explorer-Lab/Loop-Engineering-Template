# Loop Engineering Orchestrator

这是一个可独立部署的本地 Loop Engineering 控制面。它可以登记多个位于任意绝对路径的 Git 项目，为每个任务创建隔离 worktree，调用 Codex 生成和修复代码，执行项目自己的固定验证，再经过人工审查后提交任务分支。

仓库只包含 Orchestrator。Knowledge-Base 和本地 MCP Server 是可选的外部组件，不在本仓库复制或维护。

## 安装

项目使用现有 Conda `loop-engineering` 环境：

```bash
conda env update -n loop-engineering -f environment.yml
conda run -n loop-engineering python -c "import mcp, openai_codex"
npm ci --prefix orchestrator
npm ci --prefix orchestrator/frontend
```

Python 的 `mcp` SDK 是 Orchestrator 的运行依赖，会安装到 `loop-engineering` 环境；它不等于外部 MCP Server 仓库。

## 本地配置

默认配置可以直接启动并管理本仓库，增强 Harness 默认关闭。需要连接外部项目、Knowledge-Base 或 MCP 时：

```bash
cp orchestrator/backend/config/app.local.example.yaml \
  orchestrator/backend/config/app.local.yaml
```

然后在忽略提交的 `app.local.yaml` 中填写：

- 外部 Knowledge-Base 根目录；
- 外部 MCP `registry.json`；
- 每个被管理项目的绝对路径、知识身份；
- 项目自己的验证命令、测试目录和依赖目录。

外部路径缺失时，增强 Harness 会明确拒绝启动，不会静默绕过知识或评估能力。

## 启动

```bash
./orchestrator/start.sh
```

- 页面和 API：<http://127.0.0.1:8100>
- FastAPI 内部端口：`18100`

CLI 默认操作配置中的默认项目，也可以在子命令前选择其他项目：

```bash
conda run -n loop-engineering python -m orchestrator.codex_loop \
  --project-id my-project start \
  --task-file orchestrator/task.example.json
```

## 项目级验证

验证配置由操作者维护，不来自 Prompt 或模型回复。命令以参数数组保存，通过 `shell=False` 在原有外部沙箱中执行。每个项目可独立定义：

- `required_paths`：任务开始前必须存在的文件或目录；
- `dependency_paths`：需要映射进隔离 worktree 的本地依赖目录；
- `preflight`：运行环境探针；
- `test_groups`：变更测试的发现规则和定向验证命令；
- `full_commands`：每轮必须执行的完整验证。

示例见 `orchestrator/backend/config/app.local.example.yaml`。

## 安全与状态边界

- Codex 只写任务 worktree，不能写控制项目、运行记录或 Git 元数据。
- 网络、Web Search、Apps、插件、多 Agent、生产命令和敏感环境变量默认关闭。
- `.codex-orchestrator/` 位于每个被管理项目中，保存该项目自己的 runs、queues、worktrees、事件与审查记录，不提交到本仓库。
- 批准只在 `codex/<task-id>` 任务分支创建一次提交；不会自动 merge、push、创建 PR 或部署。
- KB/MCP 仅由控制面访问，Generator 不直接获得这些能力。

更完整的状态机、运行产物和 API 说明见 [`orchestrator/README.md`](orchestrator/README.md)。

## 验证本仓库

```bash
conda run -n loop-engineering pytest -q orchestrator/tests
conda run -n loop-engineering pytest -q orchestrator/backend/tests
npm --prefix orchestrator/frontend test
npm --prefix orchestrator/frontend run build
git diff --check
```
