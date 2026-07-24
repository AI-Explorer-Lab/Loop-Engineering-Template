<script setup lang="ts">
import { computed } from "vue";

import CopyButton from "./CopyButton.vue";
import {
  deliveryProgressFor,
  useOrchestrator,
} from "../composables/useOrchestrator";
import type { TaskData } from "../types/task";

const props = defineProps<{ task: TaskData }>();
const store = useOrchestrator();

const labels: Record<TaskData["status"], string> = {
  accepted: "已接收",
  running: "执行中",
  pausing: "正在暂停",
  paused: "已暂停",
  cancelling: "正在取消",
  cancelled: "已取消",
  success: "机器验证通过",
  manual_review: "需要人工判断",
  infrastructure_error: "运行环境故障",
};

const reviewLabels: Record<TaskData["review_status"], string> = {
  pending: "待人工审核",
  approved: "已批准",
  changes_requested: "要求修改",
  rejected: "已驳回",
  unavailable: "历史信息缺失",
};
const deliveryLabels: Record<TaskData["delivery_status"], string> = {
  not_ready: "未进入交付",
  commit_pending: "等待 commit",
  committing: "正在 commit",
  committed: "Commit 已完成",
  archive_pending: "Archiver 正在生成归档/写入知识",
  archived: "归档完成",
  failed: "交付失败",
  unavailable: "历史信息缺失",
};

const statusLabel = computed(() => labels[props.task.status]);
const reviewLabel = computed(() => reviewLabels[props.task.review_status]);
const deliveryProgress = computed(() =>
  deliveryProgressFor(
    props.task,
    store.capabilities.value === null
      ? null
      : store.capabilities.value.status !== "unavailable",
  ),
);
const effectivePermissions = computed(() => {
  const effective = props.task.permissions.effective;
  return typeof effective === "object" && effective !== null
    ? (effective as Record<string, unknown>)
    : {};
});
const elapsed = computed(() => {
  const started = new Date(props.task.started_at).getTime();
  const ended = new Date(
    props.task.finished_at || props.task.updated_at || props.task.started_at,
  ).getTime();
  if (!Number.isFinite(started) || !Number.isFinite(ended)) return "—";
  const seconds = Math.max(0, Math.round((ended - started) / 1000));
  if (seconds < 60) return `${seconds} 秒`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes} 分 ${seconds % 60} 秒`;
});

function formatTime(value: string | null): string {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN");
}
</script>

<template>
  <section class="surface status-surface" data-test="task-status">
    <div class="surface-heading">
      <div>
        <span class="section-kicker">当前任务</span>
        <h2>{{ task.requirement }}</h2>
      </div>
      <span class="status-chip" :class="`status-${task.status}`">
        <i v-if="['accepted', 'running', 'pausing', 'cancelling'].includes(task.status)" />
        {{ statusLabel }}
      </span>
    </div>

    <dl v-if="deliveryProgress.visible" class="review-result delivery-state-card" data-test="delivery-progress">
      <div><dt>审批</dt><dd>{{ deliveryProgress.review }}</dd></div>
      <div><dt>Commit</dt><dd>{{ deliveryProgress.commit }}</dd></div>
      <div><dt>Archiver</dt><dd>{{ deliveryProgress.archive }}</dd></div>
    </dl>

    <div class="metric-strip">
      <div><span>当前阶段</span><strong>{{ task.phase || "等待启动" }}</strong></div>
      <div><span>Codex 轮次</span><strong>{{ task.turn_count }}</strong></div>
      <div><span>验证轮次</span><strong>{{ task.rounds.length }}</strong></div>
      <div><span>实际耗时</span><strong>{{ elapsed }}</strong></div>
    </div>

    <details class="technical-details">
      <summary>运行与隔离详情</summary>
      <dl class="detail-grid">
        <div><dt>任务编号</dt><dd class="copy-value"><span>{{ task.task_id }}</span><CopyButton :value="task.task_id" label="任务编号" /></dd></div>
        <div><dt>更新时间</dt><dd>{{ formatTime(task.updated_at) }}</dd></div>
        <div><dt>人工审核</dt><dd>{{ reviewLabel }}</dd></div>
        <div><dt>交付状态</dt><dd>{{ deliveryLabels[task.delivery_status] }}</dd></div>
        <div><dt>Context 快照</dt><dd>{{ Object.keys(task.context).length ? "已冻结" : "该记录不具备" }}</dd></div>
        <div><dt>四层评估</dt><dd>{{ Object.keys(task.evaluations).length ? "已有产物" : "该记录不具备" }}</dd></div>
        <div v-if="task.commit.commit_sha"><dt>Commit</dt><dd class="copy-value"><span>{{ task.commit.commit_sha }}</span><CopyButton :value="String(task.commit.commit_sha)" label="commit" /></dd></div>
        <div><dt>Thread</dt><dd class="copy-value"><span>{{ task.thread_id || "尚未创建" }}</span><CopyButton v-if="task.thread_id" :value="task.thread_id" label="Thread" /></dd></div>
        <div v-if="!task.legacy"><dt>任务分支</dt><dd>{{ task.workspace.task_branch || "—" }}</dd></div>
        <div v-if="!task.legacy"><dt>基线 commit</dt><dd class="copy-value"><span>{{ task.workspace.base_commit || "—" }}</span><CopyButton v-if="task.workspace.base_commit" :value="String(task.workspace.base_commit)" label="commit" /></dd></div>
        <div v-if="!task.legacy"><dt>独立 worktree</dt><dd>{{ task.workspace.worktree || "—" }}</dd></div>
        <div v-if="!task.legacy"><dt>权限核验</dt><dd>{{ effectivePermissions.verified ? "已通过" : "未通过" }}</dd></div>
        <div v-if="!task.legacy"><dt>网络</dt><dd>{{ effectivePermissions.network || "disabled" }}</dd></div>
        <div v-if="!task.legacy"><dt>越权拒绝</dt><dd>{{ task.audit_summary.denied_event_count || 0 }} 次</dd></div>
      </dl>
    </details>

    <div v-if="task.history_warning" class="callout warning-callout">
      <strong>历史记录不完整</strong><p>{{ task.history_warning }}</p>
    </div>
    <div v-if="task.infrastructure_error" class="callout danger-callout">
      <strong>运行环境故障</strong><p>{{ task.infrastructure_error }}</p>
    </div>
    <div v-else-if="task.delivery_status === 'failed'" class="callout danger-callout">
      <strong>自动交付需要处理</strong><p>{{ task.last_error_summary || "可从审核页重试 commit 或知识归档。" }}</p>
    </div>
    <div v-else-if="task.status === 'manual_review'" class="callout warning-callout">
      <strong>机器流程需要人工判断</strong>
      <p>{{ task.last_error_summary || "代码已连续三轮验证失败。" }}</p>
    </div>
  </section>
</template>
