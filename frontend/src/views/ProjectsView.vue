<script setup lang="ts">
import { useRouter } from "vue-router";

import { useOrchestrator } from "../composables/useOrchestrator";

const store = useOrchestrator();
const router = useRouter();

async function select(projectId: string): Promise<void> {
  await store.selectProject(projectId);
  await router.push(store.hasRun.value ? "/monitor" : "/create");
}
</script>

<template>
  <div class="view-stack projects-view">
    <header class="view-header"><div><span class="section-kicker">服务端允许列表</span><h1>项目</h1><p>每个项目保持独立状态与串行执行，全局并发由后端统一限制。</p></div></header>
    <div class="project-grid">
      <article v-for="project in store.projects.value" :key="project.project_id" class="surface project-card" :class="{ selected: project.project_id === store.activeProjectId.value }">
        <div class="project-card-top"><span class="project-glyph">{{ project.name.slice(0, 1).toUpperCase() }}</span><span v-if="project.is_default" class="default-chip">默认</span></div>
        <h2>{{ project.name }}</h2><code>{{ project.repo_root }}</code>
        <div class="project-runtime"><i :class="{ active: project.active_identifier }" /><span>{{ project.active_identifier ? `运行中 · ${project.active_identifier}` : "当前空闲" }}</span></div>
        <button v-if="project.project_id !== store.activeProjectId.value" class="secondary-button" type="button" @click="select(project.project_id)">切换到此项目</button>
        <span v-else class="selected-project-label">✓ 当前项目</span>
      </article>
    </div>
    <section class="surface configuration-note"><div><span class="section-kicker">配置说明</span><h2>项目根目录由服务端控制</h2></div><p>浏览器不能任意指定仓库路径。新增项目请编辑后端配置中的 <code>agent.projects</code>，重启后才会进入允许列表。</p></section>
  </div>
</template>
