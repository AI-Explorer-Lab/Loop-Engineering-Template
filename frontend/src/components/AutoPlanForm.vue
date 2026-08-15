<script setup lang="ts">
import { computed, ref, watch } from "vue";

import type {
  PlanCreatePayload,
  PlanDraft,
  PlannedSubtask,
} from "../types/task";

const props = defineProps<{
  plan: PlanDraft | null;
  disabled?: boolean;
  planning?: boolean;
  confirming?: boolean;
}>();
const emit = defineEmits<{
  generate: [payload: PlanCreatePayload];
  confirm: [payload: { reviewer: string; draft: PlanDraft }];
}>();

const requirement = ref("");
const reviewer = ref("");
const edited = ref<PlanDraft | null>(null);
const formError = ref("");

const acceptanceEntries = computed(() =>
  Object.entries(edited.value?.acceptance_criteria || {}),
);
const assignedAcceptanceIds = computed(() => new Set(
  (edited.value?.subtasks || []).flatMap((item) => item.source_acceptance_ids),
));
const unassignedAcceptanceIds = computed(() =>
  acceptanceEntries.value
    .map(([id]) => id)
    .filter((id) => !assignedAcceptanceIds.value.has(id)),
);

watch(
  () => props.plan,
  (value) => {
    edited.value = value
      ? JSON.parse(JSON.stringify(value)) as PlanDraft
      : null;
    formError.value = "";
  },
  { immediate: true },
);

function generate(): void {
  const normalizedRequirement = requirement.value.trim();
  if (!normalizedRequirement) {
    formError.value = "请填写完整需求";
    return;
  }
  formError.value = "";
  emit("generate", {
    requirement: normalizedRequirement,
    acceptance_criteria: [],
  });
}

function normalizeSequences(): void {
  if (!edited.value) return;
  edited.value.subtasks.forEach((item, index) => {
    item.sequence = index + 1;
  });
  edited.value.execution_mode = edited.value.subtasks.length === 1 ? "single" : "queue";
}

function addSubtask(): void {
  if (!edited.value) return;
  const next: PlannedSubtask = {
    sequence: edited.value.subtasks.length + 1,
    title: "",
    requirement_slice: "",
    source_acceptance_ids: [],
  };
  edited.value.subtasks.push(next);
  normalizeSequences();
}

function removeSubtask(index: number): void {
  if (!edited.value || edited.value.subtasks.length === 1) return;
  edited.value.subtasks.splice(index, 1);
  normalizeSequences();
}

function moveSubtask(index: number, offset: -1 | 1): void {
  if (!edited.value) return;
  const target = index + offset;
  if (target < 0 || target >= edited.value.subtasks.length) return;
  const [item] = edited.value.subtasks.splice(index, 1);
  edited.value.subtasks.splice(target, 0, item);
  normalizeSequences();
}

function confirm(): void {
  if (!edited.value) return;
  const reviewerValue = reviewer.value.trim();
  if (!reviewerValue) {
    formError.value = "请填写 Plan 确认人";
    return;
  }
  normalizeSequences();
  edited.value.name = edited.value.name.trim();
  edited.value.subtasks.forEach((item) => {
    item.title = item.title.trim();
    item.requirement_slice = item.requirement_slice.trim();
    item.source_acceptance_ids = [...new Set(item.source_acceptance_ids)].sort();
  });
  if (!edited.value.name) {
    formError.value = "Plan 名称不能为空";
    return;
  }
  if (edited.value.subtasks.some((item) => !item.title || !item.requirement_slice)) {
    formError.value = "每个子任务都需要标题和需求切片";
    return;
  }
  if (edited.value.subtasks.some((item) => item.source_acceptance_ids.length === 0)) {
    formError.value = "每个子任务至少要映射一条原始验收标准";
    return;
  }
    if (unassignedAcceptanceIds.value.length) {
    formError.value = `仍有未映射的验收标准：${unassignedAcceptanceIds.value.join("、")}`;
    return;
  }
  if (edited.value.clarification_questions.length) {
    edited.value.clarification_questions = [];
  }
  edited.value.status = "ready";
  edited.value.unassigned_acceptance_ids = [];
  formError.value = "";
  emit("confirm", { reviewer: reviewerValue, draft: edited.value });
}
</script>

