import { mount } from "@vue/test-utils";
import { describe, expect, it, vi } from "vitest";

vi.mock("../src/composables/useOrchestrator", () => ({
  useOrchestrator: () => ({
    eventIntegrity: { value: { status: "valid" } },
  }),
}));

import EventTimeline from "../src/components/EventTimeline.vue";

describe("EventTimeline", () => {
  it("shows readable progress and keeps protocol events in debug details", () => {
    const wrapper = mount(EventTimeline, {
      props: {
        events: [
          {
            seq: 1,
            type: "run.started",
            timestamp: "2026-08-13T09:08:00+08:00",
            payload: {},
          },
          {
            seq: 2,
            type: "command.completed",
            timestamp: "2026-08-13T09:08:01+08:00",
            payload: {
              command: ["python3", "-m", "unittest", "discover", "-s", "tests", "-v"],
              duration_seconds: 0.781,
              exit_code: 0,
              log_path: "logs/codex/turn-01-command-01.log",
            },
          },
          {
            seq: 3,
            type: "file.changed",
            timestamp: "2026-08-13T09:08:02+08:00",
            payload: {
              item: { changes: [{ path: "note.py" }] },
            },
          },
          {
            seq: 4,
            type: "codex.item.completed",
            timestamp: "2026-08-13T09:08:02+08:00",
            payload: { data: { item: { type: "commandExecution" } } },
          },
          {
            seq: 5,
            type: "codex.item.unknown",
            timestamp: "2026-08-13T09:08:02+08:00",
            payload: { data: { delta: "internal stream fragment" } },
          },
        ],
      },
    });

    expect(wrapper.findAll(".event-row")).toHaveLength(3);
    expect(wrapper.findAll(".event-row strong").map((node) => node.text())).toEqual([
      "代码已更新",
      "测试通过",
      "任务启动",
    ]);
    expect(wrapper.get(".event-list").text()).toContain("耗时 0.78 秒");
    expect(wrapper.get(".event-list").text()).toContain("修改文件：note.py");
    expect(wrapper.get(".timeline-debug").text()).toContain("查看隐藏的内部记录（2 条）");
    expect(wrapper.get(".timeline-debug").text()).toContain("codex.item.unknown");
    expect(wrapper.get(".timeline-debug").text()).toContain("internal stream fragment");
  });

  it("groups MCP calls by the following frozen context stage", () => {
    const wrapper = mount(EventTimeline, {
      props: {
        events: [
          {
            seq: 1,
            type: "mcp.tool_completed",
            timestamp: "2026-08-13T09:08:00+08:00",
            payload: { tool: "knowledge_catalog", mode: "read" },
          },
          {
            seq: 2,
            type: "mcp.tool_completed",
            timestamp: "2026-08-13T09:08:01+08:00",
            payload: { tool: "knowledge_search", mode: "read" },
          },
          {
            seq: 3,
            type: "context.assembled",
            timestamp: "2026-08-13T09:08:02+08:00",
            payload: { stage: "generation" },
          },
        ],
      },
    });

    expect(wrapper.findAll(".event-row")).toHaveLength(2);
    expect(wrapper.get(".event-list").text()).toContain("MCP 知识调用 · generation");
    expect(wrapper.get(".event-list").text()).toContain("knowledge_catalog 1 次");
    expect(wrapper.get(".event-list").text()).toContain("knowledge_search 1 次");
  });
});
