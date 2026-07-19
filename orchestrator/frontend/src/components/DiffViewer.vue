<script setup lang="ts">
import { computed, ref, watch } from "vue";

const props = defineProps<{ diff: string }>();
interface DiffSection { name: string; lines: string[] }

const selected = ref(0);
const sections = computed<DiffSection[]>(() => {
  if (!props.diff.trim()) return [];
  const values: DiffSection[] = [];
  let current: DiffSection | null = null;
  for (const line of props.diff.split("\n")) {
    if (line.startsWith("diff --git ")) {
      const match = line.match(/ b\/(.+)$/);
      current = { name: match?.[1] || line.slice(11), lines: [line] };
      values.push(current);
    } else if (current) {
      current.lines.push(line);
    } else {
      current = { name: "变更摘要", lines: [line] };
      values.push(current);
    }
  }
  return values;
});
const visibleLines = computed(() => sections.value[selected.value]?.lines || []);
watch(sections, () => { if (selected.value >= sections.value.length) selected.value = 0; });

function lineClass(line: string): string {
  if (line.startsWith("+") && !line.startsWith("+++")) return "diff-add";
  if (line.startsWith("-") && !line.startsWith("---")) return "diff-remove";
  if (line.startsWith("@@")) return "diff-hunk";
  if (line.startsWith("diff ") || line.startsWith("index ")) return "diff-meta";
  return "";
}
</script>

<template>
  <div v-if="sections.length" class="diff-layout" data-test="diff-viewer">
    <aside class="diff-files">
      <button
        v-for="(section, index) in sections"
        :key="`${section.name}-${index}`"
        type="button"
        :class="{ active: selected === index }"
        @click="selected = index"
      ><span>M</span>{{ section.name }}</button>
    </aside>
    <pre class="diff-code"><code><span
      v-for="(line, index) in visibleLines"
      :key="index"
      :class="lineClass(line)"
    ><i>{{ index + 1 }}</i>{{ line || " " }}</span></code></pre>
  </div>
  <div v-else class="empty-state compact-empty"><strong>没有文件差异</strong><p>当前运行没有生成可展示的 Diff。</p></div>
</template>
