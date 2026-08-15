<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from "vue";

import type { TaskData } from "../types/task";

export type CheckpointKey = "context" | "evaluations" | "commit" | "archive";

const props = defineProps<{
  task: TaskData;
  checkpoint: CheckpointKey;
}>();

const emit = defineEmits<{
  close: [];
}>();

const closeButton = ref<HTMLButtonElement | null>(null);

const checkpointMeta: Record<CheckpointKey, { kicker: string; title: string }> = {
  context: { kicker: "冻结依据", title: "Context 快照详情" },
  evaluations: { kicker: "独立评估", title: "四层评估详情" },
  commit: { kicker: "交付检查点", title: "Commit 详情" },
  archive: { kicker: "交付检查点", title: "Archive 详情" },
};

const meta = computed(() => checkpointMeta[props.checkpoint]);

function record(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null
    ? value as Record<string, unknown>
    : {};
}

function text(value: unknown, fallback = "—"): string {
  if (value === null || value === undefined || value === "") return fallback;
  return String(value);
}

function shortHash(value: unknown): string {
  const valueText = text(value);
  return valueText.length > 16 ? `${valueText.slice(0, 12)}…` : valueText;
}

function formatTime(value: unknown): string {
  if (!value) return "—";
  const date = new Date(String(value));
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString("zh-CN");
}

const contextSnapshot = computed(() => {
  const evaluation = record(props.task.context.evaluation);
  return Object.keys(evaluation).length
    ? evaluation
    : record(props.task.context.generation);
});

const contextKnowledge = computed(() => {
  const value = contextSnapshot.value.knowledge;
  return Array.isArray(value) ? value.map(record) : [];
});

const evaluationAggregate = computed(() => record(props.task.evaluations.aggregate));
const evaluationLayers = computed(() =>
  ["syntax", "logic", "specification", "architecture"].map((name) => ({
    name,
    status: text(record(evaluationAggregate.value[name]).status, "not_evaluated"),
  })),
);
const evaluationWarnings = computed(() => {
  const value = evaluationAggregate.value.warnings;
  return Array.isArray(value) ? value.map(record) : [];
});
const blockingFindings = computed(() => {
  const value = evaluationAggregate.value.blocking_findings;
  return Array.isArray(value) ? value.map(record) : [];
});

const commit = computed(() => record(props.task.commit));
const review = computed(() => record(props.task.review));
const workspace = computed(() => record(props.task.workspace));
const archiveSummary = computed(() => record(props.task.archive.summary));
const archiveOutbox = computed(() => record(props.task.archive.outbox));

function close(): void {
  emit("close");
}

function onKeydown(event: KeyboardEvent): void {
  if (event.key === "Escape") close();
}

onMounted(() => {
  document.addEventListener("keydown", onKeydown);
  void nextTick(() => closeButton.value?.focus());
});

onBeforeUnmount(() => document.removeEventListener("keydown", onKeydown));
</script>

