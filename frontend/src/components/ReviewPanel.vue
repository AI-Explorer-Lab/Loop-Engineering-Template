<script setup lang="ts">
import { computed, nextTick, ref, watch } from "vue";

import {
  deliveryProgressFor,
  useOrchestrator,
} from "../composables/useOrchestrator";
import type { ReviewDecision, TaskData } from "../types/task";

const props = defineProps<{ task: TaskData; submitting: boolean }>();
const store = useOrchestrator();
const emit = defineEmits<{
  review: [payload: { decision: ReviewDecision; reviewer: string; comment: string; commit_subject: string }];
  retryCommit: [];
  retryArchive: [];
}>();

const reviewer = ref("");
const comment = ref("");
const commitSubject = ref("");
const formError = ref("");
const pendingDecision = ref<ReviewDecision | null>(null);
const confirmButton = ref<HTMLButtonElement | null>(null);
const decisionLabels: Record<ReviewDecision, string> = {
  approved: "批准变更",
  changes_requested: "要求修改",
  rejected: "驳回任务",
};
const existingReview = computed(() => props.task.review as Record<string, unknown> | null);
const deliveryProgress = computed(() =>
  deliveryProgressFor(
    props.task,
    store.capabilities.value === null
      ? null
      : store.capabilities.value.status !== "unavailable",
  ),
);
const deliveryLabels: Record<TaskData["delivery_status"], string> = {
  not_ready: "尚未进入交付",
  commit_pending: "等待创建 commit",
  committing: "正在创建 commit",
  committed: "Commit 已完成",
  archive_pending: "Archiver 正在生成归档/写入知识",
  archived: "归档完成",
  failed: "交付需要重试",
  unavailable: "历史信息缺失",
};
const archiveOutbox = computed(() => {
  const value = props.task.archive.outbox;
  return typeof value === "object" && value !== null
    ? value as Record<string, unknown>
    : {};
});
const canRetryArchive = computed(() =>
  props.task.delivery_status === "failed" &&
  props.task.commit.status === "committed" &&
  archiveOutbox.value.status !== "completed",
);
const canRetryCommit = computed(() =>
  props.task.delivery_status === "failed" && props.task.commit.status !== "committed",
);
const impactCopy = computed(() => {
  if (!pendingDecision.value) return "";
  if (!props.task.queue_id) {
    return pendingDecision.value === "changes_requested"
      ? "任务会从当前 Thread 继续修改，原审核记录保持不变。"
      : pendingDecision.value === "approved"
        ? "批准会绑定当前 Diff 和 commit subject，并在任务分支创建一次 commit；不会 merge 或 push。"
        : "结论会绑定当前 Diff 指纹并写入不可变审核历史。";
  }
  const labels: Record<ReviewDecision, string> = {
    approved: "当前子任务将完成，队列会继续执行下一个尚未开始的子任务。",
    changes_requested: "当前子任务会从同一 Thread 继续修改，队列暂不前进。",
    rejected: "整个长任务将停止，后续子任务不会自动执行。",
  };
  return labels[pendingDecision.value];
});

function prepare(decision: ReviewDecision): void {
  if (!reviewer.value.trim()) {
    formError.value = "请填写审核人。";
    return;
  }
  if (decision === "approved" && !commitSubject.value.trim()) {
    formError.value = "批准前请填写 commit subject。";
    return;
  }
  formError.value = "";
  pendingDecision.value = decision;
}

function confirm(): void {
  if (!pendingDecision.value) return;
  emit("review", {
    decision: pendingDecision.value,
    reviewer: reviewer.value.trim(),
    comment: comment.value.trim(),
    commit_subject: pendingDecision.value === "approved" ? commitSubject.value.trim() : "",
  });
  pendingDecision.value = null;
}

function display(value: unknown): string {
  return value === null || value === undefined || value === "" ? "—" : String(value);
}

watch(pendingDecision, async (value) => {
  if (!value) return;
  await nextTick();
  confirmButton.value?.focus();
});

watch(
  () => props.task.task_id,
  () => {
    const existing = props.task.review?.commit_subject;
    commitSubject.value = typeof existing === "string" && existing.trim()
      ? existing
      : props.task.requirement.split(/\r?\n/, 1)[0].trim().slice(0, 200);
  },
  { immediate: true },
);
</script>

