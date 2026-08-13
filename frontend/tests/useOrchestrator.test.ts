import { describe, expect, it } from "vitest";

import { ApiError } from "../src/api/http";
import { isTransientRunArtifactNotFound } from "../src/composables/useOrchestrator";

describe("isTransientRunArtifactNotFound", () => {
  it("ignores missing artifacts while a rerun task is only accepted", () => {
    expect(
      isTransientRunArtifactNotFound(new ApiError("Task not found", 404), "task", "accepted"),
    ).toBe(true);
  });

  it("ignores missing artifacts while a queue is still pending", () => {
    expect(
      isTransientRunArtifactNotFound(new ApiError("Queue not found", 404), "queue", "pending"),
    ).toBe(true);
  });

  it("does not hide missing tasks after execution has started", () => {
    expect(
      isTransientRunArtifactNotFound(new ApiError("Task not found", 404), "task", "running"),
    ).toBe(false);
  });

  it("does not hide non-404 errors", () => {
    expect(
      isTransientRunArtifactNotFound(new ApiError("Server error", 500), "task", "accepted"),
    ).toBe(false);
  });
});
