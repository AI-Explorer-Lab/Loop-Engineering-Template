import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";

import TaskForm from "../src/components/TaskForm.vue";


describe("TaskForm", () => {
  it("normalizes one requirement and multiple acceptance criteria", async () => {
    const wrapper = mount(TaskForm);

    await wrapper.get('[data-test="requirement"]').setValue("  Add filtering  ");
    await wrapper.get('[data-test="criterion-0"]').setValue("  Filters rows  ");
    await wrapper.get('[data-test="add-criterion"]').trigger("click");
    await wrapper.get('[data-test="criterion-1"]').setValue("Keeps old behavior");
    await wrapper.get('[data-test="task-form"]').trigger("submit");

    expect(wrapper.emitted("submit")?.[0]?.[0]).toEqual({
      requirement: "Add filtering",
      acceptance_criteria: ["Filters rows", "Keeps old behavior"],
    });
  });

  it("requires a concrete value in every field", async () => {
    const wrapper = mount(TaskForm);

    await wrapper.get('[data-test="task-form"]').trigger("submit");
    expect(wrapper.get('[role="alert"]').text()).toBe("请填写功能需求。");

    await wrapper.get('[data-test="requirement"]').setValue("Add filtering");
    await wrapper.get('[data-test="task-form"]').trigger("submit");
    expect(wrapper.get('[role="alert"]').text()).toBe(
      "每条验收标准都需要填写。",
    );
    expect(wrapper.emitted("submit")).toBeUndefined();
  });

  it("removes an acceptance criterion while keeping at least one", async () => {
    const wrapper = mount(TaskForm);

    await wrapper.get('[data-test="criterion-0"]').setValue("First result");
    await wrapper.get('[data-test="add-criterion"]').trigger("click");
    await wrapper.get('[data-test="criterion-1"]').setValue("Second result");
    await wrapper.get('[aria-label="删除验收标准 1"]').trigger("click");

    expect(wrapper.find('[data-test="criterion-1"]').exists()).toBe(false);
    expect(wrapper.get('[data-test="criterion-0"]').element).toHaveProperty(
      "value",
      "Second result",
    );
    expect(
      wrapper.get('[aria-label="删除验收标准 1"]').attributes("disabled"),
    ).toBeDefined();
  });
});
