import { flushPromises, mount } from "@vue/test-utils";
import { describe, expect, it, vi } from "vitest";

const store = vi.hoisted(() => ({
  task: { value: null as any },
  activeProject: { value: null as any },
  controlling: { value: false },
  publishCurrentTask: vi.fn(),
}));

vi.mock("../src/composables/useOrchestrator", () => ({
  useOrchestrator: () => store,
}));

import PublishView from "../src/views/PublishView.vue";

function task(overrides: Record<string, unknown> = {}) {
  return {
    task_id: "task-1",
    requirement: "Publish a change",
    review_status: "pending",
    delivery_status: "not_ready",
    queue_id: null,
    commit: {},
    publish: {},
    workspace: { task_branch: "codex/task-1" },
    ...overrides,
  };
}

function project(overrides: Record<string, unknown> = {}) {
  return {
    repo_root: "/Users/mon/Documents/Accounting-Software",
    publish_enabled: true,
    publish_remote_url: "https://github.com/example/Accounting-Software.git",
    publish_repository_name: "Accounting-Software",
    ...overrides,
  };
}

function mountView() {
  return mount(PublishView, {
    global: {
      stubs: { RouterLink: { template: "<a><slot /></a>" } },
    },
  });
}

describe("PublishView", () => {
  it("keeps the publish action visible and explains blocked prerequisites", () => {
    store.task.value = task();
    store.activeProject.value = project();

    const wrapper = mountView();

    const button = wrapper.get('[data-test="publish-action"] button');
    expect(button.attributes("disabled")).toBeDefined();
    expect(button.text()).toContain("等待发布条件");
    expect(wrapper.text()).toContain("需要先完成人工审核");
    expect(wrapper.text()).toContain("Accounting-Software");
  });

  it("lets a human click the machine publish operation after all gates pass", async () => {
    store.task.value = task({
      review_status: "approved",
      delivery_status: "archived",
      commit: { commit_sha: "a".repeat(40) },
    });
    store.activeProject.value = project();
    store.publishCurrentTask.mockResolvedValue(true);

    const wrapper = mountView();
    await wrapper.get('[data-test="publish-action"] input').setValue("Reviewer");
    await wrapper.get('[data-test="publish-action"] input[type="checkbox"]').setValue(true);
    await flushPromises();
    await wrapper.get('[data-test="publish-action"] button').trigger("click");

    expect(store.publishCurrentTask).toHaveBeenCalledWith("Reviewer");
  });
});
