<script setup lang="ts">
import { computed } from "vue";
import type { EventRecord } from "../types/task";
import { useOrchestrator } from "../composables/useOrchestrator";

const props = defineProps<{ events: EventRecord[] }>();
const store = useOrchestrator();

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
  "context.assembled": "运行上下文已准备",
  "evaluation.completed": "结果评估完成",
  "commit.completed": "代码已提交",
  "archive.queued": "已进入归档队列",
  "archive.completed": "知识归档完成",
  "review.recorded": "人工审核完成",
  "permission.denied": "操作被阻止",
  "queue.created": "队列已创建",
  "subtask.started": "子任务已开始",
  "subtask.skipped": "子任务已跳过",
  "subtask.infrastructure_error": "子任务基础设施异常",
  "queue.reordered": "队列顺序已调整",
  "command.started": "命令开始执行",
  "file.changed": "代码已更新",
  "workspace.created": "工作区已创建",
  "run.created": "运行记录已创建",
  "backend_architecture.context_bound": "后端架构上下文已绑定",
  "backend_architecture.bootstrap_loaded": "后端架构知识已加载",
  "backend_architecture.bootstrap_reused": "已复用后端架构知识",
  "context.fixed_assembled": "固定上下文已组装",
  "knowledge.write_failed": "知识归档失败",
  "delivery.published": "代码已发布",
};

const stageLabels: Record<string, string> = {
  planning: "规划阶段",
  generation: "生成阶段",
  validation: "验证阶段",
  review: "审核阶段",
  archive: "归档阶段",
  publish: "发布阶段",
  other: "其他阶段",
};

const toolLabels: Record<string, string> = {
  knowledge_catalog: "知识目录",
  knowledge_search: "知识检索",
  knowledge_read: "知识读取",
  knowledge_write: "知识写入",
};

function eventType(event: EventRecord): string {
  return String(event.type || event.event || "event");
}

function payloadOf(event: EventRecord): Record<string, unknown> {
  const payload = event.payload;
  return payload && typeof payload === "object" && !Array.isArray(payload)
    ? payload as Record<string, unknown>
    : {};
}

function itemOf(event: EventRecord): Record<string, unknown> {
  const item = payloadOf(event).item;
  return item && typeof item === "object" && !Array.isArray(item)
    ? item as Record<string, unknown>
    : {};
}

function commandOf(event: EventRecord): string {
  const command = payloadOf(event).command;
  return Array.isArray(command) ? command.join(" ") : String(command || "");
}

function changedPaths(event: EventRecord): string[] {
  const changes = itemOf(event).changes;
  if (!Array.isArray(changes)) return [];
  return changes
    .filter((change): change is Record<string, unknown> =>
      Boolean(change && typeof change === "object" && !Array.isArray(change)),
    )
    .map((change) => String(change.path || ""))
    .filter(Boolean);
}

function isInternalEvent(event: EventRecord): boolean {
  const type = eventType(event);
  return type.startsWith("codex.item.")
    || type === "prompt.saved"
    || type === "turn.diff.updated";
}

function isMcpEvent(event: EventRecord): boolean {
  return eventType(event) === "mcp.tool_completed";
}

type McpGroup = {
  stage: string;
  firstSeq: number;
  lastSeq: number;
  events: EventRecord[];
  counts: Record<string, number>;
};

const mcpGroups = computed<McpGroup[]>(() => {
  const ordered = props.events.slice().sort((left, right) => left.seq - right.seq);
  const contexts = ordered.filter((event) => eventType(event) === "context.assembled");
  const groups = new Map<string, McpGroup>();
  for (const event of ordered.filter(isMcpEvent)) {
    const nextContext = contexts.find((context) => context.seq > event.seq);
    const stagePayload = nextContext
      ? payloadOf(nextContext)
      : contexts.length
        ? payloadOf(contexts[contexts.length - 1])
        : {};
    const stage = String(stagePayload.stage || "其他阶段");
    const tool = String(payloadOf(event).tool || "unknown");
    const group = groups.get(stage) || {
      stage,
      firstSeq: event.seq,
      lastSeq: event.seq,
      events: [],
      counts: {},
    };
    group.firstSeq = Math.min(group.firstSeq, event.seq);
    group.lastSeq = Math.max(group.lastSeq, event.seq);
    group.events.push(event);
    group.counts[tool] = (group.counts[tool] || 0) + 1;
    groups.set(stage, group);
  }
  return [...groups.values()].sort((left, right) => right.lastSeq - left.lastSeq);
});

const visibleEvents = computed(() =>
  props.events.filter((event) => !isInternalEvent(event) && !isMcpEvent(event)),
);
const hiddenEvents = computed(() =>
  props.events.filter((event) => isInternalEvent(event)),
);

function commandKind(command: string): "test" | "build" | "generic" {
  const normalized = command.toLowerCase();
  if (normalized.includes("unittest") || normalized.includes("pytest") || normalized.includes(" test")) {
    return "test";
  }
  if (normalized.includes("build") || normalized.includes("compile")) return "build";
  return "generic";
}

