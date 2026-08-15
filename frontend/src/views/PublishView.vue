<script setup lang="ts">
import { computed, ref } from "vue";

import CopyButton from "../components/CopyButton.vue";
import { useOrchestrator } from "../composables/useOrchestrator";

const store = useOrchestrator();
const reviewer = ref("");
const confirmed = ref(false);
const commit = computed(() => String(store.task.value?.commit.commit_sha || ""));
const published = computed(() => store.task.value?.publish.status === "published");
const repositoryName = computed(() =>
  store.activeProject.value?.publish_repository_name
  || store.activeProject.value?.repo_root.split(/[\\/]/).filter(Boolean).pop()
  || "—",
);
const configuredRemote = computed(() =>
  String(store.activeProject.value?.publish_remote_url || "").trim(),
);
const autoCreateRemote = computed(() =>
  Boolean(store.activeProject.value?.publish_auto_create_remote),
);
const publishBranch = computed(() =>
  String(store.activeProject.value?.publish_branch || (autoCreateRemote.value ? "main" : "—")),
);
const publishConfigured = computed(() =>
  Boolean(
    (store.activeProject.value?.publish_enabled && configuredRemote.value)
    || autoCreateRemote.value,
  ),
);
const eligible = computed(() => Boolean(store.task.value) &&
  !store.task.value?.queue_id &&
  store.task.value?.review_status === "approved" &&
  store.task.value?.delivery_status === "archived" &&
  Boolean(commit.value));
const remoteUrl = computed(() => configuredRemote.value ||
  String(store.task.value?.publish.remote_url || "未配置固定远端"));
const actionDisabled = computed(() =>
  published.value
  || !eligible.value
  || !publishConfigured.value
  || store.controlling.value,
);
const actionLabel = computed(() => {
  if (published.value) return "已发布";
  if (store.controlling.value) return "正在发布…";
  if (!publishConfigured.value) return "先配置 GitHub 远端";
  if (!eligible.value) return "等待发布条件";
  return "推送到 GitHub";
});
const actionMessage = computed(() => {
  const task = store.task.value;
  if (!task) return "请先选择一个任务";
  if (published.value) return "该任务分支已经发布，重复点击不会再次推送";
  if (task.queue_id) return "队列子任务不能单独发布，请发布队列完成后的整体结果";
  if (task.review_status !== "approved") return "需要先完成人工审核并批准当前 Diff";
  if (task.delivery_status !== "archived") return "需要先完成 commit 和本地归档";
  if (!commit.value) return "缺少已确认的 commit 证据";
  if (!publishConfigured.value) {
    return "当前项目未启用 GitHub 发布，请先在项目配置中启用";
  }
  if (!configuredRemote.value && autoCreateRemote.value) {
    return `尚未配置固定 GitHub 远端；点击发布后将创建私有仓库“${repositoryName.value}”，并把已审核 commit 发布到 ${publishBranch.value}`;
  }
  return `点击后由机器把当前已审核 commit 推送到固定远端的 ${publishBranch.value} 分支；仓库名：${repositoryName.value}`;
});

async function publish(): Promise<void> {
  if (actionDisabled.value || !confirmed.value || !reviewer.value.trim()) return;
  await store.publishCurrentTask(reviewer.value.trim());
}
</script>

<template>
  <div class="view-stack">
    <header class="view-header">
      <div><span class="section-kicker">外部交付</span><h1>发布交付</h1><p>只发布已人工批准、已 commit、已归档的单任务结果。新项目首次发布到 main；不会创建 PR 或部署</p></div>
      <span class="safety-statement"><i>✓</i> 显式确认</span>
    </header>

    <section v-if="!store.task.value" class="surface empty-state large-empty">
      <h2>没有选中的任务</h2><p>请先从历史记录打开一个已完成任务，再回到本页</p>
      <RouterLink class="secondary-button link-button" to="/history">查看历史</RouterLink>
    </section>

    <section v-else class="surface delivery-report">
      <div class="surface-heading"><div><span class="section-kicker">发布前核验</span><h2>{{ store.task.value.requirement }}</h2></div><span :class="published ? 'status-chip status-completed' : 'status-chip status-running'">{{ published ? '已发布' : eligible ? '可发布' : '尚不可发布' }}</span></div>
      <div class="delivery-summary">
        <div><span>人工审核</span><strong>{{ store.task.value.review_status }}</strong></div>
        <div><span>本地交付</span><strong>{{ store.task.value.delivery_status }}</strong></div>
        <div><span>任务分支</span><strong>{{ String(store.task.value.workspace.task_branch || '—') }}</strong></div>
        <div><span>远端分支</span><strong>{{ publishBranch }}</strong></div>
        <div><span>任务类型</span><strong>{{ store.task.value.queue_id ? '队列子任务（不可单独发布）' : '单任务' }}</strong></div>
      </div>
      <div class="delivery-hash"><span>待发布 Commit</span><div><code>{{ commit || '—' }}</code><CopyButton v-if="commit" :value="commit" label="Commit SHA" /></div></div>
      <div class="delivery-hash"><span>GitHub 仓库名</span><code>{{ repositoryName }}</code></div>
      <div class="delivery-hash"><span>固定远端</span><code>{{ remoteUrl }}</code></div>

      <div v-if="published" class="global-success"><strong>已发布到 GitHub</strong><span>{{ String(store.task.value.publish.published_at || '') }} · {{ String(store.task.value.publish.branch || '') }}</span></div>
      <div class="review-form publish-action" data-test="publish-action">
        <label>发布确认人<input v-model="reviewer" maxlength="200" placeholder="填写你的姓名或标识" :disabled="published" /></label>
        <label class="check-row"><input v-model="confirmed" type="checkbox" :disabled="published" />我确认将上述 commit 推送到固定 GitHub 远端<span v-if="!configuredRemote && autoCreateRemote">（必要时自动创建私有仓库）</span></label>
        <button class="primary-button" type="button" :disabled="actionDisabled || !confirmed || !reviewer.trim()" @click="publish">{{ actionLabel }}</button>
        <p class="field-hint">{{ actionMessage }}</p>
      </div>
    </section>
  </div>
</template>
