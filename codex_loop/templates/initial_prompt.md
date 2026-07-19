# 任务
任务编号：{{task_id}}
队列归属：{{queue_context}}
需求：{{requirement}}

# 验收标准
{{acceptance_criteria}}

# 执行边界
- 当前目录是本任务的独立 Git worktree：{{worktree}}。
- 基线 commit：{{base_commit}}；任务 branch：{{task_branch}}。
- 查看项目文件列表时，统一使用 `git ls-files --cached --others --exclude-standard`，不要执行 `rg`，也不要扫描 `.git` 或 `.codex-runtime`。
- 只能修改当前 worktree 中与需求有关的文件。
- 不得切换 branch、提交代码、修改 Git 元数据或请求扩大权限。
- 不得访问工作区外文件、网络、生产凭据、生产数据库或部署环境。
- 权限扩大请求会被系统拒绝并记录，不要尝试绕过限制。

# 完成要求
- 先理解现有实现，再进行最小且完整的修改。
- 为新增行为补充或修改测试，不得删除或弱化已有测试。
- 不执行部署、数据库迁移或生产操作。
- 最终回复只说明修改内容、验证情况和仍需人工关注的问题。
