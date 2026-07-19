import { apiRequest } from "./http";
import type { HealthData } from "../types/task";

export function getHealth(): Promise<HealthData> {
  return apiRequest<HealthData>("/api/health");
}