<template>
  <form v-if="!edited" class="task-form plan-input-form" data-test="plan-form" @submit.prevent="generate">
    <label>完整需求
      <textarea v-model="requirement" data-test="plan-requirement" :disabled="disabled || planning" placeholder="描述最终结果和明确边界；Codex 会生成验收标准和执行切片" />
    </label>
    <p class="field-hint plan-generation-hint">你只需要描述想要的结果。Codex 会根据需求生成可观察的验收标准，并在执行前展示给你确认</p>
    <p v-if="formError" class="form-error" role="alert">{{ formError }}</p>
    <button class="primary-button" type="submit" data-test="generate-plan" :disabled="disabled || planning">
      {{ planning ? "正在生成草稿…" : "生成 Plan 草稿" }}
    </button>
    <p class="field-hint">生成草稿不会创建任务、队列、分支或 worktree</p>
  </form>

  <section v-else class="plan-editor" data-test="plan-preview">
    <div class="plan-summary-bar">
      <div><span>Plan</span><code>{{ edited.plan_id }}</code></div>
      <span class="status-chip" :class="edited.status === 'ready' ? 'status-success' : 'status-manual_review'">{{ edited.status }}</span>
    </div>
    <label class="plan-name-field">Plan 名称<input v-model="edited.name" :disabled="confirming" /></label>

    <div v-if="edited.warnings.length || unassignedAcceptanceIds.length" class="callout warning-callout">
      <strong>确认前需要复核</strong>
      <p v-for="warning in edited.warnings" :key="warning">{{ warning }}</p>
      <p v-if="unassignedAcceptanceIds.length">未映射：{{ unassignedAcceptanceIds.join("、") }}</p>
    </div>
    <div v-if="edited.clarification_questions.length" class="callout warning-callout">
      <strong>需要先澄清需求</strong>
      <p v-for="question in edited.clarification_questions" :key="question">{{ question }}</p>
    </div>

    <ol class="plan-subtask-list">
      <li v-for="(subtask, index) in edited.subtasks" :key="index" class="plan-subtask-card">
        <header>
          <strong>步骤 {{ index + 1 }}</strong>
          <div class="order-actions">
            <button class="icon-action" type="button" aria-label="上移 Plan 子任务" :disabled="index === 0 || confirming" @click="moveSubtask(index, -1)">↑</button>
            <button class="icon-action" type="button" aria-label="下移 Plan 子任务" :disabled="index === edited.subtasks.length - 1 || confirming" @click="moveSubtask(index, 1)">↓</button>
            <button class="text-action danger-text" type="button" :disabled="edited.subtasks.length === 1 || confirming" @click="removeSubtask(index)">删除</button>
          </div>
        </header>
        <label>标题<input v-model="subtask.title" :data-test="`plan-subtask-title-${index}`" :disabled="confirming" /></label>
        <label>需求切片<textarea v-model="subtask.requirement_slice" :data-test="`plan-subtask-requirement-${index}`" rows="3" :disabled="confirming" /></label>
        <fieldset>
          <legend>复核 Codex 生成的验收标准</legend>
          <label v-for="([id, criterion]) in acceptanceEntries" :key="id" class="acceptance-option">
            <input v-model="subtask.source_acceptance_ids" type="checkbox" :value="id" :disabled="confirming" />
            <span><code>{{ id }}</code>{{ criterion }}</span>
          </label>
        </fieldset>
      </li>
    </ol>

    <button class="secondary-button" type="button" data-test="add-plan-subtask" :disabled="confirming" @click="addSubtask">＋ 添加需求切片</button>
    <label class="plan-reviewer-field">Plan 确认人
      <input v-model="reviewer" data-test="plan-reviewer" :disabled="confirming" placeholder="本地声明身份" />
    </label>
    <p v-if="formError" class="form-error" role="alert">{{ formError }}</p>
    <div class="plan-confirmation-note">
      <strong>确认后才会开始执行</strong>
      <p>{{ edited.subtasks.length === 1 ? "将创建一个单任务" : `将按当前顺序创建 ${edited.subtasks.length} 个串行子任务` }}</p>
    </div>
    <button class="primary-button" type="button" data-test="confirm-plan" :disabled="confirming" @click="confirm">
      {{ confirming ? "正在确认并启动…" : "确认 Plan 并开始执行" }}
    </button>
  </section>
</template>
