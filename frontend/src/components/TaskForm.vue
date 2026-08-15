<script setup lang="ts">
import { ref } from "vue";

import type { TaskCreatePayload } from "../types/task";


const props = defineProps<{
  disabled?: boolean;
}>();

const emit = defineEmits<{
  submit: [payload: TaskCreatePayload];
}>();

const requirement = ref("");
const criteriaText = ref("");
const validationMessage = ref("");

function handleCriteriaKeydown(event: KeyboardEvent): void {
  if (event.key !== "Enter") return;
  const textarea = event.target as HTMLTextAreaElement;
  const beforeCursor = textarea.value.slice(0, textarea.selectionStart);
  const nextNumber = beforeCursor.split("\n").length + 1;
  const afterCursor = textarea.value.slice(textarea.selectionEnd);
  const nextValue = `${beforeCursor}\n${nextNumber}. ${afterCursor}`;
  event.preventDefault();
  criteriaText.value = nextValue;
  requestAnimationFrame(() => {
    const cursor = beforeCursor.length + `\n${nextNumber}. `.length;
    textarea.setSelectionRange(cursor, cursor);
  });
}

function parseCriteria(): string[] {
  return criteriaText.value
    .split("\n")
    .map((item) => item.replace(/^\s*\d+[.)、]\s*/, "").trim())
    .filter(Boolean);
}

function submit(): void {
  const normalizedRequirement = requirement.value.trim();
  const normalizedCriteria = parseCriteria();
  if (!normalizedRequirement) {
    validationMessage.value = "请填写功能需求";
    return;
  }
  if (!normalizedCriteria.length) {
    validationMessage.value = "每条验收标准都需要填写";
    return;
  }
  validationMessage.value = "";
  emit("submit", {
    requirement: normalizedRequirement,
    acceptance_criteria: normalizedCriteria,
  });
}
</script>

<template>
  <form class="task-form" data-test="task-form" @submit.prevent="submit">
    <div class="field-group">
      <label for="requirement">功能需求</label>
      <textarea
        id="requirement"
        v-model="requirement"
        data-test="requirement"
        rows="5"
        :disabled="props.disabled"
        placeholder="例如：交易列表支持按最低金额筛选"
      />
    </div>

    <fieldset class="criteria" :disabled="props.disabled">
      <legend>验收标准</legend>
      <p class="field-hint">
        每行写一条可观察、可验证的结果，编号可写可不写。按回车会自动生成下一项编号，两种格式都支持
      </p>
      <textarea
        v-model="criteriaText"
        data-test="criteria"
        rows="6"
        placeholder="每行一条，可写编号：1. 传入 min_amount=100 时，只返回金额大于或等于 100 的交易"
        @keydown="handleCriteriaKeydown"
      />
    </fieldset>

    <p v-if="validationMessage" class="form-error" role="alert">
      {{ validationMessage }}
    </p>

    <button
      class="primary-button"
      data-test="submit"
      type="submit"
      :disabled="props.disabled"
    >
      {{ props.disabled ? "任务执行中" : "提交给 Codex" }}
    </button>
  </form>
</template>