<template>
  <div class="checkpoint-detail-backdrop" data-test="checkpoint-detail-backdrop" @click.self="close">
    <aside
      class="checkpoint-detail-drawer"
      role="dialog"
      aria-modal="true"
      aria-labelledby="checkpoint-detail-title"
    >
      <header class="checkpoint-detail-header">
        <div>
          <span class="section-kicker">{{ meta.kicker }}</span>
          <h2 id="checkpoint-detail-title">{{ meta.title }}</h2>
        </div>
        <button ref="closeButton" class="drawer-close-button" type="button" aria-label="关闭检查点详情" @click="close">×</button>
      </header>

      <div class="checkpoint-detail-body">
        <template v-if="checkpoint === 'context'">
          <section class="checkpoint-detail-section">
            <div class="checkpoint-detail-summary">
              <div><span>状态</span><strong>{{ Object.keys(contextSnapshot).length ? "已冻结" : "该记录不具备" }}</strong></div>
              <div><span>快照哈希</span><code>{{ shortHash(contextSnapshot.context_sha256 || contextSnapshot.snapshot_sha256) }}</code></div>
              <div><span>阶段</span><strong>{{ text(contextSnapshot.stage) }}</strong></div>
            </div>
          </section>
          <section class="checkpoint-detail-section">
            <h3>冻结依据</h3>
            <div v-if="contextKnowledge.length" class="checkpoint-detail-list">
              <article v-for="(item, index) in contextKnowledge" :key="`${item.knowledge_id || index}`">
                <strong>{{ text(item.title || item.knowledge_id, "未命名知识") }}</strong>
                <p>{{ text(item.type, "knowledge") }} · {{ text(item.maturity, "—") }} · revision {{ text(item.revision, "—") }}</p>
                <small>{{ text(item.path, "未提供来源路径") }}</small>
              </article>
            </div>
            <p v-else class="checkpoint-detail-empty">没有注入适用知识</p>
          </section>
        </template>

        <template v-else-if="checkpoint === 'evaluations'">
          <section class="checkpoint-detail-section">
            <div class="checkpoint-detail-summary">
              <div><span>产物状态</span><strong>{{ Object.keys(evaluationAggregate).length ? "结果已持久化" : "尚未产出" }}</strong></div>
              <div><span>评估依据</span><code>{{ shortHash(evaluationAggregate.context_sha256) }}</code></div>
            </div>
          </section>
          <section class="checkpoint-detail-section">
            <h3>四层结果</h3>
            <div class="evaluation-layer-grid checkpoint-evaluation-grid">
              <div v-for="layer in evaluationLayers" :key="layer.name">
                <span>{{ layer.name }}</span>
                <strong :class="`evaluation-${layer.status}`">{{ layer.status }}</strong>
              </div>
            </div>
          </section>
          <section v-if="blockingFindings.length" class="checkpoint-detail-section">
            <h3>阻断问题</h3>
            <div class="checkpoint-detail-list warning-list">
              <article v-for="(finding, index) in blockingFindings" :key="index">
                <strong>{{ text(finding.layer || finding.finding_id, "需要处理") }}</strong>
                <p>{{ text(finding.message || finding.rationale, "未提供问题说明") }}</p>
              </article>
            </div>
          </section>
          <section v-if="evaluationWarnings.length" class="checkpoint-detail-section">
            <h3>提醒</h3>
            <div class="checkpoint-detail-list">
              <article v-for="(warning, index) in evaluationWarnings" :key="index">
                <strong>{{ text(warning.layer, "warning") }}</strong>
                <p>{{ text(warning.message || warning.rationale || warning.knowledge_id, "需要人工复核") }}</p>
              </article>
            </div>
          </section>
        </template>

        <template v-else-if="checkpoint === 'commit'">
          <section class="checkpoint-detail-section">
            <div class="checkpoint-detail-summary">
              <div><span>审核</span><strong>{{ text(props.task.review_status) }}</strong></div>
              <div><span>交付状态</span><strong>{{ text(props.task.delivery_status) }}</strong></div>
              <div><span>变更文件</span><strong>{{ props.task.changed_files.length }} 个</strong></div>
            </div>
          </section>
          <section class="checkpoint-detail-section">
            <h3>Commit 与 Diff</h3>
            <dl class="checkpoint-detail-fields">
              <div><dt>Commit SHA</dt><dd>{{ text(commit.commit_sha) }}</dd></div>
              <div><dt>当前 Diff SHA</dt><dd>{{ shortHash(props.task.final_diff_sha256) }}</dd></div>
              <div><dt>任务分支</dt><dd>{{ text(workspace.task_branch) }}</dd></div>
              <div><dt>基线 Commit</dt><dd>{{ shortHash(workspace.base_commit) }}</dd></div>
              <div><dt>审核人</dt><dd>{{ text(review.reviewer) }}</dd></div>
              <div><dt>审核时间</dt><dd>{{ formatTime(review.reviewed_at || review.created_at) }}</dd></div>
            </dl>
            <p v-if="review.comment" class="checkpoint-detail-note">审核意见：{{ text(review.comment) }}</p>
          </section>
        </template>

        <template v-else>
          <section class="checkpoint-detail-section">
            <div class="checkpoint-detail-summary">
              <div><span>交付状态</span><strong>{{ text(props.task.delivery_status) }}</strong></div>
              <div><span>归档摘要</span><strong>{{ Object.keys(archiveSummary).length ? "已生成" : "尚未生成" }}</strong></div>
              <div><span>Outbox</span><strong>{{ text(archiveOutbox.status, "无") }}</strong></div>
            </div>
          </section>
          <section class="checkpoint-detail-section">
            <h3>归档记录</h3>
            <dl class="checkpoint-detail-fields">
              <div><dt>归档状态</dt><dd>{{ text(archiveSummary.delivery_status || archiveSummary.status) }}</dd></div>
              <div><dt>任务编号</dt><dd>{{ props.task.task_id }}</dd></div>
              <div><dt>归档时间</dt><dd>{{ formatTime(archiveSummary.created_at || archiveSummary.archived_at) }}</dd></div>
              <div><dt>知识写入</dt><dd>{{ text(archiveSummary.knowledge_status || archiveSummary.knowledge_write_status) }}</dd></div>
              <div><dt>Outbox 重试</dt><dd>{{ text(archiveOutbox.attempts, "0") }} 次</dd></div>
              <div><dt>最后错误</dt><dd>{{ text(archiveSummary.error || archiveOutbox.last_error, "—") }}</dd></div>
            </dl>
          </section>
        </template>
      </div>
    </aside>
  </div>
</template>
