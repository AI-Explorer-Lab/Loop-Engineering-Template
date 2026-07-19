<script setup lang="ts">
import { computed, ref } from "vue";

import { useOrchestrator } from "../composables/useOrchestrator";

const store = useOrchestrator();
const saving = ref(false);
const browserPermission = ref(
  typeof Notification === "undefined" ? "unsupported" : Notification.permission,
);
const settings = computed(() => store.notificationSettings.value);
const capability = computed(() => store.capabilities.value);
const metrics = computed(() => store.metrics.value);
const readMode = computed(() => {
  const value = capability.value?.mcp_read;
  return typeof value === "object" && value !== null
    ? value as Record<string, unknown>
    : {};
});

function percent(value: number | null | undefined): string {
  return typeof value === "number" ? `${Math.round(value * 100)}%` : "暂无样本";
}

function formatTime(value: string | undefined): string {
  if (!value) return "尚未检查";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN");
}

async function save(inApp: boolean, browser: boolean): Promise<void> {
  saving.value = true;
  await store.updateNotificationPreferences(inApp, browser);
  saving.value = false;
}

async function toggleBrowser(): Promise<void> {
  if (settings.value.browser && browserPermission.value === "granted") {
    await save(settings.value.in_app, false);
    return;
  }
  if (typeof Notification === "undefined") return;
  browserPermission.value = await Notification.requestPermission();
  if (browserPermission.value === "granted") {
    await save(settings.value.in_app, true);
  }
}
</script>

<template>
  <div class="view-stack settings-view">
    <header class="view-header"><div><span class="section-kicker">本地工作台偏好</span><h1>设置</h1><p>选择需要的提醒入口；外部投递由服务端配置，不影响任务结果。</p></div></header>
    <div class="settings-grid">
      <section class="surface setting-card harness-setting-card" data-test="harness-settings">
        <span class="setting-icon">H</span>
        <div>
          <h2>Harness 本地能力</h2>
          <p>Knowledge、Skills 和 MCP 由控制面读取；Generator 不直接获得外部目录权限。</p>
          <dl class="capability-list">
            <div><dt>Knowledge-Base</dt><dd><code>{{ capability?.knowledge_base_path || "不可用" }}</code></dd></div>
            <div><dt>MCP registry</dt><dd><code>{{ capability?.mcp_registry || "不可用" }}</code></dd></div>
            <div><dt>运行模式</dt><dd>{{ readMode.transport || "—" }} · network {{ readMode.network || "—" }}</dd></div>
            <div><dt>Skills</dt><dd>{{ capability?.skill_count ?? "—" }} 个纯文本指南</dd></div>
            <div><dt>消费 actor</dt><dd>{{ capability?.knowledge_actor_id || store.activeProject.value?.knowledge_actor_id || "未配置" }}</dd></div>
            <div><dt>最近检查</dt><dd>{{ formatTime(capability?.checked_at) }}</dd></div>
          </dl>
        </div>
        <span class="configuration-state" :class="{ configured: capability?.status === 'healthy' }">{{ capability?.status || "不可用" }}</span>
      </section>
      <section class="surface setting-card harness-setting-card" data-test="harness-metrics">
        <span class="setting-icon">Σ</span>
        <div>
          <h2>文件化观测指标</h2>
          <p>由持久化运行产物按需计算，不引入新的数据库。</p>
          <dl class="capability-list compact-capability-list">
            <div><dt>任务成功率</dt><dd>{{ percent(metrics?.task_success_rate) }}</dd></div>
            <div><dt>知识命中率</dt><dd>{{ percent(metrics?.knowledge_hit_rate) }}</dd></div>
            <div><dt>Commit 成功率</dt><dd>{{ percent(metrics?.commit_success_rate) }}</dd></div>
            <div><dt>Planner 确认任务</dt><dd>{{ metrics?.planned_tasks ?? 0 }}</dd></div>
            <div><dt>修复轮数</dt><dd>{{ metrics?.repair_rounds ?? 0 }}</dd></div>
            <div><dt>归档积压</dt><dd>{{ metrics?.archive_backlog ?? capability?.archive_backlog ?? 0 }}</dd></div>
          </dl>
          <small v-if="store.harnessError.value">{{ store.harnessError.value }}</small>
        </div>
        <button class="secondary-button" type="button" @click="store.refreshHarnessStatus">刷新</button>
      </section>
      <section class="surface setting-card"><span class="setting-icon">●</span><div><h2>站内通知</h2><p>审核、完成、失败与取消事件会进入右上角通知中心。</p></div><button class="switch" :class="{ 'is-on': settings.in_app }" type="button" role="switch" :aria-checked="settings.in_app" :disabled="saving" @click="save(!settings.in_app, settings.browser)"><i /></button></section>
      <section class="surface setting-card"><span class="setting-icon">◫</span><div><h2>浏览器通知</h2><p>授权后，新事件会通过系统通知提示；首次读取不会补发旧提醒。</p><small v-if="browserPermission === 'denied'">浏览器已拒绝授权，请在浏览器设置中重新开启。</small></div><button class="switch" :class="{ 'is-on': settings.browser && browserPermission === 'granted' }" type="button" role="switch" :aria-checked="settings.browser && browserPermission === 'granted'" :disabled="saving || browserPermission === 'unsupported' || browserPermission === 'denied'" @click="toggleBrowser"><i /></button></section>
      <section class="surface setting-card"><span class="setting-icon">@</span><div><h2>邮件投递</h2><p>SMTP 投递失败只记录在通知中，不会改变任务状态。</p><code>backend/config/app.yaml</code></div><span class="configuration-state" :class="{ configured: settings.email_configured }">{{ settings.email_configured ? "已配置" : "未配置" }}</span></section>
      <section class="surface setting-card"><span class="setting-icon">↗</span><div><h2>Webhook</h2><p>向内部自动化入口发送结构化通知，超时或失败不会阻塞执行。</p><code>notifications.webhook_url</code></div><span class="configuration-state" :class="{ configured: settings.webhook_configured }">{{ settings.webhook_configured ? "已配置" : "未配置" }}</span></section>
    </div>
  </div>
</template>
