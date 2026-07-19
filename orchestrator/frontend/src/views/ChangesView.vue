<script setup lang="ts">
import { computed, ref } from "vue";

import CopyButton from "../components/CopyButton.vue";
import DiffViewer from "../components/DiffViewer.vue";
import { useOrchestrator } from "../composables/useOrchestrator";

const store = useOrchestrator();
const tab = ref<"diff" | "responses" | "report">("diff");
const showCumulative = computed(() =>
  Boolean(store.queue.value) && ["completed", "rejected"].includes(store.queue.value?.status || ""),
);
const visibleDiff = computed(() =>
  showCumulative.value ? store.queueDiff.value : store.diff.value,
);
const visibleReport = computed(() =>
  showCumulative.value ? store.queueReport.value : store.report.value,
);
const visibleSha = computed(() =>
  showCumulative.value
    ? store.queue.value?.cumulative_diff_sha256 || ""
    : store.task.value?.final_diff_sha256 || "",
);

function display(value: unknown): string {
  return value === null || value === undefined || value === "" ? "—" : String(value);
}
</script>

<template>
  <div class="view-stack changes-view">
    <header class="view-header action-header">
      <div><span class="section-kicker">代码检查台</span><h1>变更</h1><p>逐文件检查 Diff、Codex 可见回复与机器报告。</p></div>
      <div v-if="store.task.value" class="change-summary">
        <span><strong>{{ store.task.value.changed_files.length }}</strong> 个文件</span>
        <span><strong>{{ store.task.value.codex_responses.length }}</strong> 次回复</span>
      </div>
    </header>

    <div v-if="!store.hasRun.value" class="surface empty-state large-empty">
      <h2>尚未选择运行记录</h2><p>从监控或历史页打开一次任务后再检查变更。</p>
      <RouterLink class="secondary-button link-button" to="/history">打开历史记录</RouterLink>
    </div>

    <template v-else>
      <section v-if="store.task.value?.changed_files.length" class="surface file-summary-surface">
        <div class="surface-heading compact-heading"><div><span class="section-kicker">文件摘要</span><h2>本次影响范围</h2></div></div>
        <div class="file-summary-grid">
          <article v-for="file in store.task.value.changed_files" :key="String(file.path)">
            <span class="file-status">{{ display(file.status).slice(0, 1).toUpperCase() }}</span>
            <code>{{ display(file.path) }}</code>
            <small>+{{ display(file.additions) }} / -{{ display(file.deletions) }}</small>
          </article>
        </div>
      </section>

      <section class="surface changes-surface">
        <div class="content-tabs" role="tablist">
          <button type="button" role="tab" :aria-selected="tab === 'diff'" :class="{ active: tab === 'diff' }" @click="tab = 'diff'">代码 Diff</button>
          <button type="button" role="tab" :aria-selected="tab === 'responses'" :class="{ active: tab === 'responses' }" @click="tab = 'responses'">Codex 回复</button>
          <button type="button" role="tab" :aria-selected="tab === 'report'" :class="{ active: tab === 'report' }" @click="tab = 'report'">运行报告</button>
          <span v-if="visibleSha" class="tab-hash">{{ showCumulative ? '累计 ' : '' }}SHA {{ visibleSha.slice(0, 12) }} · 敏感信息替换 {{ store.task.value?.diff_redaction_count || 0 }} 处 <CopyButton :value="visibleSha" label="Diff 哈希" /></span>
        </div>
        <div v-if="store.task.value?.diff_redaction_count" class="sensitive-warning">
          <strong>当前 Diff 含已替换的疑似敏感信息</strong>
          <span>审核操作保持禁用；请进入人工审查页查看约束说明。</span>
          <RouterLink to="/review">查看原因 →</RouterLink>
        </div>
        <DiffViewer v-if="tab === 'diff'" :diff="visibleDiff" />
        <div v-else-if="tab === 'responses'" class="response-list">
          <article v-for="response in store.task.value?.codex_responses || []" :key="response.turn_number">
            <header><strong>第 {{ response.turn_number }} 轮</strong><span>Codex</span></header>
            <pre>{{ response.response }}</pre>
          </article>
          <div v-if="!store.task.value?.codex_responses.length" class="empty-inline">没有持久化的可见回复。</div>
        </div>
        <pre v-else class="report-content">{{ visibleReport || "报告尚未生成。" }}</pre>
      </section>

      <nav class="next-step-bar" aria-label="后续步骤">
        <div><span>检查完成后</span><strong>把当前 Diff 绑定到人工审核结论</strong></div>
        <RouterLink class="primary-button link-button" to="/review">进入审核 →</RouterLink>
      </nav>
    </template>
  </div>
</template>
