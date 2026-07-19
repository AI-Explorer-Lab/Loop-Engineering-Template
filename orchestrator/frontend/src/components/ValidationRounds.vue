<script setup lang="ts">
import type { ValidationRound } from "../types/task";

defineProps<{ rounds: ValidationRound[] }>();

function duration(round: ValidationRound): string {
  const total = round.commands.reduce((sum, command) => sum + command.duration_seconds, 0);
  return `${total.toFixed(total >= 10 ? 0 : 1)}s`;
}
</script>

<template>
  <section v-if="rounds.length" class="surface" data-test="validation-rounds">
    <div class="surface-heading compact-heading">
      <div><span class="section-kicker">机器验证</span><h2>验证轮次</h2></div>
      <span class="surface-count">{{ rounds.length }} 轮</span>
    </div>
    <div class="round-list">
      <details v-for="round in rounds" :key="round.round_number" class="round-card">
        <summary>
          <span class="round-index">{{ round.round_number }}</span>
          <span class="round-name">{{ round.stage || "验证" }}</span>
          <span :class="round.passed ? 'success-text' : 'danger-text'">
            {{ round.passed ? "通过" : "失败" }}
          </span>
          <span class="round-duration">{{ duration(round) }}</span>
        </summary>
        <div class="round-body">
          <p v-if="round.failure_summary" class="round-summary">{{ round.failure_summary }}</p>
          <ul class="command-list">
            <li v-for="(command, index) in round.commands" :key="index">
              <span :class="command.passed ? 'command-pass' : 'command-fail'" />
              <code>{{ command.command.join(" ") }}</code>
              <span>{{ command.duration_seconds.toFixed(2) }}s</span>
            </li>
          </ul>
        </div>
      </details>
    </div>
  </section>
</template>
