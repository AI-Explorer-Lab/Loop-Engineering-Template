<script setup lang="ts">
import { computed } from "vue";

import CopyButton from "./CopyButton.vue";
import type { QueueData, QueueSubtaskStatus } from "../types/task";

const props = defineProps<{ queue: QueueData; disabled?: boolean }>();
const emit = defineEmits<{
  skip: [taskId: string];
  move: [taskId: string, offset: -1 | 1];
}>();

const queueLabels: Record<QueueData["status"], string> = {
  pending: "等待启动",
  running: "执行中",
  pausing: "正在暂停",
  paused: "已暂停",
  cancelling: "正在取消",
  cancelled: "已取消",
  waiting_review: "等待人工审核",
  rejected: "已驳回",
  infrastructure_error: "运行环境故障",
  completed: "全部完成",
};
const subtaskLabels: Record<QueueSubtaskStatus, string> = {
  pending: "等待执行",
  running: "执行中",
  pausing: "正在暂停",
  paused: "已暂停",
  cancelling: "正在取消",
  cancelled: "已取消",
  skipped: "已跳过",
  waiting_review: "等待审核",
  completed: "已完成",
  rejected: "已驳回",
  infrastructure_error: "环境故障",
};
const completedCount = computed(
  () => props.queue.subtasks.filter((task) => ["completed", "skipped"].includes(task.status)).length,
);
const editable = computed(() =>
  ["pending", "paused", "waiting_review", "infrastructure_error"].includes(props.queue.status),
);
const pendingIds = computed(() =>
  props.queue.subtasks.filter((item) => item.status === "pending").map((item) => item.task_id),
);
</script>

<template>
  <section class="surface queue-surface" data-test="queue-progress">
    <div class="surface-heading">
      <div><span class="section-kicker">长任务</span><h2>{{ queue.name }}</h2></div>
      <span class="status-chip" :class="`status-${queue.status}`">
        <i v-if="['pending', 'running', 'pausing', 'cancelling'].includes(queue.status)" />
        {{ queueLabels[queue.status] }} · {{ completedCount }}/{{ queue.subtasks.length }}
      </span>
    </div>
    <div class="progress-track" aria-hidden="true">
      <span :style="{ width: `${queue.subtasks.length ? completedCount / queue.subtasks.length * 100 : 0}%` }" />
    </div>
    <p class="identifier-copy">队列编号 <code>{{ queue.queue_id }}</code><CopyButton :value="queue.queue_id" label="队列编号" /></p>
    <ol class="queue-list">
      <li
        v-for="subtask in queue.subtasks"
        :key="subtask.task_id"
        :class="{
          'current-subtask': subtask.task_id === queue.current_task_id,
          'completed-subtask': subtask.status === 'completed',
          'skipped-subtask': subtask.status === 'skipped',
        }"
      >
        <span class="queue-sequence">{{ subtask.sequence }}</span>
        <div class="queue-copy">
          <strong>{{ subtask.requirement }}</strong>
          <small>{{ subtaskLabels[subtask.status] }} · 交付 {{ subtask.delivery_status }} · {{ subtask.task_id }}</small>
        </div>
        <div v-if="subtask.status === 'pending'" class="row-actions">
          <button
            class="icon-action"
            type="button"
            aria-label="上移子任务"
            :disabled="disabled || !editable || pendingIds[0] === subtask.task_id"
            @click="emit('move', subtask.task_id, -1)"
          >↑</button>
          <button
            class="icon-action"
            type="button"
            aria-label="下移子任务"
            :disabled="disabled || !editable || pendingIds[pendingIds.length - 1] === subtask.task_id"
            @click="emit('move', subtask.task_id, 1)"
          >↓</button>
          <button
            class="text-action danger-text"
            type="button"
            :disabled="disabled || !editable"
            @click="emit('skip', subtask.task_id)"
          >跳过</button>
        </div>
      </li>
    </ol>
    <p class="identifier-copy">队列交付状态 <strong>{{ queue.delivery_status }}</strong></p>
    <div v-if="queue.last_error_summary" class="callout danger-callout">
      <strong>队列需要处理</strong><p>{{ queue.last_error_summary }}</p>
    </div>
  </section>
</template>
