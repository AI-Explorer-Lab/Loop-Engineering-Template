<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { useRouter } from "vue-router";

import QueueForm from "../components/QueueForm.vue";
import TaskForm from "../components/TaskForm.vue";
import AutoPlanForm from "../components/AutoPlanForm.vue";
import { useOrchestrator } from "../composables/useOrchestrator";
import type {
  PlanCreatePayload,
  PlanDraft,
  QueueCreatePayload,
  TaskCreatePayload,
} from "../types/task";

const store = useOrchestrator();
const router = useRouter();
const mode = ref<"task" | "queue" | "auto">("task");
const showProjectForm = ref(false);
const projectName = ref("");
const projectPath = ref("");
const projectType = ref<"python" | "frontend" | "fullstack">("python");
const knowledgeActorId = ref("");
const selectedValidationOptions = ref<string[]>(["python_tests"]);
const backendArchitectureEnabled = ref(false);
const projectFormError = ref("");
const creatingProject = ref(false);
const disabled = computed(() =>
  store.submitting.value ||
  store.planning.value ||
  store.confirmingPlan.value ||
  store.isRunning.value,
);

const validationOptions = computed(() => {
  if (projectType.value === "python") {
    return [{ id: "python_tests", label: "Python 测试", required: true, detail: "tests/ · unittest" }];
  }
  const frontend = [
    { id: "frontend_tests", label: "前端单元测试", required: true, detail: "frontend/ · npm test" },
    { id: "frontend_typecheck", label: "TypeScript 类型检查", required: false, detail: "npm run typecheck" },
    { id: "frontend_build", label: "前端生产构建", required: false, detail: "npm run build" },
  ];
  return projectType.value === "fullstack"
    ? [{ id: "python_tests", label: "后端 Python 测试", required: true, detail: "tests/ · unittest" }, ...frontend]
    : frontend;
});

const requiredValidationOptions = computed(() =>
  validationOptions.value.filter((item) => item.required).map((item) => item.id),
);

watch(projectType, (value) => {
  const required = value === "python" ? ["python_tests"] : value === "fullstack" ? ["python_tests", "frontend_tests"] : ["frontend_tests"];
  selectedValidationOptions.value = [...required];
});

function toggleValidationOption(id: string, checked: boolean): void {
  if (!checked) {
    selectedValidationOptions.value = selectedValidationOptions.value.filter((item) => item !== id);
    return;
  }
  if (!selectedValidationOptions.value.includes(id)) selectedValidationOptions.value.push(id);
}

async function submitTask(payload: TaskCreatePayload): Promise<void> {
  if (await store.submitTask(payload)) await router.push("/monitor");
}

async function submitQueue(payload: QueueCreatePayload): Promise<void> {
  if (await store.submitQueue(payload)) await router.push("/monitor");
}

async function generatePlan(payload: PlanCreatePayload): Promise<void> {
  await store.generatePlan(payload);
}

async function confirmPlan(payload: { reviewer: string; draft: PlanDraft }): Promise<void> {
  if (await store.confirmCurrentPlan(payload.reviewer, payload.draft)) {
    await router.push("/monitor");
  }
}

async function createProject(): Promise<void> {
  const name = projectName.value.trim();
  const repoPath = projectPath.value.trim();
  const actor = knowledgeActorId.value.trim();
  if (!name || !repoPath || !actor) {
    projectFormError.value = "请填写项目名称、绝对路径和知识库身份";
    return;
  }
  if (!/^[A-Za-z0-9]{1,64}$/.test(name)) {
    projectFormError.value = "项目名称只能包含英文字母和数字，长度 1-64 位";
    return;
  }
  if (!repoPath.startsWith("/")) {
    projectFormError.value = "目标路径必须是绝对路径，例如 /Users/mon/Documents/read-notes";
    return;
  }
  projectFormError.value = "";
  creatingProject.value = true;
  const created = await store.createProject({
    name,
    repo_path: repoPath,
    project_type: projectType.value,
    validation_options: selectedValidationOptions.value,
    knowledge_actor_id: actor,
    backend_architecture_enabled: backendArchitectureEnabled.value,
  });
  creatingProject.value = false;
  if (!created) {
    projectFormError.value = store.pageError.value || "项目创建失败";
    return;
  }
  projectName.value = "";
  projectPath.value = "";
  knowledgeActorId.value = "";
  projectType.value = "python";
  selectedValidationOptions.value = ["python_tests"];
  backendArchitectureEnabled.value = false;
  showProjectForm.value = false;
}
</script>

