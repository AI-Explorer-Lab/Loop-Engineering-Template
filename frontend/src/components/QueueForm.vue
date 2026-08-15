<script setup lang="ts">
import { ref } from "vue";

import type { QueueCreatePayload } from "../types/task";


defineProps<{ disabled?: boolean }>();

const emit = defineEmits<{
  submit: [payload: QueueCreatePayload];
}>();

interface EditableSubtask {
  requirement: string;
  acceptance_criteria_text: string;
}

const name = ref("");
const subtasks = ref<EditableSubtask[]>([newSubtask(), newSubtask()]);
const validationMessage = ref("");

function newSubtask(): EditableSubtask {
  return { requirement: "", acceptance_criteria_text: "" };
}

function addSubtask(): void {
  subtasks.value.push(newSubtask());
}

function removeSubtask(index: number): void {
  if (subtasks.value.length <= 2) return;
  subtasks.value.splice(index, 1);
}

function moveSubtask(index: number, offset: -1 | 1): void {
  const target = index + offset;
  if (target < 0 || target >= subtasks.value.length) return;
  const [item] = subtasks.value.splice(index, 1);
  subtasks.value.splice(target, 0, item);
}

function handleCriteriaKeydown(
  event: KeyboardEvent,
  taskIndex: number,
): void {
  if (event.key !== "Enter") return;
  const textarea = event.target as HTMLTextAreaElement;
  const beforeCursor = textarea.value.slice(0, textarea.selectionStart);
  const nextNumber = beforeCursor.split("\n").length + 1;
  const afterCursor = textarea.value.slice(textarea.selectionEnd);
  const nextValue = `${beforeCursor}\n${nextNumber}. ${afterCursor}`;
  event.preventDefault();
  subtasks.value[taskIndex].acceptance_criteria_text = nextValue;
  requestAnimationFrame(() => {
    const cursor = beforeCursor.length + `\n${nextNumber}. `.length;
    textarea.setSelectionRange(cursor, cursor);
  });
}

function parseCriteria(value: string): string[] {
  return value
    .split("\n")
    .map((item) => item.replace(/^\s*\d+[.)、]\s*/, "").trim())
    .filter(Boolean);
}

function submit(): void {
  const normalizedName = name.value.trim();
  const normalizedSubtasks = subtasks.value.map((task) => ({
    requirement: task.requirement.trim(),
    acceptance_criteria: parseCriteria(task.acceptance_criteria_text),
  }));
  if (!normalizedName) {
    validationMessage.value = "请填写长任务名称。";
    return;
  }
  if (normalizedSubtasks.some((task) => !task.requirement)) {
    validationMessage.value = "每个子任务都需要填写需求。";
    return;
  }
  if (
    normalizedSubtasks.some((task) => !task.acceptance_criteria.length)
  ) {
    validationMessage.value = "每条验收标准都需要填写。";
    return;
  }
  validationMessage.value = "";
  emit("submit", { name: normalizedName, subtasks: normalizedSubtasks });
}
</script>

<template>
  <form class="task-form" data-test="queue-form" @submit.prevent="submit">
    <div class="field-group">
      <label for="queue-name">长任务名称</label>
      <input
        id="queue-name"
        v-model="name"
        data-test="queue-name"
        :disabled="disabled"
        placeholder="例如：完成交易管理功能"
      />
    </div>

    <div class="subtask-list">
      <article
        v-for="(subtask, taskIndex) in subtasks"
        :key="taskIndex"
        class="subtask-card"
        :data-test="`subtask-${taskIndex}`"
      >
        <div class="subtask-heading">
          <strong>子任务 {{ taskIndex + 1 }}</strong>
          <div class="order-actions">
            <button
              type="button"
              class="icon-button"
              :disabled="disabled || taskIndex === 0"
              :aria-label="`上移子任务 ${taskIndex + 1}`"
              @click="moveSubtask(taskIndex, -1)"
            >↑</button>
            <button
              type="button"
              class="icon-button"
              :disabled="disabled || taskIndex === subtasks.length - 1"
              :aria-label="`下移子任务 ${taskIndex + 1}`"
              @click="moveSubtask(taskIndex, 1)"
            >↓</button>
            <button
              type="button"
              class="icon-button"
              :disabled="disabled || subtasks.length <= 2"
              :aria-label="`删除子任务 ${taskIndex + 1}`"
              @click="removeSubtask(taskIndex)"
            >×</button>
          </div>
        </div>

        <label>
          需求
          <textarea
            v-model="subtask.requirement"
            :data-test="`subtask-requirement-${taskIndex}`"
            :disabled="disabled"
            rows="3"
            placeholder="描述这个子任务要完成什么"
          />
        </label>

        <fieldset class="criteria" :disabled="disabled">
          <legend>验收标准</legend>
          <p class="field-hint">每行一条，按回车自动生成下一项编号。</p>
          <textarea
            v-model="subtask.acceptance_criteria_text"
            :data-test="`subtask-${taskIndex}-criteria`"
            rows="4"
            placeholder="1. 新增后可以在列表中看到这条交易"
            @keydown="handleCriteriaKeydown($event, taskIndex)"
          />
        </fieldset>
      </article>
    </div>

    <button
      type="button"
      class="secondary-button"
      data-test="add-subtask"
      :disabled="disabled"
      @click="addSubtask"
    >+ 增加子任务</button>

    <p v-if="validationMessage" class="form-error" role="alert">
      {{ validationMessage }}
    </p>
    <button
      type="submit"
      class="primary-button"
      data-test="submit-queue"
      :disabled="disabled"
    >{{ disabled ? "任务执行中" : "提交长任务" }}</button>
  </form>
</template>