<template>
  <section class="surface review-surface" data-test="review-panel">
    <div class="surface-heading">
      <div><span class="section-kicker">人工关口</span><h2>提交审核结论</h2></div>
      <span class="hash-chip">SHA {{ task.final_diff_sha256.slice(0, 10) || "—" }}</span>
    </div>

    <div v-if="task.diff_redaction_count > 0" class="callout danger-callout">
      <strong>暂时不能提交审核</strong>
      <p>Diff 中有 {{ task.diff_redaction_count }} 处疑似敏感信息已被替换，请先移除后重新运行。</p>
    </div>

    <dl class="review-result delivery-state-card">
      <div><dt>交付状态</dt><dd>{{ deliveryLabels[task.delivery_status] }}</dd></div>
      <div><dt>审批</dt><dd>{{ deliveryProgress.review }}</dd></div>
      <div><dt>Commit</dt><dd>{{ deliveryProgress.commit }}</dd></div>
      <div><dt>Archiver</dt><dd>{{ deliveryProgress.archive }}</dd></div>
      <div v-if="task.commit.commit_sha"><dt>Commit SHA</dt><dd><code>{{ task.commit.commit_sha }}</code></dd></div>
      <div v-if="task.commit.subject"><dt>Subject</dt><dd>{{ task.commit.subject }}</dd></div>
      <div v-if="task.archive.summary"><dt>知识归档</dt><dd>{{ archiveOutbox.status === 'completed' ? '已完成' : '本地摘要已保存' }}</dd></div>
    </dl>
    <div v-if="submitting && !deliveryProgress.visible" class="callout warning-callout" data-test="review-submitting">
      <strong>正在记录审批</strong>
      <p>审批返回后会继续显示 Commit 与 Archiver 的实际进度。</p>
    </div>
    <div v-if="task.delivery_status === 'failed'" class="callout danger-callout delivery-retry">
      <strong>自动交付未完成</strong>
      <p>{{ task.last_error_summary || task.commit.error || "可以在不重新生成代码的情况下重试对应检查点。" }}</p>
      <button v-if="canRetryCommit" class="secondary-button" type="button" :disabled="submitting" @click="emit('retryCommit')">重试 commit</button>
      <button v-if="canRetryArchive" class="secondary-button" type="button" :disabled="submitting" @click="emit('retryArchive')">重试知识归档</button>
    </div>

    <dl v-if="existingReview && task.review_status !== 'pending'" class="review-result">
      <div><dt>结论</dt><dd>{{ display(existingReview.decision) }}</dd></div>
      <div><dt>审核人</dt><dd>{{ display(existingReview.reviewer) }}</dd></div>
      <div><dt>说明</dt><dd>{{ display(existingReview.comment) }}</dd></div>
      <div v-if="existingReview.commit_subject"><dt>Commit subject</dt><dd>{{ display(existingReview.commit_subject) }}</dd></div>
      <div><dt>对应 Diff</dt><dd><code>{{ display(existingReview.reviewed_diff_sha256) }}</code></dd></div>
    </dl>

    <form v-else class="review-form" @submit.prevent>
      <label>审核人（本地声明身份）<input v-model="reviewer" data-test="reviewer" :disabled="submitting" /></label>
      <label>审核说明<textarea v-model="comment" data-test="review-comment" rows="4" :disabled="submitting" placeholder="记录判断依据，方便之后回看。" /></label>
      <label>Commit subject
        <input v-model="commitSubject" data-test="commit-subject" maxlength="200" :disabled="submitting" />
        <small>仅在批准时使用；会与当前 Diff 一起绑定。系统不会 merge 或 push。</small>
      </label>
      <p v-if="formError" class="form-error" role="alert">{{ formError }}</p>
      <div class="review-actions">
        <button class="primary-button" type="button" data-test="approve" :disabled="submitting || task.diff_redaction_count > 0" @click="prepare('approved')">批准</button>
        <button class="secondary-button" type="button" :disabled="submitting || task.diff_redaction_count > 0" @click="prepare('changes_requested')">要求修改</button>
        <button class="secondary-button danger-button" type="button" :disabled="submitting || task.diff_redaction_count > 0" @click="prepare('rejected')">驳回</button>
      </div>
    </form>

    <div v-if="task.review_history.length" class="review-history">
      <h3>审核历史</h3>
      <ol>
        <li v-for="(item, index) in task.review_history" :key="index">
          <strong>第 {{ item.review_number || index + 1 }} 次 · {{ display(item.decision) }}</strong>
          <span>{{ display(item.reviewer) }} · {{ display(item.comment) }}</span>
        </li>
      </ol>
    </div>

    <div v-if="pendingDecision" class="dialog-backdrop" @click.self="pendingDecision = null" @keydown.esc="pendingDecision = null">
      <div class="confirm-dialog" role="dialog" aria-modal="true" aria-labelledby="review-confirm-title" tabindex="-1">
        <span class="dialog-icon">✓</span>
        <h3 id="review-confirm-title">确认{{ decisionLabels[pendingDecision] }}？</h3>
        <p>{{ impactCopy }}</p>
        <div class="dialog-actions">
          <button class="secondary-button" type="button" @click="pendingDecision = null">返回检查</button>
          <button ref="confirmButton" class="primary-button" data-test="confirm-review" type="button" @click="confirm">确认提交</button>
        </div>
      </div>
    </div>
  </section>
</template>
