import { flushPromises, mount } from "@vue/test-utils";
import { createMemoryHistory, createRouter } from "vue-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const taskApi = vi.hoisted(() => ({
  createTask: vi.fn(),
  getTask: vi.fn(),
  getTaskDiff: vi.fn(),
  getTaskReport: vi.fn(),
  submitTaskReview: vi.fn(),
  retryTaskCommit: vi.fn(),
  retryTaskArchive: vi.fn(),
  pauseTask: vi.fn(),
  cancelTask: vi.fn(),
  rerunTask: vi.fn(),
  resumeTask: vi.fn(),
}));
const planApi = vi.hoisted(() => ({
  createPlan: vi.fn(),
  getPlan: vi.fn(),
  confirmPlan: vi.fn(),
}));
const queueApi = vi.hoisted(() => ({
  createQueue: vi.fn(),
  getQueue: vi.fn(),
  getQueueDiff: vi.fn(),
  getQueueReport: vi.fn(),
  pauseQueue: vi.fn(),
  cancelQueue: vi.fn(),
  rerunQueue: vi.fn(),
  resumeQueue: vi.fn(),
  skipQueueSubtask: vi.fn(),
  reorderQueue: vi.fn(),
}));
const platformApi = vi.hoisted(() => ({
  getProjects: vi.fn(),
  getHistory: vi.fn(),
  getEvents: vi.fn(),
  eventStreamUrl: vi.fn(),
  getLogs: vi.fn(),
  getLog: vi.fn(),
  getNotifications: vi.fn(),
  markNotificationRead: vi.fn(),
  getNotificationSettings: vi.fn(),
  updateNotificationSettings: vi.fn(),
  getCapabilities: vi.fn(),
  getMetrics: vi.fn(),
}));
const healthApi = vi.hoisted(() => ({ getHealth: vi.fn() }));

vi.mock("../src/api/tasks", () => taskApi);
vi.mock("../src/api/queues", () => queueApi);
vi.mock("../src/api/plans", () => planApi);
vi.mock("../src/api/platform", () => platformApi);
vi.mock("../src/api/health", () => healthApi);

import App from "../src/App.vue";
import { deliveryProgressFor } from "../src/composables/useOrchestrator";
import { routes } from "../src/router";
import type { PlanDraft, QueueData, TaskData } from "../src/types/task";

function task(status: TaskData["status"], overrides: Partial<TaskData> = {}): TaskData {
  return {
    task_id: "task-1",
    requirement: "Add filtering",
    acceptance_criteria: ["Filtering works"],
    status,
    schema_version: 1,
    legacy: false,
    history_warning: null,
    machine_status: status,
    review_status: "pending",
    delivery_status: "not_ready",
    phase: status === "running" ? "validating" : "completed",
    thread_id: "thread-1",
    turn_count: 1,
    failure_count: 0,
    cycle_turn_count: 1,
    cycle_failure_count: 0,
    rounds: [],
    last_error_summary: "",
    infrastructure_error: null,
    started_at: "2026-07-15T08:00:00+08:00",
    updated_at: "2026-07-15T08:00:01+08:00",
    finished_at: status === "success" ? "2026-07-15T08:00:02+08:00" : null,
    report_url: status === "success" ? "/api/tasks/task-1/report" : null,
    diff_url: status === "success" ? "/api/tasks/task-1/diff" : null,
    workspace: { base_commit: "a".repeat(40), task_branch: "codex/task-1", worktree: ".codex-orchestrator/worktrees/task-1" },
    permissions: { effective: { verified: true, network: "disabled" } },
    audit_summary: { event_count: 12, denied_event_count: 0 },
    changed_files: [{ path: "src/filter.ts", status: "modified", additions: 4, deletions: 1 }],
    codex_responses: [{ turn_number: 1, response: "Implemented." }],
    final_diff_sha256: "b".repeat(64),
    diff_redaction_count: 0,
    review: null,
    review_history: [],
    context: {},
    evaluations: {},
    commit: {},
    archive: {},
    queue_id: null,
    sequence: null,
    rerun_of: null,
    ...overrides,
  };
}

