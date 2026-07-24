import {
  computed,
  inject,
  provide,
  ref,
  type InjectionKey,
  type Ref,
} from "vue";

import {
  cancelQueue,
  createQueue,
  getQueue,
  getQueueDiff,
  getQueueReport,
  pauseQueue,
  reorderQueue,
  rerunQueue,
  resumeQueue,
  skipQueueSubtask,
} from "../api/queues";
import { getHealth } from "../api/health";
import { confirmPlan, createPlan } from "../api/plans";
import {
  eventStreamUrl,
  getCapabilities,
  getEvents,
  getLog,
  getLogs,
  getMetrics,
  getNotificationSettings,
  getNotifications,
  getProjects,
  markNotificationRead,
  updateNotificationSettings,
} from "../api/platform";
import {
  cancelTask,
  createTask,
  getTask,
  getTaskDiff,
  getTaskReport,
  pauseTask,
  rerunTask,
  retryTaskArchive,
  retryTaskCommit,
  resumeTask,
  submitTaskReview,
} from "../api/tasks";
import { ApiError, PROJECT_STORAGE_KEY } from "../api/http";
import type {
  EventRecord,
  HarnessCapabilitiesData,
  HarnessMetricsData,
  HealthData,
  LogData,
  NotificationData,
  NotificationSettingsData,
  PlanCreatePayload,
  PlanDraft,
  ProjectData,
  QueueCreatePayload,
  QueueData,
  ReviewDecision,
  RunKind,
  TaskCreatePayload,
  TaskData,
} from "../types/task";

const TASK_STORAGE_KEY = "codex-orchestrator:last-task-id";
const QUEUE_STORAGE_KEY = "codex-orchestrator:last-queue-id";
const LAST_KIND_STORAGE_KEY = "codex-orchestrator:last-kind";
const PROJECT_RUNS_STORAGE_KEY = "codex-orchestrator:project-runs";
const POLL_INTERVAL_MS = 2_000;

const ACTIVE_TASK_STATUSES = new Set<TaskData["status"]>([
  "accepted",
  "running",
  "pausing",
  "cancelling",
]);
const ACTIVE_DELIVERY_STATUSES = new Set<TaskData["delivery_status"]>([
  "commit_pending",
  "committing",
  "archive_pending",
]);
const ACTIVE_QUEUE_STATUSES = new Set<QueueData["status"]>([
  "pending",
  "running",
  "pausing",
  "cancelling",
]);

interface StoredRun {
  kind: RunKind;
  identifier: string;
}

interface StoredRuns {
  [projectId: string]: StoredRun;
}

export interface DeliveryProgress {
  visible: boolean;
  review: string;
  commit: string;
  archive: string;
}

function record(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null
    ? value as Record<string, unknown>
    : {};
}

