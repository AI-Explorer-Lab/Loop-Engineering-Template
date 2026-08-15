<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { useRouter } from "vue-router";

import { useOrchestrator } from "../composables/useOrchestrator";

const store = useOrchestrator();
const router = useRouter();
const showCreateForm = ref(false);
const projectName = ref("");
const projectPath = ref("");
const projectType = ref<"python" | "frontend" | "fullstack">("python");
const knowledgeActorId = ref("");
const selectedValidationOptions = ref<string[]>(["python_tests"]);
const backendArchitectureEnabled = ref(false);
const formError = ref("");
const creating = ref(false);
const validationOptions = computed(() => projectType.value === "python"
  ? [{ id: "python_tests", label: "Python 测试", required: true }]
  : projectType.value === "fullstack"
    ? [
        { id: "python_tests", label: "后端 Python 测试", required: true },
        { id: "frontend_tests", label: "前端单元测试", required: true },
        { id: "frontend_typecheck", label: "TypeScript 类型检查", required: false },
        { id: "frontend_build", label: "前端生产构建", required: false },
      ]
    : [
        { id: "frontend_tests", label: "前端单元测试", required: true },
        { id: "frontend_typecheck", label: "TypeScript 类型检查", required: false },
        { id: "frontend_build", label: "前端生产构建", required: false },
      ]);

watch(projectType, (value) => {
  selectedValidationOptions.value = value === "python"
    ? ["python_tests"]
    : value === "fullstack" ? ["python_tests", "frontend_tests"] : ["frontend_tests"];
});

function toggleValidationOption(id: string, checked: boolean): void {
  if (!checked) selectedValidationOptions.value = selectedValidationOptions.value.filter((item) => item !== id);
  else if (!selectedValidationOptions.value.includes(id)) selectedValidationOptions.value.push(id);
}

async function select(projectId: string): Promise<void> {
  await store.selectProject(projectId);
  await router.push(store.hasRun.value ? "/monitor" : "/create");
}

async function create(): Promise<void> {
  const name = projectName.value.trim();
  const repoPath = projectPath.value.trim();
  const actor = knowledgeActorId.value.trim();
  if (!name || !repoPath || !actor) {
    formError.value = "请填写项目名称、目标路径和知识库身份。";
    return;
  }
  formError.value = "";
  creating.value = true;
  const created = await store.createProject({
    name,
    repo_path: repoPath,
    project_type: projectType.value,
    validation_options: selectedValidationOptions.value,
    knowledge_actor_id: actor,
    backend_architecture_enabled: backendArchitectureEnabled.value,
  });
  creating.value = false;
  if (!created) {
    formError.value = store.pageError.value || "项目创建失败。";
    return;
  }
  projectName.value = "";
  projectPath.value = "";
  knowledgeActorId.value = "";
  projectType.value = "python";
  selectedValidationOptions.value = ["python_tests"];
  backendArchitectureEnabled.value = false;
  showCreateForm.value = false;
  await router.push("/create");
}

async function remove(project: { project_id: string; name: string }): Promise<void> {
  if (!window.confirm(`确定删除项目“${project.name}”的登记配置吗？项目目录不会删除。`)) return;
  await store.deleteProject(project.project_id);
}
</script>

<template>
  <div class="view-stack projects-view">
    <header class="view-header">
      <div><span class="section-kicker">本地项目工作区</span><h1>项目</h1><p>每个项目保持独立状态与串行执行；新项目会自动初始化 Git、Harness 项目配置和本地运行隔离。</p></div>
      <button class="primary-button" type="button" data-test="new-project" @click="showCreateForm = !showCreateForm">＋ 新建项目</button>
    </header>
    <section v-if="showCreateForm" class="surface project-create-form" data-test="project-create-form">
      <div class="surface-heading compact-heading"><div><span class="section-kicker">创建本地项目</span><h2>填写项目基础配置</h2></div></div>
      <form class="task-form" @submit.prevent="create">
        <label>项目名称<input v-model="projectName" data-test="project-name" :disabled="creating" placeholder="例如：read-notes" /></label>
        <label>目标路径<input v-model="projectPath" data-test="project-path" :disabled="creating" placeholder="例如：/Users/mon/Documents/read-notes" /></label>
        <label>项目类型<select v-model="projectType" :disabled="creating"><option value="python">Python / 后端</option><option value="frontend">前端</option><option value="fullstack">全栈</option></select></label>
        <label>知识库身份<input v-model="knowledgeActorId" :disabled="creating" placeholder="例如：zhangsan" /><span class="field-hint">创建时通过 MCP 校验。</span></label>
        <fieldset class="validation-options"><legend>验证能力</legend><label v-for="item in validationOptions" :key="item.id" class="checkbox-row"><input type="checkbox" :checked="selectedValidationOptions.includes(item.id)" :disabled="creating || item.required" @change="toggleValidationOption(item.id, ($event.target as HTMLInputElement).checked)" /><span>{{ item.label }}{{ item.required ? "（必选）" : "（可选）" }}</span></label></fieldset>
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
        <div class="button-row">
          <button v-if="project.project_id !== store.activeProjectId.value" class="secondary-button" type="button" @click="select(project.project_id)">切换到此项目</button>
          <span v-else class="selected-project-label">✓ 当前项目</span>
          <button v-if="!project.is_default" class="text-action danger-text" type="button" @click="remove(project)">删除配置</button>
        </div>
      </article>
    </div>
    <section class="surface configuration-note"><div><span class="section-kicker">创建后的默认行为</span><h2>项目会立即进入允许列表</h2></div><p>后端会创建目录、初始化 Git、写入 <code>.harness/project.json</code> 和 <code>.gitignore</code>，并按项目类型和勾选项保存验证配置；知识库默认启用，中期记忆默认读取。</p></section>
  </div>
</template>