function taskQueue(status: QueueData["status"]): QueueData {
  return {
    queue_id: "queue-1",
    name: "交易管理",
    status,
    base_ref: "HEAD",
    base_commit: "a".repeat(40),
    current_task_id: status === "pending" ? null : "queue-1-task-01",
    cumulative_diff_sha256: "",
    last_error_summary: "",
    delivery_status: "not_ready",
    subtasks: [
      { task_id: "queue-1-task-01", sequence: 1, requirement: "新增交易", acceptance_criteria: ["可以新增"], status: status === "waiting_review" ? "waiting_review" : "pending", machine_status: status === "waiting_review" ? "success" : null, review_status: "pending", delivery_status: "not_ready", thread_id: status === "waiting_review" ? "thread-1" : null, last_error_summary: "", updated_at: "2026-07-17T08:00:00+08:00" },
      { task_id: "queue-1-task-02", sequence: 2, requirement: "交易列表", acceptance_criteria: ["可以查看"], status: "pending", machine_status: null, review_status: "pending", delivery_status: "not_ready", thread_id: null, last_error_summary: "", updated_at: "2026-07-17T08:00:00+08:00" },
    ],
    started_at: "2026-07-17T08:00:00+08:00",
    updated_at: "2026-07-17T08:00:00+08:00",
    finished_at: null,
    report_url: status === "waiting_review" ? "/api/queues/queue-1/report" : null,
    diff_url: null,
    rerun_of: null,
  };
}

function planDraft(): PlanDraft {
  return {
    schema_version: 1,
    plan_id: "plan-1234567890abcdef",
    name: "Filtering plan",
    source_requirement_sha256: "c".repeat(64),
    context_sha256: "d".repeat(64),
    acceptance_criteria: { "AC-001": "Filtering works" },
    status: "ready",
    execution_mode: "single",
    subtasks: [{
      sequence: 1,
      title: "Add filtering",
      requirement_slice: "Add filtering",
      source_acceptance_ids: ["AC-001"],
    }],
    unassigned_acceptance_ids: [],
    warnings: [],
    planner_thread_id: "thread-plan",
    created_at: "2026-07-18T08:00:00+08:00",
  };
}

async function mountAt(path: string) {
  const router = createRouter({ history: createMemoryHistory(), routes });
  await router.push(path);
  await router.isReady();
  const wrapper = mount(App, { global: { plugins: [router] } });
  await flushPromises();
  return { wrapper, router };
}

