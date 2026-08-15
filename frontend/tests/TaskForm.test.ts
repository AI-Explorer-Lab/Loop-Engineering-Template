import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";

import TaskForm from "../src/components/TaskForm.vue";


describe("TaskForm", () => {
  it("normalizes one requirement and newline-separated acceptance criteria", async () => {
    const wrapper = mount(TaskForm);

    await wrapper.get('[data-test="requirement"]').setValue("  Add filtering  ");
    await wrapper.get('[data-test="criteria"]').setValue(
      "1.  Filters rows  \n2. Keeps old behavior",
    );
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

  it("rejects content without any acceptance criterion", async () => {
    const wrapper = mount(TaskForm);

    await wrapper.get('[data-test="requirement"]').setValue("A requirement");
    await wrapper.get('[data-test="criteria"]').setValue("\n");
    await wrapper.get('[data-test="task-form"]').trigger("submit");
    expect(wrapper.get('[role="alert"]').text()).toBe(
      "每条验收标准都需要填写。",
    );
  });

  it("adds the next markdown number when pressing Enter", async () => {
    const wrapper = mount(TaskForm);
    const criteria = wrapper.get('[data-test="criteria"]');

    await criteria.setValue("1. First result");
    await criteria.trigger("keydown", { key: "Enter" });

    expect((criteria.element as HTMLTextAreaElement).value).toBe(
      "1. First result\n2. ",
    );
  });
});