function title(event: EventRecord): string {
  const type = eventType(event);
  if (type === "command.completed") {
    const command = commandOf(event);
    const passed = payloadOf(event).exit_code === 0;
    const kind = commandKind(command);
    if (kind === "test") return passed ? "测试通过" : "测试失败";
    if (kind === "build") return passed ? "构建通过" : "构建失败";
    return passed ? "命令执行成功" : "命令执行失败";
  }
  if (type === "validation.completed") {
    return payloadOf(event).passed === true ? "验证通过" : "验证未通过";
  }
  return labels[type] || "系统事件";
}

function summary(event: EventRecord): string {
  const type = eventType(event);
  const payload = payloadOf(event);
  if (type === "command.completed") {
    const kind = commandKind(commandOf(event));
    const duration = Number(payload.duration_seconds);
    const action = kind === "test" ? "运行单元测试" : kind === "build" ? "运行项目构建" : "执行验证命令";
    return Number.isFinite(duration) && duration >= 0
      ? `${action}，耗时 ${duration.toFixed(2)} 秒`
      : `${action}已结束`;
  }
  if (type === "validation.completed") {
    return String(payload.failure_summary || (payload.passed === true ? "所有检查已完成" : "检查未通过"));
  }
  if (type === "file.changed") {
    const paths = changedPaths(event);
    if (!paths.length) return "已产生文件变更";
    const visible = paths.slice(0, 3).join("、");
    return `修改文件：${visible}${paths.length > 3 ? ` 等 ${paths.length} 个文件` : ""}`;
  }
  if (type === "permission.denied") {
    return String(payload.reason || "该操作被当前权限策略阻止");
  }
  if (typeof payload.message === "string") return payload.message;
  if (typeof payload.failure_summary === "string") return payload.failure_summary;
  return "";
}

function hasTechnicalDetails(event: EventRecord): boolean {
  return Object.keys(payloadOf(event)).length > 0;
}

function technicalDetails(event: EventRecord): string {
  return JSON.stringify(payloadOf(event), null, 2);
}

function mcpSummary(group: McpGroup): string {
  return Object.entries(group.counts)
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([tool, count]) => `${toolLabels[tool] || "知识工具"} ${count} 次`)
    .join("、");
}

function stageLabel(stage: string): string {
  return stageLabels[stage] || "其他阶段";
}

function mcpTechnicalDetails(group: McpGroup): string {
  return JSON.stringify(
    {
      stage: group.stage,
      first_seq: group.firstSeq,
      last_seq: group.lastSeq,
      event_count: group.events.length,
      events: group.events.map((event) => ({
        seq: event.seq,
        timestamp: event.timestamp,
        payload: payloadOf(event),
      })),
    },
    null,
    2,
  );
}

function rawType(event: EventRecord): string {
  return eventType(event);
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
    <div v-if="store.eventIntegrity.value?.status === 'invalid'" class="callout danger-callout" role="alert">
      <strong>事件时间线不可信，已停止展示</strong>
      <p>磁盘中的事件序号或 JSONL 结构不连续；系统不会用 seq 去重来掩盖损坏记录</p>
    </div>
    <template v-else>
      <div v-if="visibleEvents.length || mcpGroups.length" class="event-list">
        <article v-for="event in visibleEvents.slice().reverse()" :key="event.seq" class="event-row">
          <span class="event-marker" />
          <div class="event-copy">
            <strong>{{ title(event) }}</strong>
            <p v-if="summary(event)" class="event-summary">{{ summary(event) }}</p>
            <details v-if="hasTechnicalDetails(event)" class="event-technical">
              <summary>技术详情</summary>
              <pre>{{ technicalDetails(event) }}</pre>
            </details>
          </div>
          <time>{{ formatTime(event.timestamp) }}</time>
        </article>
        <article v-for="group in mcpGroups" :key="`mcp-${group.stage}-${group.firstSeq}`" class="event-row mcp-summary-row">
          <span class="event-marker" />
          <div class="event-copy">
            <strong>MCP 知识调用 · {{ stageLabel(group.stage) }}</strong>
            <p class="event-summary">已合并 {{ group.events.length }} 条调用：{{ mcpSummary(group) }}</p>
            <details class="event-technical">
              <summary>查看原始 MCP 调用</summary>
              <pre>{{ mcpTechnicalDetails(group) }}</pre>
            </details>
          </div>
          <time>{{ formatTime(group.events[group.events.length - 1].timestamp) }}</time>
        </article>
      </div>
      <div v-else class="empty-inline">正在处理，详细进度会在这里更新</div>
      <details v-if="hiddenEvents.length" class="timeline-debug">
        <summary>查看隐藏的内部记录（{{ hiddenEvents.length }} 条）</summary>
        <div class="timeline-debug-list">
          <article v-for="event in hiddenEvents.slice().reverse()" :key="event.seq">
            <div>
              <strong>{{ rawType(event) }}</strong>
              <time>{{ formatTime(event.timestamp) }}</time>
            </div>
            <pre>{{ technicalDetails(event) }}</pre>
          </article>
        </div>
      </details>
    </template>
  </section>
</template>