describe("App workbench", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    localStorage.clear();
    Object.values(taskApi).forEach((mock) => mock.mockReset());
    Object.values(queueApi).forEach((mock) => mock.mockReset());
    Object.values(planApi).forEach((mock) => mock.mockReset());
    Object.values(platformApi).forEach((mock) => mock.mockReset());
    healthApi.getHealth.mockReset();
    healthApi.getHealth.mockResolvedValue({ status: "ok", environment: "test", version: "0.1.0" });
    platformApi.getProjects.mockResolvedValue([
      { project_id: "default", name: "Accounting-Software", repo_root: "/repo", is_default: true, active_identifier: null, knowledge_actor_id: "zhangsan" },
    ]);
    platformApi.getNotifications.mockResolvedValue([]);
    platformApi.getEvents.mockResolvedValue({ items: [], next_cursor: 0, terminal: false });
    platformApi.getLogs.mockResolvedValue([]);
    platformApi.eventStreamUrl.mockReturnValue("/events");
    platformApi.getNotificationSettings.mockResolvedValue({ in_app: true, browser: true, email_configured: false, webhook_configured: false });
    platformApi.updateNotificationSettings.mockResolvedValue({ in_app: true, browser: true, email_configured: false, webhook_configured: false });
    platformApi.getCapabilities.mockResolvedValue({ status: "healthy", project_id: "default", knowledge_base_path: "/knowledge", mcp_registry: "/mcp/registry.json", mcp_read: { transport: "stdio", network: "disabled" }, mcp_archive: {}, skill_count: 1, archive_backlog: 0, knowledge_actor_id: "zhangsan", checked_at: "2026-07-18T08:00:00+08:00" });
    platformApi.getMetrics.mockResolvedValue({ project_id: "default", task_success_rate: null, completed_tasks: 0, layer_failure_counts: {}, repair_rounds: 0, knowledge_hit_rate: null, planned_tasks: 0, planner_manual_edit_count: 0, commit_success_rate: null, archive_backlog: 0 });
    taskApi.getTaskDiff.mockResolvedValue("diff --git a/src/filter.ts b/src/filter.ts\n--- a/src/filter.ts\n+++ b/src/filter.ts\n+export const filter = true;");
    taskApi.getTaskReport.mockResolvedValue("# Final report");
    queueApi.getQueueDiff.mockResolvedValue("");
    queueApi.getQueueReport.mockResolvedValue("# Queue report");
  });

  afterEach(() => vi.useRealTimers());

  it("submits a task and polls it to a completed monitor state", async () => {
    taskApi.createTask.mockResolvedValue(task("accepted"));
    taskApi.getTask.mockResolvedValue(task("success"));
    const { wrapper, router } = await mountAt("/create");

    await wrapper.get('[data-test="requirement"]').setValue("Add filtering");
    await wrapper.get('[data-test="criterion-0"]').setValue("Filtering works");
    await wrapper.get('[data-test="task-form"]').trigger("submit");
    await flushPromises();

    expect(taskApi.createTask).toHaveBeenCalledWith({ requirement: "Add filtering", acceptance_criteria: ["Filtering works"] });
    expect(router.currentRoute.value.path).toBe("/monitor");
    expect(wrapper.get('[data-test="task-status"]').text()).toContain("已接收");
    expect(localStorage.getItem("codex-orchestrator:last-task-id")).toBe("task-1");

    await vi.advanceTimersByTimeAsync(2_000);
    await flushPromises();
    expect(taskApi.getTask).toHaveBeenCalledWith("task-1");
    expect(wrapper.get('[data-test="task-status"]').text()).toContain("机器验证通过");
    wrapper.unmount();
  });

  it("restores a legacy storage reference and shows the persisted diff", async () => {
    localStorage.setItem("codex-orchestrator:last-task-id", "task-1");
    taskApi.getTask.mockResolvedValue(task("success"));
    const { wrapper } = await mountAt("/changes");

    expect(taskApi.getTask).toHaveBeenCalledWith("task-1");
    expect(wrapper.get('[data-test="diff-viewer"]').text()).toContain("export const filter = true");
    expect(wrapper.text()).toContain("src/filter.ts");
    wrapper.unmount();
  });

  it("submits an ordered queue and opens its current review subtask", async () => {
    queueApi.createQueue.mockResolvedValue(taskQueue("pending"));
    queueApi.getQueue.mockResolvedValue(taskQueue("waiting_review"));
    taskApi.getTask.mockResolvedValue(task("success", { task_id: "queue-1-task-01", requirement: "新增交易", acceptance_criteria: ["可以新增"], queue_id: "queue-1", sequence: 1 }));
    const { wrapper } = await mountAt("/create");

    await wrapper.get('[data-test="queue-mode"]').trigger("click");
    await wrapper.get('[data-test="queue-name"]').setValue("交易管理");
    await wrapper.get('[data-test="subtask-requirement-0"]').setValue("新增交易");
    await wrapper.get('[data-test="subtask-0-criterion-0"]').setValue("可以新增");
    await wrapper.get('[data-test="subtask-requirement-1"]').setValue("交易列表");
    await wrapper.get('[data-test="subtask-1-criterion-0"]').setValue("可以查看");
    await wrapper.get('[data-test="queue-form"]').trigger("submit");
    await flushPromises();
    await vi.advanceTimersByTimeAsync(2_000);
    await flushPromises();

    expect(queueApi.createQueue).toHaveBeenCalledWith({ name: "交易管理", subtasks: [
      { requirement: "新增交易", acceptance_criteria: ["可以新增"] },
      { requirement: "交易列表", acceptance_criteria: ["可以查看"] },
    ] });
    expect(wrapper.get('[data-test="queue-progress"]').text()).toContain("等待人工审核");
    expect(wrapper.get('[data-test="task-status"]').text()).toContain("新增交易");
    wrapper.unmount();
  });

  it("previews an automatic plan before explicit confirmation starts a task", async () => {
    const draft = planDraft();
    planApi.createPlan.mockResolvedValue(draft);
    planApi.confirmPlan.mockResolvedValue({
      plan_id: draft.plan_id,
      target_kind: "task",
      target: task("accepted"),
      confirmation: { reviewer: "Planner Reviewer" },
    });
    taskApi.getTask.mockResolvedValue(task("accepted"));
    const { wrapper, router } = await mountAt("/create");

    await wrapper.get('[data-test="auto-mode"]').trigger("click");
    await wrapper.get('[data-test="plan-name"]').setValue("Filtering plan");
    await wrapper.get('[data-test="plan-requirement"]').setValue("Add filtering");
    await wrapper.get('[data-test="plan-criterion-0"]').setValue("Filtering works");
    await wrapper.get('[data-test="plan-form"]').trigger("submit");
    await flushPromises();

    expect(planApi.createPlan).toHaveBeenCalledWith({
      name: "Filtering plan",
      requirement: "Add filtering",
      acceptance_criteria: ["Filtering works"],
    });
    expect(taskApi.createTask).not.toHaveBeenCalled();
    expect(wrapper.get('[data-test="plan-preview"]').text()).toContain("AC-001");

    await wrapper.get('[data-test="plan-reviewer"]').setValue("Planner Reviewer");
    await wrapper.get('[data-test="confirm-plan"]').trigger("click");
    await flushPromises();

    expect(planApi.confirmPlan).toHaveBeenCalledWith(
      draft.plan_id,
      "Planner Reviewer",
      expect.objectContaining({ status: "ready", execution_mode: "single" }),
    );
    expect(router.currentRoute.value.path).toBe("/monitor");
    wrapper.unmount();
  });

  it("confirms an immutable review bound to the displayed diff", async () => {
    localStorage.setItem("codex-orchestrator:last-task-id", "task-1");
    taskApi.getTask.mockResolvedValue(task("success"));
    taskApi.submitTaskReview.mockResolvedValue(task("success", { review_status: "approved", review: { decision: "approved", reviewer: "Local Reviewer", comment: "Checked.", reviewed_diff_sha256: "b".repeat(64) } }));
    const { wrapper } = await mountAt("/review");

    await wrapper.get('[data-test="reviewer"]').setValue("Local Reviewer");
    await wrapper.get('[data-test="review-comment"]').setValue("Checked.");
    await wrapper.get('[data-test="approve"]').trigger("click");
    await wrapper.get('[data-test="confirm-review"]').trigger("click");
    await flushPromises();

    expect(taskApi.submitTaskReview).toHaveBeenCalledWith("task-1", {
      decision: "approved", reviewer: "Local Reviewer", comment: "Checked.", commit_subject: "Add filtering", reviewed_diff_sha256: "b".repeat(64),
    });
    expect(wrapper.get('[data-test="review-panel"]').text()).toContain("Local Reviewer");
    wrapper.unmount();
  });

  it("polls an approved delivery until archiving reaches a terminal state", async () => {
    localStorage.setItem("codex-orchestrator:last-task-id", "task-1");
    const review = {
      decision: "approved",
      reviewer: "Local Reviewer",
      comment: "Checked.",
      commit_subject: "Add filtering",
      reviewed_diff_sha256: "b".repeat(64),
    };
    taskApi.getTask
      .mockResolvedValueOnce(task("success"))
      .mockResolvedValueOnce(task("success", {
        review_status: "approved",
        delivery_status: "archive_pending",
        review,
        commit: {
          status: "committed",
          commit_sha: "c".repeat(40),
          subject: "Add filtering",
        },
        archive: {
          summary: { delivery_status: "archive_pending" },
          outbox: {
            status: "pending",
            items: [
              { status: "completed" },
              { status: "pending" },
            ],
          },
        },
      }))
      .mockResolvedValueOnce(task("success", {
        review_status: "approved",
        delivery_status: "archived",
        review,
        commit: {
          status: "committed",
          commit_sha: "c".repeat(40),
          subject: "Add filtering",
        },
        archive: {
          summary: { delivery_status: "archived" },
          outbox: {
            status: "completed",
            items: [
              { status: "completed" },
              { status: "completed" },
            ],
          },
        },
      }));
    taskApi.submitTaskReview.mockResolvedValue(task("success", {
      review_status: "approved",
      delivery_status: "archive_pending",
      review,
      commit: {
        status: "committed",
        commit_sha: "c".repeat(40),
        subject: "Add filtering",
      },
    }));
    const { wrapper } = await mountAt("/review");

    await wrapper.get('[data-test="reviewer"]').setValue("Local Reviewer");
    await wrapper.get('[data-test="review-comment"]').setValue("Checked.");
    await wrapper.get('[data-test="approve"]').trigger("click");
    await wrapper.get('[data-test="confirm-review"]').trigger("click");
    await flushPromises();

    expect(wrapper.get('[data-test="review-panel"]').text()).toContain("审批已记录");
    expect(wrapper.get('[data-test="review-panel"]').text()).toContain("Commit 已完成");
    expect(wrapper.get('[data-test="review-panel"]').text()).toContain("Archiver 正在生成归档总结");

    await vi.advanceTimersByTimeAsync(2_000);
    await flushPromises();
    expect(taskApi.getTask).toHaveBeenCalledTimes(2);
    expect(wrapper.get('[data-test="review-panel"]').text()).toContain("Archiver 正在写入 Knowledge");
    expect(wrapper.get('[data-test="review-panel"]').text()).toContain("Knowledge 1/2");

    await vi.advanceTimersByTimeAsync(2_000);
    await flushPromises();
    expect(taskApi.getTask).toHaveBeenCalledTimes(3);
    expect(wrapper.get('[data-test="review-panel"]').text()).toContain("归档完成");
    expect(wrapper.get('[data-test="review-panel"]').text()).toContain("Knowledge 2/2");

    await vi.advanceTimersByTimeAsync(4_000);
    expect(taskApi.getTask).toHaveBeenCalledTimes(3);
    wrapper.unmount();
  });

  it("treats committed as terminal when the project has no Archiver even if metrics fail", async () => {
    localStorage.setItem("codex-orchestrator:last-task-id", "task-1");
    platformApi.getCapabilities.mockResolvedValue({
      status: "unavailable",
      project_id: "default",
      reason: "harness feature is disabled",
    });
    platformApi.getMetrics.mockRejectedValue(new Error("metrics unavailable"));
    const committed = task("success", {
      review_status: "approved",
      delivery_status: "committed",
      review: {
        decision: "approved",
        reviewer: "Local Reviewer",
        comment: "Checked.",
        commit_subject: "Add filtering",
        reviewed_diff_sha256: "b".repeat(64),
      },
      commit: {
        status: "committed",
        commit_sha: "c".repeat(40),
      },
    });
    taskApi.getTask
      .mockResolvedValueOnce(task("success"))
      .mockResolvedValue(committed);
    taskApi.submitTaskReview.mockResolvedValue(committed);

    const { wrapper } = await mountAt("/review");
    await wrapper.get('[data-test="reviewer"]').setValue("Local Reviewer");
    await wrapper.get('[data-test="review-comment"]').setValue("Checked.");
    await wrapper.get('[data-test="approve"]').trigger("click");
    await wrapper.get('[data-test="confirm-review"]').trigger("click");
    await flushPromises();
    expect(wrapper.get('[data-test="review-panel"]').text()).toContain(
      "Archiver 未启用，Commit 为交付终态",
    );

    await vi.advanceTimersByTimeAsync(2_000);
    await flushPromises();
    expect(taskApi.getTask).toHaveBeenCalledTimes(2);

    await vi.advanceTimersByTimeAsync(4_000);
    expect(taskApi.getTask).toHaveBeenCalledTimes(2);
    wrapper.unmount();
  });

  it("renders persisted archive state and reports unknown capability honestly", () => {
    const pending = task("success", {
      review_status: "approved",
      delivery_status: "archive_pending",
      review: { decision: "approved" },
      commit: {
        status: "committed",
        commit_sha: "c".repeat(40),
      },
    });
    expect(deliveryProgressFor(pending, true).archive).toBe(
      "Archiver 正在生成归档总结",
    );

    const committed = task("success", {
      review_status: "approved",
      delivery_status: "committed",
      review: { decision: "approved" },
      commit: {
        status: "committed",
        commit_sha: "c".repeat(40),
      },
    });
    expect(deliveryProgressFor(committed, null).archive).toBe(
      "Archiver 状态待确认，Commit 已完成",
    );
  });

  it("stops delivery polling when Archiver reports a failure", async () => {
    localStorage.setItem("codex-orchestrator:last-task-id", "task-1");
    const committed = {
      status: "committed",
      commit_sha: "c".repeat(40),
    };
    taskApi.getTask
      .mockResolvedValueOnce(task("success", {
        review_status: "approved",
        delivery_status: "archive_pending",
        review: { decision: "approved" },
        commit: committed,
      }))
      .mockResolvedValueOnce(task("success", {
        review_status: "approved",
        delivery_status: "failed",
        review: { decision: "approved" },
        commit: committed,
        archive: {
          outbox: {
            status: "failed",
            items: [
              { status: "completed" },
              { status: "failed" },
            ],
          },
        },
      }));

    const { wrapper } = await mountAt("/monitor");
    await vi.advanceTimersByTimeAsync(2_000);
    await flushPromises();
    expect(taskApi.getTask).toHaveBeenCalledTimes(2);
    expect(wrapper.get('[data-test="delivery-progress"]').text()).toContain(
      "Archiver 写入知识失败",
    );

    await vi.advanceTimersByTimeAsync(4_000);
    expect(taskApi.getTask).toHaveBeenCalledTimes(2);
    wrapper.unmount();
  });

  it("does not let audit events invent a delivery state", async () => {
    localStorage.setItem("codex-orchestrator:last-task-id", "task-1");
    taskApi.getTask.mockResolvedValue(task("success"));
    platformApi.getEvents.mockResolvedValue({
      items: [
        {
          seq: 1,
          type: "review.recorded",
          timestamp: "2026-07-15T08:00:02+08:00",
          payload: { decision: "approved" },
        },
        {
          seq: 2,
          type: "commit.completed",
          timestamp: "2026-07-15T08:00:03+08:00",
        },
        {
          seq: 3,
          type: "archive.queued",
          timestamp: "2026-07-15T08:00:04+08:00",
        },
      ],
      next_cursor: 3,
      terminal: true,
    });

    const { wrapper } = await mountAt("/monitor");
    expect(wrapper.find('[data-test="delivery-progress"]').exists()).toBe(false);
    wrapper.unmount();
  });

  it("reopens queue review controls while preserving an earlier change request", async () => {
    localStorage.setItem("codex-orchestrator:last-queue-id", "queue-1");
    localStorage.setItem("codex-orchestrator:last-kind", "queue");
    queueApi.getQueue.mockResolvedValue(taskQueue("waiting_review"));
    taskApi.getTask.mockResolvedValue(task("success", {
      task_id: "queue-1-task-01",
      requirement: "新增交易",
      acceptance_criteria: ["可以新增"],
      queue_id: "queue-1",
      sequence: 1,
      review_status: "pending",
      review: {
        decision: "changes_requested",
        reviewer: "Reviewer",
        comment: "Please revise it.",
        reviewed_diff_sha256: "b".repeat(64),
      },
      review_history: [{
        review_number: 1,
        decision: "changes_requested",
        reviewer: "Reviewer",
        comment: "Please revise it.",
        reviewed_diff_sha256: "b".repeat(64),
      }],
    }));

    const { wrapper } = await mountAt("/review");

    expect(wrapper.find('[data-test="reviewer"]').exists()).toBe(true);
    expect(wrapper.get('[data-test="review-panel"]').text()).toContain("第 1 次 · changes_requested");
    wrapper.unmount();
  });

  it("requests a cooperative pause from the monitor", async () => {
    localStorage.setItem("codex-orchestrator:last-task-id", "task-1");
    taskApi.getTask.mockResolvedValue(task("running"));
    taskApi.pauseTask.mockResolvedValue(task("pausing"));
    const { wrapper } = await mountAt("/monitor");

    const pause = wrapper.findAll("button").find((button) => button.text() === "暂停");
    expect(pause).toBeDefined();
    await pause!.trigger("click");
    await flushPromises();
    expect(taskApi.pauseTask).toHaveBeenCalledWith("task-1");
    expect(wrapper.get('[data-test="task-status"]').text()).toContain("正在暂停");
    wrapper.unmount();
  });

  it("does not invent review data for legacy records", async () => {
    localStorage.setItem("codex-orchestrator:last-task-id", "task-1");
    taskApi.getTask.mockResolvedValue(task("success", { schema_version: 0, legacy: true, history_warning: "历史记录不完整。", review_status: "unavailable", workspace: {}, permissions: {}, diff_url: null }));
    const { wrapper } = await mountAt("/review");
    expect(wrapper.text()).toContain("这是一条旧版记录");
    expect(wrapper.find('[data-test="review-panel"]').exists()).toBe(false);
    wrapper.unmount();
  });

  it("filters cross-project history and opens the selected record", async () => {
    platformApi.getHistory.mockResolvedValue({
      items: [{
        kind: "task",
        identifier: "task-1",
        project_id: "default",
        project_name: "Accounting-Software",
        title: "Add filtering",
        status: "success",
        review_status: "pending",
        started_at: "2026-07-15T08:00:00+08:00",
        updated_at: "2026-07-15T08:00:02+08:00",
        finished_at: "2026-07-15T08:00:02+08:00",
        current_task_id: null,
        delivery_status: "not_ready",
      }],
      page: 1,
      page_size: 20,
      total: 1,
      pages: 1,
    });
    taskApi.getTask.mockResolvedValue(task("success"));
    const { wrapper, router } = await mountAt("/history");

    await wrapper.get(".search-field input").setValue("filtering");
    await wrapper.get(".filter-bar").trigger("submit");
    await flushPromises();
    expect(platformApi.getHistory).toHaveBeenLastCalledWith(expect.objectContaining({ query: "filtering" }));

    await wrapper.get(".history-table > button").trigger("click");
    await flushPromises();
    expect(router.currentRoute.value.path).toBe("/monitor");
    expect(wrapper.get('[data-test="task-status"]').text()).toContain("Add filtering");
    wrapper.unmount();
  });

  it("persists the in-app notification preference", async () => {
    platformApi.updateNotificationSettings.mockResolvedValue({
      in_app: false,
      browser: true,
      email_configured: false,
      webhook_configured: false,
    });
    const { wrapper } = await mountAt("/settings");

    await wrapper.get('button[role="switch"]').trigger("click");
    await flushPromises();
    expect(platformApi.updateNotificationSettings).toHaveBeenCalledWith({
      in_app: false,
      browser: true,
    });
    wrapper.unmount();
  });
});
