# 任务报告

## 任务
- 编号：{{task_id}}
- 队列归属：{{queue_context}}
- 需求：{{requirement}}
- 验收标准：
{{acceptance_criteria}}

## 工作区隔离
- 基线：{{base_ref}} @ {{base_commit}}
- 任务分支：{{task_branch}}
- 独立 worktree：{{worktree}}
- 原仓库是否被修改：否

## 权限
- 文件边界：{{filesystem_summary}}
- 网络：{{network_status}}
- 审批模式：{{approval_mode}}
- 越权拒绝：{{denied_event_count}} 次

## Codex 运行
- Thread：{{thread_id}}
- Turn：{{turn_count}}
- Prompt/回复/事件：{{artifact_links}}

## 验证
{{validation_rounds}}

## 最终变更
- 文件：{{changed_files}}
- Diff：{{diff_path}}
- Diff SHA-256：{{diff_sha256}}
- 敏感信息替换：{{redaction_count}}

## 人工审查
- 状态：{{review_status}}
- 审查人：{{reviewer}}
- 结论：{{decision}}
- 说明：{{comment}}
- 审查对应 Diff：{{reviewed_diff_sha256}}