<template>
  <div class="view-stack create-view">
    <header class="view-header">
      <div>
        <span class="section-kicker">开始一次受控执行</span>
        <h1>创建任务</h1>
        <p>把需求与验收标准写清楚，Codex 会在隔离工作区中执行并留下完整记录</p>
      </div>
      <div class="project-context-card">
        <span>当前项目</span>
        <strong>{{ store.activeProject.value?.name || "正在读取" }}</strong>
        <code>{{ store.activeProject.value?.repo_root || "—" }}</code>
        <button class="secondary-button project-create-trigger" type="button" data-test="open-create-project" @click="showProjectForm = !showProjectForm">
          {{ showProjectForm ? "收起新建项目" : "＋ 新建项目" }}
        </button>
      </div>
    </header>

    <section v-if="showProjectForm" class="surface inline-project-form" data-test="create-project-panel">
      <div class="surface-heading compact-heading">
          <div><span class="section-kicker">创建本地项目</span><h2>填写项目基础配置</h2></div>
      </div>
      <form class="project-form-grid" @submit.prevent="createProject">
        <label>项目名称<input v-model="projectName" data-test="create-project-name" :disabled="creatingProject" placeholder="例如：account" /><span class="field-hint">只能使用英文字母和数字，1-64 位；将绑定 Python 环境名</span></label>
        <label>绝对路径<input v-model="projectPath" data-test="create-project-path" :disabled="creatingProject" placeholder="例如：/Users/mon/Documents/read-notes" /></label>
        <label>项目类型<select v-model="projectType" :disabled="creatingProject"><option value="python">Python / 后端</option><option value="frontend">前端</option><option value="fullstack">全栈</option></select></label>
        <label>知识库身份<input v-model="knowledgeActorId" :disabled="creatingProject" placeholder="例如：zhangsan" /><span class="field-hint">创建时会通过 MCP 校验该身份是否存在于知识库</span></label>
        <fieldset class="validation-options"><legend>验证能力</legend><label v-for="item in validationOptions" :key="item.id" class="checkbox-row"><input type="checkbox" :checked="selectedValidationOptions.includes(item.id)" :disabled="creatingProject || item.required" @change="toggleValidationOption(item.id, ($event.target as HTMLInputElement).checked)" /><span>{{ item.label }}{{ item.required ? "（必选）" : "（可选）" }}<small>{{ item.detail }}</small></span></label></fieldset>
        <label class="project-architecture-toggle">
          <input v-model="backendArchitectureEnabled" data-test="create-backend-architecture-enabled" type="checkbox" :disabled="creatingProject" />
          <span><strong>启用后端架构初始化</strong><small>第一次开发时读取 MCP 的 TK-DEC-001，并据此设计后端目录、模块边界和 API；只执行一次</small></span>
        </label>
        <p class="field-hint">知识库默认启用，中期记忆默认读取。全栈项目默认要求后端测试和前端测试；类型检查、生产构建可选</p>
        <p v-if="projectFormError" class="form-error" role="alert">{{ projectFormError }}</p>
        <div class="button-row"><button class="secondary-button" type="button" :disabled="creatingProject" @click="showProjectForm = false">取消</button><button class="primary-button" type="submit" :disabled="creatingProject">{{ creatingProject ? "正在创建…" : "创建并切换项目" }}</button></div>
      </form>
    </section>

    <div class="create-layout">
      <section class="surface form-surface">
        <div class="surface-heading compact-heading">
          <div><span class="section-kicker">任务定义</span><h2>描述要交付的结果</h2></div>
          <span class="local-chip"><i /> 本机执行</span>
        </div>
        <div class="segmented-control" role="tablist" aria-label="任务类型">
          <button type="button" role="tab" :aria-selected="mode === 'task'" data-test="single-mode" :class="{ active: mode === 'task' }" :disabled="disabled" @click="mode = 'task'">
            单任务<span>一次完整改动</span>
          </button>
          <button type="button" role="tab" :aria-selected="mode === 'queue'" data-test="queue-mode" :class="{ active: mode === 'queue' }" :disabled="disabled" @click="mode = 'queue'">
            长任务<span>人工拆分、依次执行</span>
          </button>
          <button type="button" role="tab" :aria-selected="mode === 'auto'" data-test="auto-mode" :class="{ active: mode === 'auto' }" :disabled="disabled" @click="mode = 'auto'">
            自动规划<span>草稿确认后才执行</span>
          </button>
        </div>
        <TaskForm v-if="mode === 'task'" :disabled="disabled" @submit="submitTask" />
        <QueueForm v-else-if="mode === 'queue'" :disabled="disabled" @submit="submitQueue" />
        <AutoPlanForm
          v-else
          :plan="store.plan.value"
          :disabled="store.isRunning.value"
          :planning="store.planning.value"
          :confirming="store.confirmingPlan.value"
          @generate="generatePlan"
          @confirm="confirmPlan"
        />
      </section>

      <aside class="create-aside">
        <section class="surface guide-card">
          <span class="guide-index">A</span>
          <h3>先写可观察的结果</h3>
          <p>验收标准越具体，机器验证越能准确判断改动是否完成</p>
        </section>
        <section class="surface guide-card">
          <span class="guide-index">B</span>
          <h3>长任务由你决定顺序</h3>
          <p>手工或自动规划都要由你确认顺序；子任务严格串行，并传递已批准的累计 Diff</p>
        </section>
        <section v-if="store.isRunning.value" class="callout warning-callout">
          <strong>当前项目已有执行中的任务</strong>
          <p>可以先去监控页暂停、取消或等待它完成</p>
          <RouterLink class="inline-link" to="/monitor">打开运行监控 →</RouterLink>
        </section>
      </aside>
    </div>
  </div>
</template>