export function deliveryProgressFor(
  task: TaskData,
  archiveCapabilityAvailable: boolean | null = true,
): DeliveryProgress {
  const commit = record(task.commit);
  const archiveSummary = record(task.archive.summary);
  const archiveOutbox = record(task.archive.outbox);
  const outboxItems = Array.isArray(archiveOutbox.items)
    ? archiveOutbox.items.map(record)
    : [];
  const completedKnowledgeItems = outboxItems.filter(
    (item) => item.status === "completed",
  ).length;
  const knowledgeProgress = outboxItems.length
    ? `（Knowledge ${completedKnowledgeItems}/${outboxItems.length}）`
    : "";
  const deliveryStarted = !["not_ready", "unavailable"].includes(task.delivery_status);
  const commitArtifactComplete =
    commit.status === "committed" ||
    Boolean(commit.commit_sha) ||
    ["committed", "archive_pending", "archived"].includes(task.delivery_status);
  const commitFailed = task.delivery_status === "failed" && !commitArtifactComplete;
  const commitComplete = commitArtifactComplete;
  const archiveArtifactComplete =
    task.delivery_status === "archived" ||
    archiveSummary.delivery_status === "archived" ||
    archiveOutbox.status === "completed";
  const archiveFailed = task.delivery_status === "failed" && commitComplete;
  const archiveComplete = archiveArtifactComplete;
  const archiveUnavailable =
    archiveCapabilityAvailable === false &&
    task.delivery_status === "committed" &&
    Object.keys(archiveSummary).length === 0 &&
    Object.keys(archiveOutbox).length === 0;
  const archiveCapabilityUnknown =
    archiveCapabilityAvailable === null &&
    task.delivery_status === "committed" &&
    Object.keys(archiveSummary).length === 0 &&
    Object.keys(archiveOutbox).length === 0;
  const archiveStarted =
    archiveComplete ||
    task.delivery_status === "archive_pending" ||
    (task.delivery_status === "committed" && !archiveUnavailable) ||
    Object.keys(archiveSummary).length > 0 ||
    Object.keys(archiveOutbox).length > 0;
  const approvalRecorded =
    task.review_status === "approved" ||
    String(record(task.review).decision || "") === "approved" ||
    deliveryStarted ||
    commitComplete ||
    archiveStarted;
  const commitStarted =
    commitComplete ||
    ["commit_pending", "committing"].includes(task.delivery_status);
  let archive = "等待 Archiver";
  if (archiveComplete) {
    archive = `归档完成${knowledgeProgress}`;
  } else if (archiveFailed) {
    archive = outboxItems.length
      ? `Archiver 写入知识失败${knowledgeProgress}`
      : "Archiver 处理失败";
  } else if (archiveUnavailable) {
    archive = "Archiver 未启用，Commit 为交付终态";
  } else if (archiveCapabilityUnknown) {
    archive = "Archiver 状态待确认，Commit 已完成";
  } else if (outboxItems.length) {
    archive = `Archiver 正在写入 Knowledge${knowledgeProgress}`;
  } else if (archiveStarted || commitComplete) {
    archive = "Archiver 正在生成归档总结";
  }

  return {
    visible: approvalRecorded || commitStarted || archiveStarted,
    review: approvalRecorded ? "审批已记录" : "等待审批",
    commit: commitComplete
      ? "Commit 已完成"
      : commitFailed
        ? "Commit 创建失败"
        : commitStarted
          ? "Commit 正在创建"
          : "等待 Commit",
    archive,
  };
}

function shouldPollTask(value: TaskData): boolean {
  return (
    ACTIVE_TASK_STATUSES.has(value.status) ||
    ACTIVE_DELIVERY_STATUSES.has(value.delivery_status)
  );
}

function shouldStartDeliveryPoll(value: TaskData): boolean {
  return shouldPollTask(value) || value.delivery_status === "committed";
}

