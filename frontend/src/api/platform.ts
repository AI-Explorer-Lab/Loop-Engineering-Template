import { apiRequest, apiText, PROJECT_STORAGE_KEY } from "./http";
import type {
  EventPageData,
  HarnessCapabilitiesData,
  HarnessMetricsData,
  HistoryPageData,
  LogData,
  NotificationData,
  NotificationSettingsData,
  ProjectData,
  RunKind,
} from "../types/task";

export interface HistoryQuery {
  project_id?: string;
  kind?: RunKind | "";
  status?: string;
  query?: string;
  page?: number;
  page_size?: number;
}

function queryString(values: Record<string, string | number | undefined>): string {
  const params = new URLSearchParams();
  Object.entries(values).forEach(([key, value]) => {
    if (value !== undefined && value !== "") params.set(key, String(value));
  });
  const result = params.toString();
  return result ? `?${result}` : "";
}

export function getProjects(): Promise<ProjectData[]> {
  return apiRequest<ProjectData[]>("/api/projects");
}

export function getCapabilities(): Promise<HarnessCapabilitiesData> {
  return apiRequest<HarnessCapabilitiesData>("/api/capabilities");
}

export function getMetrics(): Promise<HarnessMetricsData> {
  return apiRequest<HarnessMetricsData>("/api/metrics");
}

export function getHistory(query: HistoryQuery = {}): Promise<HistoryPageData> {
  return apiRequest<HistoryPageData>(`/api/history${queryString({ ...query })}`);
}

export function getEvents(
  kind: RunKind,
  identifier: string,
  after = 0,
): Promise<EventPageData> {
  return apiRequest<EventPageData>(
    `/api/history/${kind}/${encodeURIComponent(identifier)}/events${queryString({ after })}`,
  );
}

export function eventStreamUrl(
  kind: RunKind,
  identifier: string,
  after = 0,
): string {
  let projectId: string | undefined;
  try {
    projectId = localStorage.getItem(PROJECT_STORAGE_KEY) || undefined;
  } catch {
    projectId = undefined;
  }
  return `/api/history/${kind}/${encodeURIComponent(identifier)}/stream${queryString({
    after,
    project_id: projectId,
  })}`;
}

export function getLogs(kind: RunKind, identifier: string): Promise<LogData[]> {
  return apiRequest<LogData[]>(
    `/api/history/${kind}/${encodeURIComponent(identifier)}/logs`,
  );
}

export function getLog(
  kind: RunKind,
  identifier: string,
  logId: string,
): Promise<string> {
  const encoded = logId.split("/").map(encodeURIComponent).join("/");
  return apiText(`/api/history/${kind}/${encodeURIComponent(identifier)}/logs/${encoded}`);
}

export function getNotifications(
  projectId?: string,
  unreadOnly = false,
): Promise<NotificationData[]> {
  return apiRequest<NotificationData[]>(
    `/api/notifications${queryString({
      project_id: projectId,
      unread_only: unreadOnly ? "true" : undefined,
    })}`,
  );
}

export function markNotificationRead(
  notificationId: string,
): Promise<NotificationData> {
  return apiRequest<NotificationData>(
    `/api/notifications/${encodeURIComponent(notificationId)}/read`,
    { method: "POST" },
  );
}

export function getNotificationSettings(): Promise<NotificationSettingsData> {
  return apiRequest<NotificationSettingsData>("/api/notifications/settings");
}

export function updateNotificationSettings(payload: {
  in_app: boolean;
  browser: boolean;
}): Promise<NotificationSettingsData> {
  return apiRequest<NotificationSettingsData>("/api/notifications/settings", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}
