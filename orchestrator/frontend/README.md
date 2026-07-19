# Codex Orchestrator Frontend

这是一个 Vue 3 + TypeScript 单页界面，可以直接提交单任务、手工编排串行队列，也可以先生成自动 Plan 再由人工编辑确认。页面展示队列进度、冻结 Context、四层评估、知识依据、当前 worktree、验证轮次、Diff、commit 和归档状态。

## 安装与启动

Node 依赖只安装在当前前端目录：

```bash
npm ci --prefix orchestrator/frontend
npm --prefix orchestrator/frontend run dev
```

页面默认地址是 `http://127.0.0.1:8100`。通过 `orchestrator/start.sh` 启动时，开发服务器会把 `/api` 请求代理到仅供本机进程通信的后端端口 `18100`。

提交区可切换“单任务 / 长任务 / 自动规划”。长任务默认至少两个子任务，卡片顺序就是执行顺序，不显示依赖选择。自动 Plan 可以返回一条任务或多条队列；预览会展示每条原始验收标准的 `AC-xxx` 映射、遗漏和重复，允许修改任务切片、映射及顺序。点击明确确认前不会创建任务、队列、branch 或 worktree。

审查页同时展示语法、逻辑、规范、架构结果和冻结知识的成熟度/强度。`not_evaluated` 只是信息；draft、旧文档、pitfall 或冲突知识只显示警告；只有有效强约束且带具体违规位置的 finding 才能触发修复。批准时必须确认单行 commit subject，按钮明确提示只提交任务分支、不 merge/push。

页面每两秒查询活动队列，自动切换到当前子任务；机器流程完成后读取报告与 Diff。批准后先完成 commit 与本地归档，再进入下一项；要求修改继续使用原 worktree/thread，驳回停止队列。commit 与 archive 分别提供恢复按钮，归档重试不会重跑 Archiver 角色。

浏览器只保存最近一次任务类型及编号。刷新后，页面从后端文件记录恢复完整队列和当前子任务，不把调度状态保存在浏览器中。

旧任务会显示“历史记录不完整”，不会显示伪造的隔离、权限或审查数据，也不能提交审查。

## 测试与构建

```bash
npm --prefix orchestrator/frontend test
npm --prefix orchestrator/frontend run build
```

构建结果位于 `orchestrator/frontend/dist/`，该目录和 `node_modules/` 都不会提交到 Git。

当前版本不包含账号、远程部署、路由、Pinia、WebSocket 或任务内并行。人工批准会在正确任务分支触发一次 commit，但不会合并、推送、创建 PR 或部署。设置页只读显示 MCP/Knowledge-Base/Skills 健康和文件化指标，不回传敏感配置。旧记录缺少新产物时显示“该历史记录不具备此能力”，不会伪造 Context、评估、commit 或归档结果。
