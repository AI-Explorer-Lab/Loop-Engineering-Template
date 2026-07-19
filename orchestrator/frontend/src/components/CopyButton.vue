<script setup lang="ts">
import { ref } from "vue";

const props = defineProps<{ value: string; label?: string }>();
const copied = ref(false);
let timer: ReturnType<typeof setTimeout> | null = null;

async function copy(): Promise<void> {
  if (!props.value) return;
  try {
    await navigator.clipboard.writeText(props.value);
  } catch {
    const input = document.createElement("textarea");
    input.value = props.value;
    input.style.position = "fixed";
    input.style.opacity = "0";
    document.body.appendChild(input);
    input.select();
    document.execCommand("copy");
    input.remove();
  }
  copied.value = true;
  if (timer) clearTimeout(timer);
  timer = setTimeout(() => { copied.value = false; }, 1_200);
}
</script>

<template>
  <button class="copy-button" type="button" :aria-label="`复制${label || '内容'}`" @click="copy">
    {{ copied ? "已复制" : "复制" }}
  </button>
</template>
