# Codex Orchestrator API

这个 FastAPI 服务把网页请求交给现有 `codex_loop`，并负责增强 Harness 的控制面：本地 MCP、Context、Planner、独立评估、人工 Commit Gate 和 Archiver。它不创建第二套任务数据；单任务来自 `.codex-orchestrator/runs/`，长任务及其子任务来自 `.codex-orchestrator/queues/`。

## 安装

所有 Python 依赖都安装到 Conda `loop-engineering` 环境，不创建 `.venv`：

```bash
conda activate loop-engineering
python -m pip install -r requirements.txt
python -m pip install -r backend/requirements.txt
npm ci
```

最后一条命令安装现有编排器固定版本的 Codex runtime。服务继续复用本机 Codex 登录，不使用 API Key。

## 启动

在仓库根目录运行：

```bash
conda run -n loop-engineering uvicorn backend.main:app \
  --reload --host 127.0.0.1 --port 18100
```

通过根目录的 `start.sh` 启动时，页面和 API 统一从
`http://127.0.0.1:8100` 访问；`18100` 仅用于 Vite 与 FastAPI 之间的本机通信。
默认配置位于 `backend/config/app.yaml`，机器专属覆盖写入忽略提交的
`backend/config/app.local.yaml`；示例见 `backend/config/app.local.example.yaml`。

`agent.harness_enabled=true` 时，每个 `agent.projects[]` 必须配置
`knowledge_actor_id`，全局 `agent.knowledge` 必须指向使用者自行准备的
Knowledge-Base 和 MCP registry。每个项目还需配置自己的验证命令和依赖目录。
这些身份只用于知识访问与审计，不是登录账号。能力不可用时服务启动失败关闭，
不会静默退回无知识评估。

发布默认关闭。启用时，项目的 `publish` 配置必须同时固定 `remote_name` 和
`remote_url`；通过前端创建的新项目可以自动创建私有 GitHub 仓库，并默认把首次发布目标设为
`main`。服务会在实际 push 前再次核对任务分支、HEAD、工作区、commit 证据和仓库远端，不能把
发布远端或目标分支交给任务或模型动态决定。

## 接口

| 方法 | 地址 | 用途 |
|---|---|---|
| `GET` | `/api/health` | 检查 API 是否可用，不启动 Codex |
| `POST` | `/api/plans` | 生成无执行副作用的单任务/队列 Plan 草稿 |
| `GET` | `/api/plans/{plan_id}` | 读取尚未确认或已归档到任务根的 Plan |
| `POST` | `/api/plans/{plan_id}/confirm` | 提交人工编辑与确认后创建任务或队列 |
| `POST` | `/api/tasks` | 提交需求，立即返回 `202` 和任务编号 |
| `GET` | `/api/tasks/{task_id}` | 查询阶段、验证轮次和最终状态 |
| `POST` | `/api/tasks/{task_id}/resume` | 后端中断后恢复原任务和原 thread |
| `POST` | `/api/tasks/{task_id}/pause` | 请求暂停任务 |
| `POST` | `/api/tasks/{task_id}/cancel` | 请求取消任务 |
| `POST` | `/api/tasks/{task_id}/rerun` | 对已结束任务创建重新运行 |
| `GET` | `/api/tasks/{task_id}/report` | 读取完成后的 `report.md` |
| `GET` | `/api/tasks/{task_id}/diff` | 只读获取脱敏后的最终 diff |
| `POST` | `/api/tasks/{task_id}/review` | 提交一次人工结论并绑定 diff SHA-256 |
| `POST` | `/api/tasks/{task_id}/delivery/retry` | 幂等恢复批准后的 commit 检查点 |
| `POST` | `/api/tasks/{task_id}/archive/retry` | 只重试已有归档 outbox，不重跑 Archiver |
| `POST` | `/api/tasks/{task_id}/publish` | 再次人工确认后推送已归档的单任务分支 |
| `POST` | `/api/queues` | 提交至少两个有固定顺序的子任务 |
| `GET` | `/api/queues/{queue_id}` | 查询整体进度和当前子任务 |
| `POST` | `/api/queues/{queue_id}/resume` | 环境故障修复后恢复当前子任务 |
| `POST` | `/api/queues/{queue_id}/pause` | 请求暂停队列 |
| `POST` | `/api/queues/{queue_id}/cancel` | 请求取消队列 |
| `POST` | `/api/queues/{queue_id}/rerun` | 对已结束队列创建重新运行 |
| `POST` | `/api/queues/{queue_id}/subtasks/{task_id}/skip` | 跳过指定的待执行子任务 |
| `POST` | `/api/queues/{queue_id}/reorder` | 在版本匹配时重排待执行子任务 |
| `GET` | `/api/queues/{queue_id}/report` | 读取长任务汇总报告 |
| `GET` | `/api/queues/{queue_id}/diff` | 读取已批准的最终累计 Diff |
| `GET` | `/api/projects` | 查看已登记项目 |
| `POST` | `/api/projects` | 创建本地 Git 项目并立即注册 |
| `GET` | `/api/capabilities` | 只读查看 Knowledge-Base、MCP、Skills 和归档积压 |
| `GET` | `/api/metrics` | 从文件记录计算成功率、分层失败、修复与交付指标 |
| `GET` | `/api/history` | 分页查询任务和队列历史 |
| `GET` | `/api/history/{kind}/{identifier}/events` | 分页读取事件 |
| `GET` | `/api/history/{kind}/{identifier}/stream` | 以 SSE 持续读取事件 |
| `GET` | `/api/history/{kind}/{identifier}/logs` | 查看运行日志清单 |
| `GET` | `/api/history/{kind}/{identifier}/logs/{log_id}` | 读取指定日志 |
| `GET/POST` | `/api/notifications*` | 查询通知、标记已读和更新通知设置 |

