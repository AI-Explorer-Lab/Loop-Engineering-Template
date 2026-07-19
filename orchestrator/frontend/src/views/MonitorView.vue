<script setup lang="ts">
import { computed, ref } from "vue";

import EventTimeline from "../components/EventTimeline.vue";
import QueueProgress from "../components/QueueProgress.vue";
import TaskStatus from "../components/TaskStatus.vue";
import ValidationRounds from "../components/ValidationRounds.vue";
import { useOrchestrator } from "../composables/useOrchestrator";

const store = useOrchestrator();
const confirmCancel = ref(false);

const canPause = computed(() => {
  const status = store.runStatus.value;
  return store.currentKind.value === "queue"
    ? ["pending", "running"].includes(status || "")
    : ["accepted", "running"].includes(status || "");
});
const canResume = computed(() => {
  const status = store.runStatus.value;
  return store.currentKind.value === "queue"
    ? ["paused", "infrastructure_error"].includes(status || "")
    : status === "paused";
});
const canRecoverRunningTask = computed(
  () => store.currentKind.value === "task" && store.runStatus.value === "running",
);
const canCancel = computed(() => {
  const status = store.runStatus.value;
  if (store.currentKind.value === "queue") {
    return ["pending", "running", "pausing", "paused", "waiting_review", "infrastructure_error"].includes(status || "");
  }
  return ["accepted", "running", "pausing", "paused"].includes(status || "");
});
const canRerun = computed(() => {
  const status = store.runStatus.value;
  return store.currentKind.value === "queue"
    ? ["completed", "cancelled", "rejected", "infrastructure_error"].includes(status || "")
    : ["success", "manual_review", "infrastructure_error", "cancelled"].includes(status || "");
});
const planCheckpoint = computed(() =>
  store.events.value.some((event) => event.type === "plan.confirmed")
    ? "人工已确认"
    : "直接创建 / 旧记录",
);
const harnessCheckpoints = computed(() => {
  const task = store.task.value;
  return [
    { label: "Planner", value: planCheckpoint.value },
    { label: "Context", value: task && Object.keys(task.context).length ? "快照已冻结" : "该记录不具备" },
    { label: "四层评估", value: task && Object.keys(task.evaluations).length ? "结果已持久化" : "尚未产出" },
    { label: "Commit", value: task?.commit.commit_sha ? String(task.commit.commit_sha).slice(0, 10) : task?.delivery_status || "not_ready" },
    { label: "Archive", value: task?.delivery_status === "archived" ? "已完成" : task?.archive.outbox ? "已有检查点" : "尚未开始" },
  ];
});

async function cancel(): Promise<void> {
  confirmCancel.value = false;
  await store.runControl("cancel");
}
</script>

<template>
  <div class="view-stack monitor-view">
    <header class="view-header action-header">
      <div>
        <span class="section-kicker">执行控制台</span>
        <h1>运行监控</h1>
        <p v-if="store.hasRun.value">{{ store.currentTitle.value }}</p>
        <p v-else>选择一个历史任务，或先创建新的执行。</p>
      </div>
      <div v-if="store.hasRun.value" class="header-actions">
        <button v-if="canRecoverRunningTask" class="secondary-button" type="button" :disabled="store.controlling.value" @click="store.runControl('resume')">恢复执行</button>
        <button v-if="canPause" class="secondary-button" type="button" :disabled="store.controlling.value" @click="store.runControl('pause')">暂停</button>
        <button v-if="canResume" class="primary-button" type="button" :disabled="store.controlling.value" @click="store.runControl('resume')">继续运行</button>
        <button v-if="canRerun" class="secondary-button" type="button" :disabled="store.controlling.value" @click="store.runControl('rerun')">重新运行</button>
        <button v-if="canCancel" class="secondary-button danger-button" type="button" :disabled="store.controlling.value" @click="confirmCancel = true">取消任务</button>
        <button class="icon-action refresh-action" type="button" aria-label="刷新运行状态" @click="store.refreshCurrent()">↻</button>
      </div>
    </header>

    <div v-if="!store.hasRun.value && !store.initializing.value" class="surface empty-state large-empty">
      <span class="empty-orbit"><i /></span>
      <h2>还没有正在查看的任务</h2>
      <p>创建新任务，或从历史记录中打开一次已有运行。</p>
      <div><RouterLink class="primary-button link-button" to="/create">创建任务</RouterLink><RouterLink class="secondary-button link-button" to="/history">查看历史</RouterLink></div>
    </div>

    <template v-else-if="store.hasRun.value">
      <QueueProgress
        v-if="store.queue.value"
        :queue="store.queue.value"
        :disabled="store.controlling.value"
        @skip="store.skipSubtask"
        @move="store.movePendingSubtask"
      />
      <TaskStatus v-if="store.task.value" :task="store.task.value" />
      <section v-if="store.task.value" class="surface harness-checkpoints" data-test="harness-checkpoints">
        <div class="surface-heading compact-heading"><div><span class="section-kicker">Harness 控制面</span><h2>阶段检查点</h2></div></div>
        <div class="checkpoint-grid">
          <div v-for="item in harnessCheckpoints" :key="item.label"><span>{{ item.label }}</span><strong>{{ item.value }}</strong></div>
        </div>
      </section>

      <div class="monitor-grid">
        <EventTimeline :events="store.events.value" />
        <section class="surface log-surface">
          <div class="surface-heading compact-heading">
            <div><span class="section-kicker">命令输出</span><h2>完整日志</h2></div>
            <span class="surface-count">{{ store.logs.value.length }}</span>
          </div>
          <div v-if="store.logs.value.length" class="log-layout">
            <div class="log-tabs">
              <button
                v-for="log in store.logs.value"
                :key="log.log_id"
                type="button"
                :class="{ active: store.selectedLogId.value === log.log_id }"
                @click="store.selectLog(log.log_id)"
              ><span>{{ log.name }}</span><small>{{ Math.ceil(log.size / 1024) }} KB</small></button>
            </div>
            <pre class="log-content">{{ store.logContent.value || "选择一份日志查看完整命令输出。" }}</pre>
          </div>
          <div v-else class="empty-inline">命令完成后，可在这里查看持久化且已脱敏的日志正文。</div>
        </section>
      </div>

      <ValidationRounds v-if="store.task.value" :rounds="store.task.value.rounds" />

      <nav class="next-step-bar" aria-label="后续步骤">
        <div><span>下一步</span><strong>检查代码变更和 Codex 回复</strong></div>
        <RouterLink class="primary-button link-button" to="/changes">查看变更 →</RouterLink>
      </nav>
    </template>

    <div v-if="confirmCancel" class="dialog-backdrop" @click.self="confirmCancel = false">
      <div class="confirm-dialog" role="dialog" aria-modal="true">
        <span class="dialog-icon danger-dialog-icon">!</span>
        <h3>确认取消这次运行？</h3>
        <p>系统会在安全检查点停止，不会提交、合并或推送任何代码。</p>
        <div class="dialog-actions"><button class="secondary-button" type="button" @click="confirmCancel = false">返回</button><button class="primary-button danger-primary" type="button" @click="cancel">确认取消</button></div>
      </div>
    </div>
  </div>
</template>
