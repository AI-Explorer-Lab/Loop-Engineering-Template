# 人工审查修改任务

你正在继续任务 `{{task_id}}` 的第 {{turn_number}} 次 Codex 执行。继续使用当前工作区和已有实现，不要重新开始任务。

队列归属：{{queue_context}}

## 原始需求

{{requirement}}

## 人工审查意见

{{review_comment}}

## 当前改动

- 已变更文件：{{changed_files}}
- 当前 Diff SHA-256：{{diff_sha256}}

只处理人工审查意见并保持原验收标准。查看项目文件列表时，统一使用 `git ls-files --cached --others --exclude-standard`，不要执行 `rg`，也不要扫描 `.git` 或 `.codex-runtime`。完成修改后不要提交、推送或合并代码；固定验证将由 Python 编排器执行。
