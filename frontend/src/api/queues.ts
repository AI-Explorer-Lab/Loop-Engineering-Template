import { apiRequest, apiText } from "./http";
import type { QueueCreatePayload, QueueData } from "../types/task";


export function createQueue(payload: QueueCreatePayload): Promise<QueueData> {
  return apiRequest<QueueData>("/api/queues", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function getQueue(queueId: string): Promise<QueueData> {
  return apiRequest<QueueData>(`/api/queues/${encodeURIComponent(queueId)}`);
}

export function resumeQueue(queueId: string): Promise<QueueData> {
  return apiRequest<QueueData>(
    `/api/queues/${encodeURIComponent(queueId)}/resume`,
    { method: "POST" },
  );
}

export function pauseQueue(queueId: string): Promise<QueueData> {
  return apiRequest<QueueData>(`/api/queues/${encodeURIComponent(queueId)}/pause`, {
    method: "POST",
  });
}

export function cancelQueue(queueId: string): Promise<QueueData> {
  return apiRequest<QueueData>(`/api/queues/${encodeURIComponent(queueId)}/cancel`, {
    method: "POST",
  });
}

export function rerunQueue(queueId: string): Promise<QueueData> {
  return apiRequest<QueueData>(`/api/queues/${encodeURIComponent(queueId)}/rerun`, {
    method: "POST",
  });
}

export function skipQueueSubtask(
  queueId: string,
  taskId: string,
  expectedUpdatedAt: string,
): Promise<QueueData> {
  return apiRequest<QueueData>(
    `/api/queues/${encodeURIComponent(queueId)}/subtasks/${encodeURIComponent(taskId)}/skip`,
    {
      method: "POST",
      body: JSON.stringify({ expected_updated_at: expectedUpdatedAt }),
    },
  );
}

export function reorderQueue(
  queueId: string,
  taskIds: string[],
  expectedUpdatedAt: string,
): Promise<QueueData> {
  return apiRequest<QueueData>(`/api/queues/${encodeURIComponent(queueId)}/reorder`, {
    method: "POST",
    body: JSON.stringify({
      task_ids: taskIds,
      expected_updated_at: expectedUpdatedAt,
    }),
  });
}

export async function getQueueReport(queueId: string): Promise<string> {
  return apiText(`/api/queues/${encodeURIComponent(queueId)}/report`);
}

export async function getQueueDiff(queueId: string): Promise<string> {
  return apiText(`/api/queues/${encodeURIComponent(queueId)}/diff`);
}
