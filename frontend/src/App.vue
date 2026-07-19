<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from "vue";
import { useRoute, useRouter } from "vue-router";

import { provideOrchestrator } from "./composables/useOrchestrator";
import type { NotificationData } from "./types/task";

const store = provideOrchestrator();
const route = useRoute();
const router = useRouter();
const notificationsOpen = ref(false);
let notificationTimer: ReturnType<typeof setInterval> | null = null;

const pageTitle = computed(() => String(route.meta.title || "工作台"));
const runCaption = computed(() => {
  if (!store.hasRun.value) return "没有选中的运行";
  return `${store.currentKind.value === "queue" ? "长任务" : "单任务"} · ${store.identifier.value}`;
});
const statusLabels: Record<string, string> = {
  accepted: "已接收",
  pending: "等待启动",
  running: "执行中",
  pausing: "正在暂停",
  paused: "已暂停",
  cancelling: "正在取消",
  cancelled: "已取消",
  success: "验证通过",
  manual_review: "需人工处理",
  waiting_review: "等待审查",
  completed: "已完成",
  rejected: "已驳回",
  infrastructure_error: "环境故障",
};
const statusLabel = computed(() =>
  store.runStatus.value ? statusLabels[store.runStatus.value] || store.runStatus.value : "无运行",
);

async function changeProject(event: Event): Promise<void> {
  await store.selectProject((event.target as HTMLSelectElement).value);
}

async function openNotification(notification: NotificationData): Promise<void> {
  await store.readNotification(notification);
  notificationsOpen.value = false;
  await router.push(notification.category === "waiting_review" ? "/review" : "/monitor");
}

function formatTime(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN");
}

onMounted(async () => {
  await store.initialize();
  notificationTimer = setInterval(() => void store.refreshNotifications(), 15_000);
});

onUnmounted(() => {
  if (notificationTimer !== null) clearInterval(notificationTimer);
  store.dispose();
});
</script>

