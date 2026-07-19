<script setup lang="ts">
import { computed, ref } from "vue";

import type { TaskCreatePayload } from "../types/task";


const props = defineProps<{
  disabled?: boolean;
}>();

const emit = defineEmits<{
  submit: [payload: TaskCreatePayload];
}>();

const requirement = ref("");
const criteria = ref([""]);
const validationMessage = ref("");

const canRemoveCriterion = computed(() => criteria.value.length > 1);

function addCriterion(): void {
  criteria.value.push("");
}

function removeCriterion(index: number): void {
  if (!canRemoveCriterion.value) return;
  criteria.value.splice(index, 1);
}

function submit(): void {
  const normalizedRequirement = requirement.value.trim();
  const normalizedCriteria = criteria.value.map((item) => item.trim());
  if (!normalizedRequirement) {
    validationMessage.value = "请填写功能需求。";
    return;
  }
  if (normalizedCriteria.some((item) => !item)) {
    validationMessage.value = "每条验收标准都需要填写。";
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
        尽量写成可观察、可验证的结果，例如“传入 min_amount=100 时，只返回金额大于或等于 100 的交易”。
      </p>
      <div
        v-for="(_, index) in criteria"
        :key="index"
        class="criterion-row"
      >
        <span class="criterion-index">{{ index + 1 }}</span>
        <input
          v-model="criteria[index]"
          :data-test="`criterion-${index}`"
          type="text"
          :placeholder="`验收标准 ${index + 1}`"
        />
        <button
          class="icon-button"
          type="button"
          :disabled="!canRemoveCriterion || props.disabled"
          :aria-label="`删除验收标准 ${index + 1}`"
          @click="removeCriterion(index)"
        >
          ×
        </button>
      </div>
      <button
        class="secondary-button"
        data-test="add-criterion"
        type="button"
        :disabled="props.disabled"
        @click="addCriterion"
      >
        + 增加验收标准
      </button>
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
