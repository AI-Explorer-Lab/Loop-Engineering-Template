import { apiRequest } from "./http";
import type {
  PlanConfirmationData,
  PlanCreatePayload,
  PlanDraft,
} from "../types/task";

export function createPlan(payload: PlanCreatePayload): Promise<PlanDraft> {
  return apiRequest<PlanDraft>("/api/plans", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function getPlan(planId: string): Promise<PlanDraft> {
  return apiRequest<PlanDraft>(`/api/plans/${encodeURIComponent(planId)}`);
}

export function confirmPlan(
  planId: string,
  reviewer: string,
  editedDraft: PlanDraft,
): Promise<PlanConfirmationData> {
  return apiRequest<PlanConfirmationData>(
    `/api/plans/${encodeURIComponent(planId)}/confirm`,
    {
      method: "POST",
      body: JSON.stringify({ reviewer, edited_draft: editedDraft }),
    },
  );
}
