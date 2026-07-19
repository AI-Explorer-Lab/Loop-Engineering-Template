<script setup lang="ts">
import type { EventRecord } from "../types/task";

defineProps<{ events: EventRecord[] }>();

const labels: Record<string, string> = {
  "run.started": "任务启动",
  "run.resumed": "继续运行",
  "run.paused": "任务暂停",
  "run.cancelled": "任务取消",
  "run.completed": "任务完成",
  "queue.started": "队列启动",
  "queue.resumed": "队列继续",
  "queue.paused": "队列暂停",
  "queue.cancelled": "队列取消",
  "validation.started": "开始验证",
  "validation.completed": "验证完成",
  "codex.turn.started": "Codex 开始处理",
  "codex.turn.completed": "Codex 返回结果",
  "review.recorded": "人工审核完成",
};

function eventType(event: EventRecord): string {
  return String(event.type || event.event || "event");
}

function title(event: EventRecord): string {
  const type = eventType(event);
  return labels[type] || type.replaceAll(".", " · ");
}

function summary(event: EventRecord): string {
  const payload = event.payload;
  if (!payload || typeof payload !== "object") return "";
  const value = JSON.stringify(payload, null, 0);
  return value === "{}" ? "" : value;
}

function formatTime(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? value
    : date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}
</script>

<template>
  <section class="surface timeline-surface">
    <div class="surface-heading compact-heading">
      <div><span class="section-kicker">实时记录</span><h2>事件时间线</h2></div>
      <span class="live-indicator"><i /> 实时</span>
    </div>
    <div v-if="events.length" class="event-list">
      <article v-for="event in events.slice().reverse()" :key="event.seq" class="event-row">
        <span class="event-marker" />
        <div>
          <strong>{{ title(event) }}</strong>
          <p v-if="summary(event)">{{ summary(event) }}</p>
        </div>
        <time>{{ formatTime(event.timestamp) }}</time>
      </article>
    </div>
    <div v-else class="empty-inline">运行事件出现后会在这里实时更新。</div>
  </section>
</template>
