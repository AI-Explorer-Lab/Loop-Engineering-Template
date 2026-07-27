<script setup lang="ts">
import { computed, ref } from "vue";

import CopyButton from "../components/CopyButton.vue";
import { useOrchestrator } from "../composables/useOrchestrator";

const store = useOrchestrator();
const reviewer = ref("");
const confirmed = ref(false);
const commit = computed(() => String(store.task.value?.commit.commit_sha || ""));
const published = computed(() => store.task.value?.publish.status === "published");
const eligible = computed(() => Boolean(store.task.value) &&
  !store.task.value?.queue_id &&
  store.task.value?.review_status === "approved" &&
  store.task.value?.delivery_status === "archived" &&
  Boolean(commit.value));
const remoteUrl = computed(() => String(store.task.value?.publish.remote_url ||
  "已由项目发布策略固定，发布时由服务端再次核验"));

async function publish(): Promise<void> {
  if (!eligible.value || !confirmed.value || !reviewer.value.trim()) return;
  await store.publishCurrentTask(reviewer.value.trim());
}
</script>

<template>
  <div class="view-stack">
    <header class="view-header">
      <div><span class="section-kicker">外部交付</span><h1>发布交付</h1><p>只发布已人工批准、已 commit、已归档的单任务分支。不会创建 PR、合并或部署。</p></div>
      <span class="safety-statement"><i>✓</i> 显式确认</span>
    </header>

    <section v-if="!store.task.value" class="surface empty-state large-empty">
      <h2>没有选中的任务</h2><p>请先从历史记录打开一个已完成任务，再回到本页。</p>
      <RouterLink class="secondary-button link-button" to="/history">查看历史</RouterLink>
    </section>

    <section v-else class="surface delivery-report">
      <div class="surface-heading"><div><span class="section-kicker">发布前核验</span><h2>{{ store.task.value.requirement }}</h2></div><span :class="published ? 'status-chip status-completed' : 'status-chip status-running'">{{ published ? '已发布' : eligible ? '可发布' : '尚不可发布' }}</span></div>
      <div class="delivery-summary">
        <div><span>人工审核</span><strong>{{ store.task.value.review_status }}</strong></div>
        <div><span>本地交付</span><strong>{{ store.task.value.delivery_status }}</strong></div>
        <div><span>任务分支</span><strong>{{ String(store.task.value.workspace.task_branch || '—') }}</strong></div>
        <div><span>任务类型</span><strong>{{ store.task.value.queue_id ? '队列子任务（不可单独发布）' : '单任务' }}</strong></div>
      </div>
      <div class="delivery-hash"><span>待发布 Commit</span><div><code>{{ commit || '—' }}</code><CopyButton v-if="commit" :value="commit" label="Commit SHA" /></div></div>
      <div class="delivery-hash"><span>固定远端</span><code>{{ remoteUrl }}</code></div>

      <template v-if="published">
        <div class="global-success"><strong>已发布到 GitHub</strong><span>{{ String(store.task.value.publish.published_at || '') }} · {{ String(store.task.value.publish.branch || '') }}</span></div>
      </template>
      <template v-else-if="eligible">
        <div class="review-form">
          <label>发布确认人<input v-model="reviewer" maxlength="200" placeholder="填写你的姓名或标识" /></label>
          <label class="check-row"><input v-model="confirmed" type="checkbox" />我确认将上述 commit 推送到已配置的固定 GitHub 远端。</label>
          <button class="primary-button" type="button" :disabled="!confirmed || !reviewer.trim() || store.controlling.value" @click="publish">{{ store.controlling.value ? '正在发布…' : '推送到 GitHub' }}</button>
        </div>
      </template>
      <p v-else class="capability-empty">需要先满足：单任务、审核状态为 approved、交付状态为 archived，且提交证据仍可读取。服务端在实际推送前还会核对 worktree、分支、提交 SHA 与远端。</p>
    </section>
  </div>
</template>
