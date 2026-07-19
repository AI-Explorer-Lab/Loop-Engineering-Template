import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";

import QueueForm from "../src/components/QueueForm.vue";


describe("QueueForm", () => {
  it("submits at least two normalized subtasks in displayed order", async () => {
    const wrapper = mount(QueueForm);

    await wrapper.get('[data-test="queue-name"]').setValue(" 交易管理 ");
    await wrapper.get('[data-test="subtask-requirement-0"]').setValue(" 新增交易 ");
    await wrapper.get('[data-test="subtask-0-criterion-0"]').setValue(" 可以新增 ");
    await wrapper.get('[data-test="subtask-requirement-1"]').setValue("交易列表");
    await wrapper.get('[data-test="subtask-1-criterion-0"]').setValue("可以查看");
    await wrapper.get('[data-test="queue-form"]').trigger("submit");

    expect(wrapper.emitted("submit")?.[0]?.[0]).toEqual({
      name: "交易管理",
      subtasks: [
        { requirement: "新增交易", acceptance_criteria: ["可以新增"] },
        { requirement: "交易列表", acceptance_criteria: ["可以查看"] },
      ],
    });
  });

  it("adds, removes, and reorders cards while regenerating their sequence", async () => {
    const wrapper = mount(QueueForm);
    await wrapper.get('[data-test="subtask-requirement-0"]').setValue("First");
    await wrapper.get('[data-test="subtask-requirement-1"]').setValue("Second");
    await wrapper.get('[data-test="add-subtask"]').trigger("click");
    await wrapper.get('[data-test="subtask-requirement-2"]').setValue("Third");

    await wrapper.get('[aria-label="上移子任务 3"]').trigger("click");
    expect(
      (wrapper.get('[data-test="subtask-requirement-1"]').element as HTMLTextAreaElement).value,
    ).toBe("Third");

    await wrapper.get('[aria-label="删除子任务 2"]').trigger("click");
    expect(wrapper.find('[data-test="subtask-2"]').exists()).toBe(false);
    expect(
      (wrapper.get('[data-test="subtask-requirement-1"]').element as HTMLTextAreaElement).value,
    ).toBe("Second");
  });

  it("requires a name, a requirement, and acceptance criteria", async () => {
    const wrapper = mount(QueueForm);
    await wrapper.get('[data-test="queue-form"]').trigger("submit");
    expect(wrapper.get('[role="alert"]').text()).toBe("请填写长任务名称。");

    await wrapper.get('[data-test="queue-name"]').setValue("交易管理");
    await wrapper.get('[data-test="queue-form"]').trigger("submit");
    expect(wrapper.get('[role="alert"]').text()).toBe("每个子任务都需要填写需求。");
  });
});
