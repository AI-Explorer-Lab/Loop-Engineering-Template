<script setup lang="ts">
import { ref } from "vue";
import { useRouter } from "vue-router";

import { useOrchestrator } from "../composables/useOrchestrator";

const store = useOrchestrator();
const router = useRouter();
const showCreateForm = ref(false);
const projectName = ref("");
const projectPath = ref("");
const backendArchitectureEnabled = ref(false);
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
  const created = await store.createProject({
    name,
    repo_path: repoPath,
    backend_architecture_enabled: backendArchitectureEnabled.value,
  });
  creating.value = false;
  if (!created) {
    formError.value = store.pageError.value || "项目创建失败。";
    return;
  }
  projectName.value = "";
  projectPath.value = "";
  backendArchitectureEnabled.value = false;
  showCreateForm.value = false;
  await router.push("/create");
}
</script>

<template>
  <div class="view-stack projects-view">
    <header class="view-header">
      <div><span class="section-kicker">本地项目工作区</span><h1>项目</h1><p>每个项目保持独立状态与串行执行；新项目会自动初始化 Git、Harness 项目配置和本地运行隔离。</p></div>
      <button class="primary-button" type="button" data-test="new-project" @click="showCreateForm = !showCreateForm">＋ 新建项目</button>
    </header>
    <section v-if="showCreateForm" class="surface project-create-form" data-test="project-create-form">
      <div class="surface-heading compact-heading"><div><span class="section-kicker">创建本地项目</span><h2>填写项目名称和目标路径</h2></div></div>
      <form class="task-form" @submit.prevent="create">
        <label>项目名称<input v-model="projectName" data-test="project-name" :disabled="creating" placeholder="例如：read-notes" /></label>
        <label>目标路径<input v-model="projectPath" data-test="project-path" :disabled="creating" placeholder="例如：/Users/mon/Documents/read-notes" /></label>
        <label class="project-architecture-toggle">
          <input v-model="backendArchitectureEnabled" data-test="backend-architecture-enabled" type="checkbox" :disabled="creating" />
          <span><strong>启用后端架构初始化</strong><small>第一次开发时读取 MCP 的 TK-DEC-001，仅执行一次。</small></span>
        </label>
        <p class="field-hint">后端只接受绝对路径；目标路径必须是尚不存在的新目录。</p>
        <p v-if="formError" class="form-error" role="alert">{{ formError }}</p>
        <div class="button-row"><button class="secondary-button" type="button" :disabled="creating" @click="showCreateForm = false">取消</button><button class="primary-button" type="submit" :disabled="creating">{{ creating ? "正在创建…" : "创建并进入项目" }}</button></div>
      </form>
    </section>
    <div class="project-grid">
      <article v-for="project in store.projects.value" :key="project.project_id" class="surface project-card" :class="{ selected: project.project_id === store.activeProjectId.value }">
        <div class="project-card-top"><span class="project-glyph">{{ project.name.slice(0, 1).toUpperCase() }}</span><span v-if="project.is_default" class="default-chip">默认</span></div>
        <h2>{{ project.name }}</h2><code>{{ project.repo_root }}</code>
        <div v-if="project.backend_architecture_enabled" class="project-architecture-state">
          后端架构：{{ project.backend_architecture_status === "completed" ? "已完成" : project.backend_architecture_status === "in_progress" ? "初始化中" : project.backend_architecture_status === "failed" ? "初始化失败" : "待首次开发" }}
        </div>
        <div class="project-runtime"><i :class="{ active: project.active_identifier }" /><span>{{ project.active_identifier ? `运行中 · ${project.active_identifier}` : "当前空闲" }}</span></div>
        <button v-if="project.project_id !== store.activeProjectId.value" class="secondary-button" type="button" @click="select(project.project_id)">切换到此项目</button>
        <span v-else class="selected-project-label">✓ 当前项目</span>
      </article>
    </div>
    <section class="surface configuration-note"><div><span class="section-kicker">创建后的默认行为</span><h2>项目会立即进入允许列表</h2></div><p>后端会创建目录、初始化 Git、写入 <code>.harness/project.json</code> 和 <code>.gitignore</code>，并注册默认 Python unittest 验证配置；认证信息与临时运行状态保持本地隔离，完成后前端自动选中新项目。</p></section>
  </div>
</template>
