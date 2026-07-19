<script setup lang="ts">
import { computed } from "vue";
import { useRouter } from "vue-router";

import CopyButton from "../components/CopyButton.vue";
import ReviewPanel from "../components/ReviewPanel.vue";
import { useOrchestrator } from "../composables/useOrchestrator";
import type { ReviewDecision } from "../types/task";

const store = useOrchestrator();
const router = useRouter();
const reviewable = computed(() =>
  Boolean(store.task.value) &&
  !store.task.value?.legacy &&
  ["success", "manual_review"].includes(store.task.value?.status || ""),
);
const visibleReport = computed(() =>
  store.queue.value && ["completed", "rejected"].includes(store.queue.value.status)
    ? store.queueReport.value
    : store.report.value,
);
const deliverySha = computed(() =>
  store.queue.value && ["completed", "rejected"].includes(store.queue.value.status)
    ? store.queue.value.cumulative_diff_sha256
    : store.task.value?.final_diff_sha256 || "",
);
const aggregate = computed<Record<string, unknown>>(() => {
  const value = store.task.value?.evaluations.aggregate;
  return typeof value === "object" && value !== null
    ? value as Record<string, unknown>
    : {};
});
const evaluationLayers = computed(() =>
  ["syntax", "logic", "specification", "architecture"].map((name) => {
    const value = aggregate.value[name];
    const record = typeof value === "object" && value !== null
      ? value as Record<string, unknown>
      : {};
    return { name, status: String(record.status || "not_evaluated") };
  }),
);
const evaluationWarnings = computed<Record<string, unknown>[]>(() =>
  Array.isArray(aggregate.value.warnings)
    ? aggregate.value.warnings as Record<string, unknown>[]
    : [],
);
const contextSnapshot = computed<Record<string, unknown>>(() => {
  const value = store.task.value?.context.evaluation || store.task.value?.context.generation;
  return typeof value === "object" && value !== null
    ? value as Record<string, unknown>
    : {};
});
const knowledgeItems = computed<Record<string, unknown>[]>(() =>
  Array.isArray(contextSnapshot.value.knowledge)
    ? contextSnapshot.value.knowledge as Record<string, unknown>[]
    : [],
);

async function submit(payload: { decision: ReviewDecision; reviewer: string; comment: string; commit_subject: string }): Promise<void> {
  const queued = Boolean(store.task.value?.queue_id);
  if (await store.submitReview(payload) && queued) await router.push("/monitor");
}
</script>

<template>
  <div class="view-stack review-view">
    <header class="view-header">
      <div><span class="section-kicker">最终人工关口</span><h1>审核</h1><p>批准会绑定当前可见 Diff 和 commit subject，在任务分支创建一次 commit；不会 merge 或 push。</p></div>
      <span class="safety-statement"><i>✓</i> 本地不可变记录</span>
    </header>

    <div v-if="!store.task.value" class="surface empty-state large-empty">
      <h2>没有可审核的任务</h2><p>先从历史记录打开任务，或等待当前运行产出机器结果。</p>
      <RouterLink class="secondary-button link-button" to="/history">查看历史</RouterLink>
    </div>
    <div v-else-if="store.task.value.legacy" class="surface empty-state large-empty">
      <h2>这是一条旧版记录</h2><p>{{ store.task.value.history_warning || "历史数据缺少 Diff 指纹与审核结构，不能补造审核结论。" }}</p>
    </div>
    <div v-else-if="!reviewable" class="surface empty-state large-empty">
      <h2>机器流程尚未到达审核点</h2><p>当前状态为 {{ store.task.value.status }}，可以返回监控页继续观察。</p>
      <RouterLink class="secondary-button link-button" to="/monitor">返回监控</RouterLink>
    </div>

    <div v-else class="review-layout">
      <section class="surface delivery-report">
        <div class="surface-heading">
          <div><span class="section-kicker">本次交付</span><h2>汇总报告</h2></div>
          <RouterLink class="secondary-button link-button" to="/changes">查看代码变更 →</RouterLink>
        </div>
        <div class="delivery-summary">
          <div><span>任务</span><strong>{{ store.task.value.requirement }}</strong></div>
          <div><span>机器状态</span><strong>{{ store.task.value.status }}</strong></div>
          <div><span>变更文件</span><strong>{{ store.task.value.changed_files.length }}</strong></div>
          <div><span>验证轮次</span><strong>{{ store.task.value.rounds.length }}</strong></div>
        </div>
        <div class="delivery-hash"><span>{{ store.queue.value && ['completed', 'rejected'].includes(store.queue.value.status) ? '最终累计 Diff' : '当前 Diff' }}</span><div><code>{{ deliverySha || '—' }}</code><CopyButton v-if="deliverySha" :value="deliverySha" label="Diff 哈希" /></div></div>
        <section class="evaluation-block" data-test="evaluation-summary">
          <div class="subsection-heading"><div><span class="section-kicker">独立评估</span><h3>四层结果</h3></div><code v-if="aggregate.context_sha256">{{ String(aggregate.context_sha256).slice(0, 12) }}</code></div>
          <div class="evaluation-layer-grid">
            <div v-for="layer in evaluationLayers" :key="layer.name">
              <span>{{ layer.name }}</span>
              <strong :class="`evaluation-${layer.status}`">{{ layer.status }}</strong>
            </div>
          </div>
          <div v-if="evaluationWarnings.length" class="evaluation-warning-list">
            <article v-for="(warning, index) in evaluationWarnings" :key="index">
              <strong>{{ warning.layer || "warning" }}</strong>
              <p>{{ warning.message || warning.rationale || warning.knowledge_id || "需要人工复核" }}</p>
            </article>
          </div>
          <p v-else-if="!Object.keys(aggregate).length" class="capability-empty">该历史记录不具备分层评估产物。</p>
        </section>
        <section class="knowledge-block" data-test="knowledge-summary">
          <div class="subsection-heading"><div><span class="section-kicker">冻结依据</span><h3>知识与成熟度</h3></div><span>{{ knowledgeItems.length }} 条</span></div>
          <div v-if="knowledgeItems.length" class="knowledge-list">
            <article v-for="item in knowledgeItems" :key="`${item.knowledge_id}-${item.revision}`">
              <div><strong>{{ item.title || item.knowledge_id }}</strong><span>{{ item.constraint_strength || "context" }}</span></div>
              <p>{{ item.type }} · {{ item.maturity }} · revision {{ item.revision }} · {{ item.path }}</p>
            </article>
          </div>
          <p v-else class="capability-empty">没有注入适用知识；无依据的架构层应显示 not_evaluated。</p>
        </section>
        <pre class="review-report-content">{{ visibleReport || "汇总报告尚未生成。" }}</pre>
      </section>
      <ReviewPanel
        :task="store.task.value"
        :submitting="store.reviewing.value || store.controlling.value"
        @review="submit"
        @retry-commit="store.retryDelivery('commit')"
        @retry-archive="store.retryDelivery('archive')"
      />
    </div>
  </div>
</template>
