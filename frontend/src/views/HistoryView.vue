<script setup lang="ts">
import { onMounted, ref } from "vue";
import { useRouter } from "vue-router";

import { getHistory } from "../api/platform";
import { useOrchestrator } from "../composables/useOrchestrator";
import type { HistoryItemData, HistoryPageData, RunKind } from "../types/task";

const store = useOrchestrator();
const router = useRouter();
const result = ref<HistoryPageData>({ items: [], page: 1, page_size: 20, total: 0, pages: 0 });
const query = ref("");
const kind = ref<RunKind | "">("");
const status = ref("");
const projectId = ref("");
const loading = ref(false);
const error = ref("");

async function load(page = 1): Promise<void> {
  loading.value = true;
  error.value = "";
  try {
    result.value = await getHistory({ project_id: projectId.value, query: query.value, kind: kind.value, status: status.value, page, page_size: 20 });
  } catch (caught) {
    error.value = caught instanceof Error ? caught.message : "历史记录读取失败。";
  } finally {
    loading.value = false;
  }
}

async function open(item: HistoryItemData): Promise<void> {
  await store.selectProject(item.project_id, false);
  await store.activateRun(item.kind, item.identifier);
  await router.push("/monitor");
}

function formatTime(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN");
}

onMounted(() => void load());
</script>

<template>
  <div class="view-stack history-view">
    <header class="view-header"><div><span class="section-kicker">跨项目运行档案</span><h1>历史</h1><p>搜索任务与长队列，切换项目后继续查看完整记录。</p></div></header>
    <section class="surface history-surface">
      <form class="filter-bar" @submit.prevent="load(1)">
        <label class="search-field"><span>⌕</span><input v-model="query" placeholder="搜索标题或任务编号" /></label>
        <select v-model="projectId"><option value="">全部项目</option><option v-for="project in store.projects.value" :key="project.project_id" :value="project.project_id">{{ project.name }}</option></select>
        <select v-model="kind"><option value="">全部类型</option><option value="task">单任务</option><option value="queue">长任务</option></select>
        <select v-model="status"><option value="">全部状态</option><option value="running">执行中</option><option value="paused">已暂停</option><option value="success">验证通过</option><option value="waiting_review">等待审核</option><option value="completed">已完成</option><option value="cancelled">已取消</option><option value="infrastructure_error">环境故障</option></select>
        <button class="primary-button" type="submit">筛选</button>
      </form>
      <div class="history-meta"><span>共 {{ result.total }} 条记录</span><span v-if="loading">正在更新…</span></div>
      <p v-if="error" class="page-error" role="alert">{{ error }}</p>
      <div v-if="result.items.length" class="history-table">
        <button v-for="item in result.items" :key="`${item.project_id}-${item.kind}-${item.identifier}`" type="button" @click="open(item)">
          <span class="kind-icon" :class="`kind-${item.kind}`">{{ item.kind === 'queue' ? 'Q' : 'T' }}</span>
          <span class="history-title"><strong>{{ item.title }}</strong><small>{{ item.project_name }} · {{ item.identifier }}</small></span>
          <span class="status-chip" :class="`status-${item.status}`">{{ item.status }}</span>
          <span class="delivery-history-state">{{ item.delivery_status || "该记录不具备" }}</span>
          <time>{{ formatTime(item.updated_at) }}</time>
          <span class="row-chevron">›</span>
        </button>
      </div>
      <div v-else-if="!loading" class="empty-inline">没有符合条件的运行记录。</div>
      <nav v-if="result.pages > 1" class="pagination">
        <button class="secondary-button" type="button" :disabled="result.page <= 1" @click="load(result.page - 1)">上一页</button>
        <span>{{ result.page }} / {{ result.pages }}</span>
        <button class="secondary-button" type="button" :disabled="result.page >= result.pages" @click="load(result.page + 1)">下一页</button>
      </nav>
    </section>
  </div>
</template>
