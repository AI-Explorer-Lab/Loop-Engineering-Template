import { apiRequest, apiText } from "./http";
import type { ReviewPayload, TaskCreatePayload, TaskData } from "../types/task";


export function createTask(payload: TaskCreatePayload): Promise<TaskData> {
  return apiRequest<TaskData>("/api/tasks", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function getTask(taskId: string): Promise<TaskData> {
  return apiRequest<TaskData>(`/api/tasks/${encodeURIComponent(taskId)}`);
}

export function resumeTask(taskId: string): Promise<TaskData> {
  return apiRequest<TaskData>(
    `/api/tasks/${encodeURIComponent(taskId)}/resume`,
    { method: "POST" },
  );
}

export function pauseTask(taskId: string): Promise<TaskData> {
  return apiRequest<TaskData>(`/api/tasks/${encodeURIComponent(taskId)}/pause`, {
    method: "POST",
  });
}

export function cancelTask(taskId: string): Promise<TaskData> {
  return apiRequest<TaskData>(`/api/tasks/${encodeURIComponent(taskId)}/cancel`, {
    method: "POST",
  });
}

export function rerunTask(taskId: string): Promise<TaskData> {
  return apiRequest<TaskData>(`/api/tasks/${encodeURIComponent(taskId)}/rerun`, {
    method: "POST",
  });
}

export async function getTaskReport(taskId: string): Promise<string> {
  return apiText(`/api/tasks/${encodeURIComponent(taskId)}/report`);
}

export async function getTaskDiff(taskId: string): Promise<string> {
  return apiText(`/api/tasks/${encodeURIComponent(taskId)}/diff`);
}

export function submitTaskReview(
  taskId: string,
  payload: ReviewPayload,
): Promise<TaskData> {
  return apiRequest<TaskData>(
    `/api/tasks/${encodeURIComponent(taskId)}/review`,
    { method: "POST", body: JSON.stringify(payload) },
  );
}

export function retryTaskCommit(taskId: string): Promise<TaskData> {
  return apiRequest<TaskData>(
    `/api/tasks/${encodeURIComponent(taskId)}/delivery/retry`,
    { method: "POST" },
  );
}

export function retryTaskArchive(taskId: string): Promise<TaskData> {
  return apiRequest<TaskData>(
    `/api/tasks/${encodeURIComponent(taskId)}/archive/retry`,
    { method: "POST" },
  );
}