function readStorage(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function writeStorage(key: string, value: string): void {
  try {
    localStorage.setItem(key, value);
  } catch {
    // The workbench remains usable when storage is disabled.
  }
}

function readStoredRuns(): StoredRuns {
  try {
    const value = JSON.parse(readStorage(PROJECT_RUNS_STORAGE_KEY) || "{}") as unknown;
    if (!value || typeof value !== "object" || Array.isArray(value)) return {};
    return value as StoredRuns;
  } catch {
    return {};
  }
}

function messageFrom(error: unknown, fallback: string): string {
  if (error instanceof ApiError) {
    const details = [error.code, error.requestId].filter(Boolean).join(" · ");
    return details ? `${error.message}（${details}）` : error.message;
  }
  return error instanceof Error ? error.message : fallback;
}

function setRefValue<T>(target: Ref<T>, value: T): void {
  target.value = value;
}

export function createOrchestrator() {
  const projects = ref<ProjectData[]>([]);
  const activeProjectId = ref(readStorage(PROJECT_STORAGE_KEY) || "");
  const currentKind = ref<RunKind | null>(null);
  const task = ref<TaskData | null>(null);
  const queue = ref<QueueData | null>(null);
  const report = ref("");
  const diff = ref("");
  const queueReport = ref("");
  const queueDiff = ref("");
  const events = ref<EventRecord[]>([]);
  const eventCursor = ref(0);
  const logs = ref<LogData[]>([]);
  const selectedLogId = ref("");
  const logContent = ref("");
  const notifications = ref<NotificationData[]>([]);
  const notificationSettings = ref<NotificationSettingsData>({
    in_app: true,
    browser: true,
    email_configured: false,
    webhook_configured: false,
  });
  const health = ref<HealthData | null>(null);
  const capabilities = ref<HarnessCapabilitiesData | null>(null);
  const metrics = ref<HarnessMetricsData | null>(null);
  const harnessError = ref("");
  const plan = ref<PlanDraft | null>(null);
  const healthError = ref("");
  const checkingHealth = ref(false);
  const pageError = ref("");
  const submitting = ref(false);
  const planning = ref(false);
  const confirmingPlan = ref(false);
  const reviewing = ref(false);
  const controlling = ref(false);
  const initializing = ref(true);
  let pollTimer: ReturnType<typeof setTimeout> | null = null;
  let eventSource: EventSource | null = null;
  let notificationBaselineReady = false;

  const activeProject = computed(
    () =>
      projects.value.find((project) => project.project_id === activeProjectId.value) ||
      projects.value[0] ||
      null,
  );
  const identifier = computed(() =>
    currentKind.value === "queue" ? queue.value?.queue_id || null : task.value?.task_id || null,
  );
  const runStatus = computed(() =>
    currentKind.value === "queue" ? queue.value?.status || null : task.value?.status || null,
  );
  const hasRun = computed(() => Boolean(identifier.value));
  const isRunning = computed(() =>
    currentKind.value === "queue"
      ? Boolean(queue.value && ACTIVE_QUEUE_STATUSES.has(queue.value.status))
      : Boolean(task.value && ACTIVE_TASK_STATUSES.has(task.value.status)),
  );
  const currentTitle = computed(() =>
    currentKind.value === "queue"
      ? queue.value?.name || "未选择长任务"
      : task.value?.requirement || "未选择任务",
  );
  const needsReview = computed(
    () =>
      Boolean(task.value) &&
      task.value?.review_status === "pending" &&
      ["success", "manual_review"].includes(task.value.status),
  );
  const unreadCount = computed(
    () => notificationSettings.value.in_app
      ? notifications.value.filter((item) => item.read_at === null).length
      : 0,
  );
  const connectionState = computed<"checking" | "connected" | "disconnected">(
    () => checkingHealth.value ? "checking" : health.value ? "connected" : "disconnected",
  );

  function recordError(error: unknown, fallback: string): void {
    const message = messageFrom(error, fallback);
    pageError.value = message;
    if (
      (error instanceof ApiError && error.status >= 500) ||
      error instanceof TypeError
    ) {
      health.value = null;
      healthError.value = message;
    }
  }

  function clearPollTimer(): void {
    if (pollTimer !== null) clearTimeout(pollTimer);
    pollTimer = null;
  }

  function closeEventStream(): void {
    eventSource?.close();
    eventSource = null;
  }

  function resetArtifacts(): void {
    report.value = "";
    diff.value = "";
    queueReport.value = "";
    queueDiff.value = "";
    events.value = [];
    eventCursor.value = 0;
    logs.value = [];
    selectedLogId.value = "";
    logContent.value = "";
    closeEventStream();
  }

  function resetRun(): void {
    clearPollTimer();
    resetArtifacts();
    currentKind.value = null;
    task.value = null;
    queue.value = null;
    pageError.value = "";
  }

  function rememberRun(kind: RunKind, runIdentifier: string): void {
    const projectId = activeProjectId.value || "default";
    const stored = readStoredRuns();
    stored[projectId] = { kind, identifier: runIdentifier };
    writeStorage(PROJECT_RUNS_STORAGE_KEY, JSON.stringify(stored));
    writeStorage(LAST_KIND_STORAGE_KEY, kind === "task" ? "single" : "queue");
    writeStorage(kind === "task" ? TASK_STORAGE_KEY : QUEUE_STORAGE_KEY, runIdentifier);
  }

  function schedulePoll(): void {
    clearPollTimer();
    pollTimer = setTimeout(() => void refreshCurrent(), POLL_INTERVAL_MS);
  }

  async function loadTaskArtifacts(taskId: string): Promise<void> {
    const requests: Promise<void>[] = [];
    if (task.value?.report_url) {
      requests.push(getTaskReport(taskId).then((value) => setRefValue(report, value)));
    } else {
      report.value = "";
    }
    if (task.value?.diff_url) {
      requests.push(getTaskDiff(taskId).then((value) => setRefValue(diff, value)));
    } else {
      diff.value = "";
    }
    await Promise.all(requests);
  }

  async function loadQueueArtifacts(queueId: string): Promise<void> {
    const requests: Promise<void>[] = [];
    if (queue.value?.report_url) {
      requests.push(getQueueReport(queueId).then((value) => setRefValue(queueReport, value)));
    } else {
      queueReport.value = "";
    }
    if (queue.value?.diff_url) {
      requests.push(getQueueDiff(queueId).then((value) => setRefValue(queueDiff, value)));
    } else {
      queueDiff.value = "";
    }
    await Promise.all(requests);
  }

  async function refreshEventsAndLogs(): Promise<void> {
    if (!currentKind.value || !identifier.value) return;
    try {
      const [page, availableLogs] = await Promise.all([
        getEvents(currentKind.value, identifier.value, eventCursor.value),
        getLogs(currentKind.value, identifier.value),
      ]);
      const known = new Set(events.value.map((event) => event.seq));
      events.value.push(...page.items.filter((event) => !known.has(event.seq)));
      eventCursor.value = page.next_cursor;
      logs.value = availableLogs;
    } catch (error) {
      recordError(error, "事件或日志读取失败。");
    }
  }

  function connectEventStream(): void {
    closeEventStream();
    if (
      typeof EventSource === "undefined" ||
      !currentKind.value ||
      !identifier.value ||
      !isRunning.value
    ) return;
    eventSource = new EventSource(
      eventStreamUrl(currentKind.value, identifier.value, eventCursor.value),
    );
    eventSource.onmessage = (message) => {
      try {
        const event = JSON.parse(message.data) as EventRecord;
        if (!events.value.some((item) => item.seq === event.seq)) events.value.push(event);
        eventCursor.value = Math.max(eventCursor.value, event.seq || 0);
      } catch {
        // A malformed line is ignored; the persisted event poll remains authoritative.
      }
    };
    eventSource.addEventListener("end", closeEventStream);
    eventSource.onerror = () => closeEventStream();
  }

  async function refreshTask(taskId: string, remember = false): Promise<void> {
    try {
      const latest = await getTask(taskId);
      task.value = latest;
      pageError.value = "";
      if (remember) rememberRun("task", taskId);
      await Promise.all([loadTaskArtifacts(taskId), refreshEventsAndLogs()]);
      if (shouldPollTask(latest)) {
        schedulePoll();
      } else {
        clearPollTimer();
      }
      if (ACTIVE_TASK_STATUSES.has(latest.status)) {
        if (!eventSource) connectEventStream();
      } else {
        closeEventStream();
      }
    } catch (error) {
      recordError(error, "任务状态读取失败。");
      if (currentKind.value === "task") schedulePoll();
    }
  }

  async function loadQueueTask(taskId: string): Promise<void> {
    try {
      const changed = task.value?.task_id !== taskId;
      task.value = await getTask(taskId);
      if (changed) {
        report.value = "";
        diff.value = "";
      }
      await loadTaskArtifacts(taskId);
    } catch {
      // A queue can be persisted immediately before the child run directory appears.
    }
  }

  async function refreshQueue(queueId: string, remember = false): Promise<void> {
    try {
      const latest = await getQueue(queueId);
      queue.value = latest;
      pageError.value = "";
      if (remember) rememberRun("queue", queueId);
      const requests: Promise<void>[] = [loadQueueArtifacts(queueId), refreshEventsAndLogs()];
      if (latest.current_task_id) requests.push(loadQueueTask(latest.current_task_id));
      await Promise.all(requests);
      if (ACTIVE_QUEUE_STATUSES.has(latest.status)) {
        schedulePoll();
        if (!eventSource) connectEventStream();
      } else {
        clearPollTimer();
        closeEventStream();
      }
    } catch (error) {
      recordError(error, "长任务状态读取失败。");
      if (currentKind.value === "queue") schedulePoll();
    }
  }

  async function refreshCurrent(): Promise<void> {
    if (currentKind.value === "task" && task.value) {
      await refreshTask(task.value.task_id);
    } else if (currentKind.value === "queue" && queue.value) {
      await refreshQueue(queue.value.queue_id);
    }
  }

  async function activateRun(kind: RunKind, runIdentifier: string): Promise<void> {
    clearPollTimer();
    resetArtifacts();
    pageError.value = "";
    currentKind.value = kind;
    if (kind === "task") {
      queue.value = null;
      task.value = null;
      await refreshTask(runIdentifier, true);
    } else {
      task.value = null;
      queue.value = null;
      await refreshQueue(runIdentifier, true);
    }
  }

  async function selectProject(projectId: string, restore = true): Promise<void> {
    if (!projects.value.some((project) => project.project_id === projectId)) return;
    const changed = activeProjectId.value !== projectId;
    activeProjectId.value = projectId;
    writeStorage(PROJECT_STORAGE_KEY, projectId);
    await Promise.all([refreshNotificationSettings(), refreshHarnessStatus()]);
    if (!changed && !restore) return;
    resetRun();
    if (!restore) return;
    const stored = readStoredRuns()[projectId];
    if (stored?.identifier && ["task", "queue"].includes(stored.kind)) {
      await activateRun(stored.kind, stored.identifier);
    }
  }

  async function initialize(): Promise<void> {
    initializing.value = true;
    try {
      await checkHealth();
      projects.value = await getProjects();
      const selected = projects.value.some(
        (project) => project.project_id === activeProjectId.value,
      )
        ? activeProjectId.value
        : projects.value.find((project) => project.is_default)?.project_id ||
          projects.value[0]?.project_id ||
          "";
      if (selected) {
        activeProjectId.value = selected;
        writeStorage(PROJECT_STORAGE_KEY, selected);
      }
      await Promise.all([refreshNotificationSettings(), refreshHarnessStatus()]);
      let stored = selected ? readStoredRuns()[selected] : undefined;
      if (!stored && selected) {
        const legacyKind = readStorage(LAST_KIND_STORAGE_KEY);
        const legacyIdentifier = readStorage(
          legacyKind === "queue" ? QUEUE_STORAGE_KEY : TASK_STORAGE_KEY,
        );
        if (legacyIdentifier) {
          stored = {
            kind: legacyKind === "queue" ? "queue" : "task",
            identifier: legacyIdentifier,
          };
        }
      }
      if (stored) await activateRun(stored.kind, stored.identifier);
      await refreshNotifications();
    } catch (error) {
      recordError(error, "工作台初始化失败。");
    } finally {
      initializing.value = false;
    }
  }

  async function checkHealth(): Promise<void> {
    checkingHealth.value = true;
    try {
      health.value = await getHealth();
      healthError.value = "";
    } catch (error) {
      health.value = null;
      healthError.value = messageFrom(error, "后端服务不可用。");
    } finally {
      checkingHealth.value = false;
    }
  }

  async function refreshHarnessStatus(): Promise<void> {
    const [capabilityResult, metricsResult] = await Promise.allSettled([
      getCapabilities(),
      getMetrics(),
    ]);
    let statusError: unknown = null;
    if (capabilityResult.status === "fulfilled") {
      capabilities.value = capabilityResult.value;
    } else {
      capabilities.value = null;
      statusError = capabilityResult.reason;
    }
    if (metricsResult.status === "fulfilled") {
      metrics.value = metricsResult.value;
    } else {
      metrics.value = null;
      if (statusError === null) statusError = metricsResult.reason;
    }
    harnessError.value = statusError === null
      ? ""
      : messageFrom(statusError, "Harness 能力状态读取失败。");
  }

  async function generatePlan(payload: PlanCreatePayload): Promise<PlanDraft | null> {
    planning.value = true;
    pageError.value = "";
    plan.value = null;
    try {
      plan.value = await createPlan(payload);
      return plan.value;
    } catch (error) {
      recordError(error, "自动规划草稿生成失败；尚未创建任务或工作区。");
      return null;
    } finally {
      planning.value = false;
    }
  }

  async function confirmCurrentPlan(
    reviewer: string,
    editedDraft: PlanDraft,
  ): Promise<boolean> {
    if (!plan.value || plan.value.plan_id !== editedDraft.plan_id) return false;
    confirmingPlan.value = true;
    pageError.value = "";
    try {
      const result = await confirmPlan(plan.value.plan_id, reviewer, editedDraft);
      plan.value = null;
      const target = result.target;
      const targetId = result.target_kind === "task"
        ? (target as TaskData).task_id
        : (target as QueueData).queue_id;
      await activateRun(result.target_kind, targetId);
      return true;
    } catch (error) {
      recordError(error, "Plan 确认失败；未启动新的执行。草稿仍可继续修改。");
      return false;
    } finally {
      confirmingPlan.value = false;
    }
  }

  async function submitTask(payload: TaskCreatePayload): Promise<TaskData | null> {
    submitting.value = true;
    resetRun();
    currentKind.value = "task";
    try {
      const accepted = await createTask(payload);
      task.value = accepted;
      rememberRun("task", accepted.task_id);
      schedulePoll();
      await refreshEventsAndLogs();
      connectEventStream();
      return accepted;
    } catch (error) {
      recordError(error, "任务提交失败。");
      return null;
    } finally {
      submitting.value = false;
    }
  }

  async function submitQueue(payload: QueueCreatePayload): Promise<QueueData | null> {
    submitting.value = true;
    resetRun();
    currentKind.value = "queue";
    try {
      const accepted = await createQueue(payload);
      queue.value = accepted;
      rememberRun("queue", accepted.queue_id);
      schedulePoll();
      await refreshEventsAndLogs();
      connectEventStream();
      return accepted;
    } catch (error) {
      recordError(error, "长任务提交失败。");
      return null;
    } finally {
      submitting.value = false;
    }
  }

  async function runControl(action: "pause" | "cancel" | "resume" | "rerun"): Promise<void> {
    if (!identifier.value || !currentKind.value) return;
    controlling.value = true;
    pageError.value = "";
    try {
      if (currentKind.value === "task" && task.value) {
        const taskId = task.value.task_id;
        const next =
          action === "pause"
            ? await pauseTask(taskId)
            : action === "cancel"
              ? await cancelTask(taskId)
              : action === "resume"
                ? await resumeTask(taskId)
                : await rerunTask(taskId);
        if (action === "rerun") await activateRun("task", next.task_id);
        else {
          task.value = next;
          if (ACTIVE_TASK_STATUSES.has(next.status)) schedulePoll();
        }
      } else if (currentKind.value === "queue" && queue.value) {
        const queueId = queue.value.queue_id;
        const next =
          action === "pause"
            ? await pauseQueue(queueId)
            : action === "cancel"
              ? await cancelQueue(queueId)
              : action === "resume"
                ? await resumeQueue(queueId)
                : await rerunQueue(queueId);
        if (action === "rerun") await activateRun("queue", next.queue_id);
        else {
          queue.value = next;
          if (ACTIVE_QUEUE_STATUSES.has(next.status)) schedulePoll();
        }
      }
      await refreshEventsAndLogs();
    } catch (error) {
      recordError(error, "运行控制失败。");
    } finally {
      controlling.value = false;
    }
  }

  async function submitReview(payload: {
    decision: ReviewDecision;
    reviewer: string;
    comment: string;
    commit_subject: string;
  }): Promise<boolean> {
    if (!task.value) return false;
    reviewing.value = true;
    pageError.value = "";
    try {
      const reviewed = await submitTaskReview(task.value.task_id, {
        ...payload,
        reviewed_diff_sha256: task.value.final_diff_sha256,
      });
      task.value = reviewed;
      if (queue.value) {
        await refreshQueue(queue.value.queue_id);
      } else {
        await Promise.all([
          loadTaskArtifacts(reviewed.task_id),
          refreshEventsAndLogs(),
        ]);
        if (shouldStartDeliveryPoll(reviewed)) schedulePoll();
        else clearPollTimer();
      }
      await refreshNotifications();
      return true;
    } catch (error) {
      recordError(error, "审查提交失败。");
      return false;
    } finally {
      reviewing.value = false;
    }
  }

  async function retryDelivery(kind: "commit" | "archive"): Promise<void> {
    if (!task.value) return;
    controlling.value = true;
    pageError.value = "";
    try {
      const updated = kind === "commit"
        ? await retryTaskCommit(task.value.task_id)
        : await retryTaskArchive(task.value.task_id);
      task.value = updated;
      if (queue.value) {
        await refreshQueue(queue.value.queue_id);
      } else {
        await Promise.all([
          loadTaskArtifacts(updated.task_id),
          refreshEventsAndLogs(),
        ]);
        if (shouldStartDeliveryPoll(updated)) schedulePoll();
        else clearPollTimer();
      }
    } catch (error) {
      recordError(error, kind === "commit" ? "Commit 重试失败。" : "知识归档重试失败。");
    } finally {
      controlling.value = false;
    }
  }

  async function skipSubtask(taskId: string): Promise<void> {
    if (!queue.value) return;
    controlling.value = true;
    try {
      queue.value = await skipQueueSubtask(
        queue.value.queue_id,
        taskId,
        queue.value.updated_at,
      );
      await refreshEventsAndLogs();
    } catch (error) {
      recordError(error, "跳过子任务失败。");
    } finally {
      controlling.value = false;
    }
  }

  async function movePendingSubtask(taskId: string, offset: -1 | 1): Promise<void> {
    if (!queue.value) return;
    const pending = queue.value.subtasks
      .filter((item) => item.status === "pending")
      .sort((left, right) => left.sequence - right.sequence)
      .map((item) => item.task_id);
    const index = pending.indexOf(taskId);
    const target = index + offset;
    if (index < 0 || target < 0 || target >= pending.length) return;
    [pending[index], pending[target]] = [pending[target], pending[index]];
    controlling.value = true;
    try {
      queue.value = await reorderQueue(
        queue.value.queue_id,
        pending,
        queue.value.updated_at,
      );
      await refreshEventsAndLogs();
    } catch (error) {
      recordError(error, "调整子任务顺序失败。");
    } finally {
      controlling.value = false;
    }
  }

  async function selectLog(logId: string): Promise<void> {
    if (!currentKind.value || !identifier.value) return;
    selectedLogId.value = logId;
    try {
      logContent.value = await getLog(currentKind.value, identifier.value, logId);
    } catch (error) {
      recordError(error, "日志正文读取失败。");
    }
  }

  async function refreshNotifications(): Promise<void> {
    try {
      const latest = await getNotifications(activeProjectId.value || undefined);
      if (
        notificationBaselineReady &&
        notificationSettings.value.browser &&
        typeof Notification !== "undefined" &&
        Notification.permission === "granted"
      ) {
        const known = new Set(notifications.value.map((item) => item.notification_id));
        latest
          .filter((item) => item.read_at === null && !known.has(item.notification_id))
          .forEach((item) => new Notification(item.title, { body: item.message }));
      }
      notifications.value = latest;
      notificationBaselineReady = true;
    } catch {
      // Notifications are supplemental and must not block the workbench.
    }
  }

  async function refreshNotificationSettings(): Promise<void> {
    try {
      notificationSettings.value = await getNotificationSettings();
    } catch {
      // Default local preferences remain in effect if settings cannot be read.
    }
  }

  async function updateNotificationPreferences(
    inApp: boolean,
    browser: boolean,
  ): Promise<boolean> {
    try {
      notificationSettings.value = await updateNotificationSettings({
        in_app: inApp,
        browser,
      });
      return true;
    } catch (error) {
      recordError(error, "通知偏好保存失败。");
      return false;
    }
  }

  async function readNotification(notification: NotificationData): Promise<void> {
    if (notification.project_id !== activeProjectId.value) {
      await selectProject(notification.project_id, false);
    }
    if (notification.read_at === null) {
      const updated = await markNotificationRead(notification.notification_id);
      const index = notifications.value.findIndex(
        (item) => item.notification_id === updated.notification_id,
      );
      if (index >= 0) notifications.value[index] = updated;
    }
    await activateRun(notification.kind, notification.identifier);
  }

  function dispose(): void {
    clearPollTimer();
    closeEventStream();
  }

  return {
    projects,
    activeProjectId,
    activeProject,
    currentKind,
    task,
    queue,
    report,
    diff,
    queueReport,
    queueDiff,
    events,
    logs,
    selectedLogId,
    logContent,
    notifications,
    notificationSettings,
    health,
    capabilities,
    metrics,
    harnessError,
    plan,
    healthError,
    checkingHealth,
    connectionState,
    unreadCount,
    pageError,
    submitting,
    planning,
    confirmingPlan,
    reviewing,
    controlling,
    initializing,
    identifier,
    runStatus,
    hasRun,
    isRunning,
    currentTitle,
    needsReview,
    initialize,
    checkHealth,
    refreshHarnessStatus,
    dispose,
    resetRun,
    activateRun,
    selectProject,
    refreshCurrent,
    refreshEventsAndLogs,
    submitTask,
    submitQueue,
    generatePlan,
    confirmCurrentPlan,
    runControl,
    submitReview,
    retryDelivery,
    skipSubtask,
    movePendingSubtask,
    selectLog,
    refreshNotifications,
    refreshNotificationSettings,
    updateNotificationPreferences,
    readNotification,
  };
}

export type Orchestrator = ReturnType<typeof createOrchestrator>;
const ORCHESTRATOR_KEY: InjectionKey<Orchestrator> = Symbol("orchestrator");

export function provideOrchestrator(): Orchestrator {
  const orchestrator = createOrchestrator();
  provide(ORCHESTRATOR_KEY, orchestrator);
  return orchestrator;
}

export function useOrchestrator(): Orchestrator {
  const orchestrator = inject(ORCHESTRATOR_KEY);
  if (!orchestrator) throw new Error("Orchestrator context is unavailable");
  return orchestrator;
}
