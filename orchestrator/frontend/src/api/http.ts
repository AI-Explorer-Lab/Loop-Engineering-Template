import type { ApiResponse } from "../types/task";

export const PROJECT_STORAGE_KEY = "codex-orchestrator:project-id";

interface ApiErrorBody {
  message?: string;
  code?: string;
  request_id?: string | null;
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: string | null;
  readonly requestId: string | null;

  constructor(
    message: string,
    status: number,
    code: string | null = null,
    requestId: string | null = null,
  ) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.requestId = requestId;
  }
}

function activeProjectId(): string | null {
  try {
    return localStorage.getItem(PROJECT_STORAGE_KEY);
  } catch {
    return null;
  }
}

export function projectHeaders(headers?: HeadersInit): Headers {
  const result = new Headers(headers);
  const projectId = activeProjectId();
  if (projectId) result.set("X-Project-ID", projectId);
  return result;
}

export async function apiRequest<T>(
  path: string,
  init?: RequestInit,
): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: projectHeaders({
      "Content-Type": "application/json",
      ...Object.fromEntries(new Headers(init?.headers).entries()),
    }),
  });

  if (!response.ok) {
    let body: ApiErrorBody = {};
    try {
      body = (await response.json()) as ApiErrorBody;
    } catch {
      body = {};
    }
    throw new ApiError(
      body.message || `请求失败（${response.status}）`,
      response.status,
      body.code || null,
      body.request_id || null,
    );
  }

  const payload = (await response.json()) as ApiResponse<T>;
  if (!payload.success || payload.data === null) {
    throw new ApiError(payload.message || "接口没有返回任务数据", response.status);
  }
  return payload.data;
}

export async function apiText(path: string): Promise<string> {
  const response = await fetch(path, { headers: projectHeaders() });
  if (!response.ok) {
    throw new ApiError(`记录读取失败（${response.status}）`, response.status);
  }
  return response.text();
}