<template>
  <div class="workbench-shell" data-test="app-shell">
    <svg class="icon-library" aria-hidden="true">
      <symbol id="icon-create" viewBox="0 0 24 24"><path d="M12 5v14M5 12h14" /></symbol>
      <symbol id="icon-monitor" viewBox="0 0 24 24"><path d="M4 12h3l2-5 4 10 2-5h5" /></symbol>
      <symbol id="icon-changes" viewBox="0 0 24 24"><path d="M8 5 4 9l4 4M16 11l4 4-4 4M14 4l-4 16" /></symbol>
      <symbol id="icon-review" viewBox="0 0 24 24"><path d="M5 12l4 4L19 6" /></symbol>
      <symbol id="icon-history" viewBox="0 0 24 24"><path d="M4 6v5h5M5 11a8 8 0 1 0 2-5M12 8v5l3 2" /></symbol>
      <symbol id="icon-projects" viewBox="0 0 24 24"><path d="M4 7h6l2 2h8v10H4z" /></symbol>
      <symbol id="icon-settings" viewBox="0 0 24 24"><path d="M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8M12 3v2M12 19v2M3 12h2M19 12h2M5.6 5.6 7 7M17 17l1.4 1.4M18.4 5.6 17 7M7 17l-1.4 1.4" /></symbol>
      <symbol id="icon-bell" viewBox="0 0 24 24"><path d="M6 17h12l-1.5-2V10a4.5 4.5 0 0 0-9 0v5zM10 20h4" /></symbol>
    </svg>

    <aside class="sidebar">
      <RouterLink class="brand" to="/create" aria-label="Codex Orchestrator 首页">
        <span class="brand-mark"><i /><i /><i /></span>
        <span><strong>Orchestrator</strong><small>Codex Workbench</small></span>
      </RouterLink>

      <nav class="primary-nav" aria-label="工作流程">
        <span class="nav-label">工作流程</span>
        <RouterLink to="/create"><svg><use href="#icon-create" /></svg><span>新建任务</span></RouterLink>
        <RouterLink to="/monitor"><svg><use href="#icon-monitor" /></svg><span>执行监控</span><i v-if="store.isRunning.value" class="nav-live-dot" /></RouterLink>
        <RouterLink to="/changes"><svg><use href="#icon-changes" /></svg><span>代码变更</span></RouterLink>
        <RouterLink to="/review"><svg><use href="#icon-review" /></svg><span>人工审查</span><b v-if="store.needsReview.value" class="nav-badge">1</b></RouterLink>
      </nav>

      <nav class="secondary-nav" aria-label="工作台">
        <span class="nav-label">工作台</span>
        <RouterLink to="/history"><svg><use href="#icon-history" /></svg><span>历史</span></RouterLink>
        <RouterLink to="/projects"><svg><use href="#icon-projects" /></svg><span>项目</span></RouterLink>
        <RouterLink to="/settings"><svg><use href="#icon-settings" /></svg><span>设置</span></RouterLink>
      </nav>

      <div class="sidebar-runtime">
        <div class="runtime-head"><span>本地运行服务</span><i :class="{ active: !store.pageError.value }" /></div>
        <strong>{{ store.activeProject.value?.name || "正在连接" }}</strong>
        <small>{{ runCaption }}</small>
      </div>
    </aside>

    <div class="workspace">
      <header class="topbar">
        <div class="topbar-identity"><strong>Codex Orchestrator</strong><b>/</b><span>{{ pageTitle }}</span></div>
        <div class="topbar-run-context">
          <span>{{ store.hasRun.value ? store.currentTitle.value : "尚未创建任务" }}</span>
          <code v-if="store.identifier.value">{{ store.identifier.value }}</code>
          <i class="status-chip" :class="`status-${store.runStatus.value || 'empty'}`">{{ statusLabel }}</i>
        </div>
        <div class="topbar-actions">
          <button class="connection-button" type="button" :class="`connection-${store.connectionState.value}`" :title="store.healthError.value || `后端 ${store.health.value?.version || ''}`" @click="store.checkHealth()">
            <i />{{ store.connectionState.value === 'connected' ? '服务已连接' : store.connectionState.value === 'checking' ? '正在检查' : '服务未连接' }}
          </button>
          <label class="project-select-label">
            <span>项目</span>
            <select data-test="project-select" :value="store.activeProjectId.value" @change="changeProject">
              <option v-for="project in store.projects.value" :key="project.project_id" :value="project.project_id">{{ project.name }}</option>
            </select>
          </label>
          <button class="notification-button" type="button" aria-label="刷新当前任务" :disabled="!store.hasRun.value" @click="store.refreshCurrent()">↻</button>
          <button class="notification-button" type="button" aria-label="通知中心" :aria-expanded="notificationsOpen" @click="notificationsOpen = !notificationsOpen">
            <svg><use href="#icon-bell" /></svg><span v-if="store.unreadCount.value">{{ store.unreadCount.value > 9 ? '9+' : store.unreadCount.value }}</span>
          </button>
        </div>
      </header>

      <main class="workspace-content">
        <div v-if="store.pageError.value" class="global-error" role="alert"><strong>需要处理</strong><span>{{ store.pageError.value }}</span><button type="button" aria-label="关闭错误" @click="store.pageError.value = ''">×</button></div>
        <RouterView />
      </main>

      <aside v-if="notificationsOpen" class="notification-drawer">
        <header><div><span class="section-kicker">运行提醒</span><h2>通知中心</h2></div><button type="button" aria-label="关闭通知" @click="notificationsOpen = false">×</button></header>
        <div v-if="store.notificationSettings.value.in_app && store.notifications.value.length" class="notification-list">
          <button v-for="notification in store.notifications.value" :key="notification.notification_id" type="button" :class="{ unread: !notification.read_at }" @click="openNotification(notification)">
            <span class="notification-dot" /><span><strong>{{ notification.title }}</strong><small>{{ notification.message }}</small><time>{{ formatTime(notification.created_at) }}</time></span>
          </button>
        </div>
        <div v-else class="empty-inline">{{ store.notificationSettings.value.in_app ? "暂时没有运行提醒。" : "站内通知已关闭，可在设置中重新开启。" }}</div>
        <RouterLink class="drawer-settings-link" to="/settings" @click="notificationsOpen = false">通知设置 →</RouterLink>
      </aside>
      <div v-if="notificationsOpen" class="drawer-scrim" @click="notificationsOpen = false" />
    </div>
  </div>
</template>