创建任务示例：

```bash
curl -X POST http://127.0.0.1:8100/api/tasks \
  -H 'Content-Type: application/json' \
  -d '{"requirement":"交易列表支持按最低金额筛选","acceptance_criteria":["传入 min_amount=100 时，只返回金额大于或等于 100 的交易"]}'
```

API 在单工作线程中执行 Codex，因此 HTTP 请求不会等待开发和测试结束。同一时间只能有一个活动任务；已有未完成任务时，先使用恢复接口，不要创建新任务。

新项目通过项目页的“新建项目”创建，提交项目名称和绝对目标路径即可。后端只接受尚不存在的新目录，会创建目录、初始化 Git、提交可追踪的 `.harness/project.json` 和安全边界用的 `.gitignore`，并写入默认 Python `unittest` 验证配置。认证信息与临时运行状态留在本地隔离目录。项目注册保存在控制面 `.codex-orchestrator/projects.json`，不再要求为每个新项目手动编辑 `app.local.yaml`。

创建长任务时，`subtasks` 数组顺序就是唯一执行顺序，不接收依赖字段。每项分别包含 `requirement` 和至少一条 `acceptance_criteria`。当前子任务机器流程结束后，仍使用 `/api/tasks/{task_id}`、`report`、`diff` 和 `review` 查看及审查；批准后后台单工作线程才会执行下一项。

审查请求包含 `decision`、`reviewer`、`comment`、`reviewed_diff_sha256` 和批准时的单行 `commit_subject`。只允许 `approved`、`changes_requested`、`rejected`；任务未结束、Diff/HEAD/branch/tree 已变化、Diff 含疑似密钥、Git identity 缺失或旧任务时返回冲突。批准只在任务分支创建一个与审查绑定的 commit；审查接口不执行 merge、push、PR、rebase 或 tag。单任务结论不可覆盖；队列子任务的多次返修审查按历史追加保存。

发布请求只接受单任务，并要求 `review_status=approved`、`delivery_status=archived`、已确认的 commit SHA 和发布确认人。发布服务会核对记录中的 task branch、HEAD、工作区和配置的远端 URL；新项目首次发布会先将本地 `main` 快进到该 commit，再 push 远端 `main`。队列子任务不能单独发布，发布失败不会伪造成功记录。

Plan 草稿只保存输入、冻结 Context、结构化角色输出和验收映射，不创建 worktree。人工确认必须保留原始需求哈希、Context 哈希和全部 `AC-xxx`，一条子任务复用单任务服务，两条以上复用严格串行队列服务。

commit 成功后先写本地 `archive/packet.json`、`summary.json` 与项目级中期记忆，再处理 Knowledge-Base outbox。知识写入失败不会回滚 commit 或重复执行队列；`archive/retry` 只继续未完成 outbox 项。

## 测试

后端测试使用假的 workflow 和临时任务目录，不会真实调用 Codex：

```bash
env PYTHONPYCACHEPREFIX=/private/tmp/loop-engineering-pycache \
  conda run -n loop-engineering pytest -q backend/tests
```

没有 schema version 的旧任务只读展示，并明确标记历史信息不完整；不能恢复或提交审查。测试只使用临时 Git 与 Knowledge-Base fixture；系统不会自动合并、推送或部署。
