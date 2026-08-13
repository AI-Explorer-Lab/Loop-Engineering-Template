<script setup lang="ts">
import { ref } from "vue";
import { useRouter } from "vue-router";

import { useOrchestrator } from "../composables/useOrchestrator";

const store = useOrchestrator();
const router = useRouter();
const showCreateForm = ref(false);
const projectName = ref("");
const projectPath = ref("");
const formError = ref("");
const creating = ref(false);

async function select(projectId: string): Promise<void> {
  await store.selectProject(projectId);
  await router.push(store.hasRun.value ? "/monitor" : "/create");
}

async function create(): Promise<void> {
  const name = projectName.value.trim();
  const repoPath = projectPath.value.trim();
  if (!name || !repoPath) {
    formError.value = "请填写项目名称和目标路径。";
    return;
  }
  formError.value = "";
  creating.value = true;
  const created = await store.createProject({ name, repo_path: repoPath });
  creating.value = false;
  if (!created) {
    formError.value = store.pageError.value || "项目创建失败。";
    return;
  }
  projectName.value = "";
  projectPath.value = "";
  showCreateForm.value = false;
  await router.push("/create");
}
</script>

<template>
  <div class="view-stack projects-view">
    <header class="view-header">
      <div><span class="section-kicker">本地项目工作区</span><h1>项目</h1><p>每个项目保持独立状态与串行执行；新项目会自动初始化 Git 和 Harness 忽略规则。</p></div>
      <button class="primary-button" type="button" data-test="new-project" @click="showCreateForm = !showCreateForm">＋ 新建项目</button>
    </header>
    <section v-if="showCreateForm" class="surface project-create-form" data-test="project-create-form">
      <div class="surface-heading compact-heading"><div><span class="section-kicker">创建本地项目</span><h2>填写项目名称和目标路径</h2></div></div>
      <form class="task-form" @submit.prevent="create">
        <label>项目名称<input v-model="projectName" data-test="project-name" :disabled="creating" placeholder="例如：read-notes" /></label>
        <label>目标路径<input v-model="projectPath" data-test="project-path" :disabled="creating" placeholder="例如：/Users/mon/Documents/read-notes" /></label>
        <p class="field-hint">后端只接受绝对路径；目标路径必须是尚不存在的新目录。</p>
        <p v-if="formError" class="form-error" role="alert">{{ formError }}</p>
        <div class="button-row"><button class="secondary-button" type="button" :disabled="creating" @click="showCreateForm = false">取消</button><button class="primary-button" type="submit" :disabled="creating">{{ creating ? "正在创建…" : "创建并进入项目" }}</button></div>
      </form>
    </section>
    <div class="project-grid">
      <article v-for="project in store.projects.value" :key="project.project_id" class="surface project-card" :class="{ selected: project.project_id === store.activeProjectId.value }">
        <div class="project-card-top"><span class="project-glyph">{{ project.name.slice(0, 1).toUpperCase() }}</span><span v-if="project.is_default" class="default-chip">默认</span></div>
        <h2>{{ project.name }}</h2><code>{{ project.repo_root }}</code>
        <div class="project-runtime"><i :class="{ active: project.active_identifier }" /><span>{{ project.active_identifier ? `运行中 · ${project.active_identifier}` : "当前空闲" }}</span></div>
        <button v-if="project.project_id !== store.activeProjectId.value" class="secondary-button" type="button" @click="select(project.project_id)">切换到此项目</button>
        <span v-else class="selected-project-label">✓ 当前项目</span>
      </article>
    </div>
    <section class="surface configuration-note"><div><span class="section-kicker">创建后的默认行为</span><h2>项目会立即进入允许列表</h2></div><p>后端会创建目录、初始化 Git、写入 <code>.gitignore</code>，并注册默认 Python unittest 验证配置；完成后前端自动选中新项目。</p></section>
  </div>
</template>
