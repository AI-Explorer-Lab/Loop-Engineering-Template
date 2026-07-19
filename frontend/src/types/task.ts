export type TaskStatus =
  | "accepted"
  | "running"
  | "pausing"
  | "paused"
  | "cancelling"
  | "cancelled"
  | "success"
  | "manual_review"
  | "infrastructure_error";

export type ReviewStatus =
  | "pending"
  | "approved"
  | "changes_requested"
  | "rejected"
  | "unavailable";

export type ReviewDecision = Exclude<ReviewStatus, "pending" | "unavailable">;
export type RunKind = "task" | "queue";
export type DeliveryStatus =
  | "not_ready"
  | "commit_pending"
  | "committing"
  | "committed"
  | "archive_pending"
  | "archived"
  | "failed"
  | "unavailable";

export interface TaskCreatePayload {
  requirement: string;
  acceptance_criteria: string[];
}

export interface ReviewPayload {
  decision: ReviewDecision;
  reviewer: string;
  comment: string;
  reviewed_diff_sha256: string;
  commit_subject: string;
}

export interface PlanCreatePayload extends TaskCreatePayload {
  name: string;
}

export interface PlannedSubtask {
  sequence: number;
  title: string;
  requirement_slice: string;
  source_acceptance_ids: string[];
}

export interface PlanDraft {
  schema_version: number;
  plan_id: string;
  name: string;
  source_requirement_sha256: string;
  context_sha256: string;
  acceptance_criteria: Record<string, string>;
  status: "ready" | "manual_input_required";
  execution_mode: "single" | "queue";
  subtasks: PlannedSubtask[];
  unassigned_acceptance_ids: string[];
  warnings: string[];
  planner_thread_id: string;
  created_at: string;
}

export interface PlanConfirmationData {
  plan_id: string;
  target_kind: RunKind;
  target: TaskData | QueueData;
  confirmation: Record<string, unknown>;
}

export interface CommandSummary {
  command: string[];
  stage: string;
  duration_seconds: number;
  exit_code: number | null;
  timed_out: boolean;
  infrastructure_error: string | null;
  log_path: string | null;
  passed: boolean;
}

export interface ValidationRound {
  round_number: number;
  passed: boolean;
  stage: string;
  started_at: string;
  finished_at: string | null;
  failure_summary: string;
  infrastructure_error: string | null;
  commands: CommandSummary[];
}

export interface TaskData {
  task_id: string;
  requirement: string;
  acceptance_criteria: string[];
  status: TaskStatus;
  schema_version: number;
  legacy: boolean;
  history_warning: string | null;
  machine_status: TaskStatus | null;
  review_status: ReviewStatus;
  delivery_status: DeliveryStatus;
  phase: string | null;
  thread_id: string | null;
  turn_count: number;
  failure_count: number;
  cycle_turn_count: number;
  cycle_failure_count: number;
  rounds: ValidationRound[];
  last_error_summary: string;
  infrastructure_error: string | null;
  started_at: string;
  updated_at: string;
  finished_at: string | null;
  report_url: string | null;
  diff_url: string | null;
  workspace: Record<string, unknown>;
  permissions: Record<string, unknown>;
  audit_summary: Record<string, unknown>;
  changed_files: Array<Record<string, unknown>>;
  codex_responses: Array<{ turn_number: number; response: string }>;
  final_diff_sha256: string;
  diff_redaction_count: number;
  review: Record<string, unknown> | null;
  review_history: Array<Record<string, unknown>>;
  context: Record<string, unknown>;
  evaluations: Record<string, unknown>;
  commit: Record<string, unknown>;
  archive: Record<string, unknown>;
  queue_id: string | null;
  sequence: number | null;
  rerun_of: string | null;
}

export type QueueStatus =
  | "pending"
  | "running"
  | "pausing"
  | "paused"
  | "cancelling"
  | "cancelled"
  | "waiting_review"
  | "rejected"
  | "infrastructure_error"
  | "completed";

export type QueueSubtaskStatus =
  | "pending"
  | "running"
  | "pausing"
  | "paused"
  | "cancelling"
  | "cancelled"
  | "skipped"
  | "waiting_review"
  | "completed"
  | "rejected"
  | "infrastructure_error";

export interface QueueSubtaskCreatePayload extends TaskCreatePayload {}

export interface QueueCreatePayload {
  name: string;
  subtasks: QueueSubtaskCreatePayload[];
}

export interface QueueSubtaskData extends QueueSubtaskCreatePayload {
  task_id: string;
  sequence: number;
  status: QueueSubtaskStatus;
  machine_status: TaskStatus | null;
  review_status: ReviewStatus;
  delivery_status: DeliveryStatus;
  thread_id: string | null;
  last_error_summary: string;
  updated_at: string;
}

export interface QueueData {
  queue_id: string;
  name: string;
  status: QueueStatus;
  base_ref: string;
  base_commit: string;
  current_task_id: string | null;
  cumulative_diff_sha256: string;
  last_error_summary: string;
  delivery_status: DeliveryStatus;
  subtasks: QueueSubtaskData[];
  started_at: string;
  updated_at: string;
  finished_at: string | null;
  report_url: string | null;
  diff_url: string | null;
  rerun_of: string | null;
}

export interface ProjectData {
  project_id: string;
  name: string;
  repo_root: string;
  is_default: boolean;
  active_identifier: string | null;
  knowledge_actor_id: string;
}

export interface HistoryItemData {
  kind: RunKind;
  identifier: string;
  project_id: string;
  project_name: string;
  title: string;
  status: string;
  review_status: ReviewStatus | null;
  started_at: string;
  updated_at: string;
  finished_at: string | null;
  current_task_id: string | null;
  delivery_status: DeliveryStatus | null;
}

export interface HistoryPageData {
  items: HistoryItemData[];
  page: number;
  page_size: number;
  total: number;
  pages: number;
}

export interface EventRecord {
  seq: number;
  type: string;
  event?: string;
  timestamp: string;
  task_id?: string;
  queue_id?: string;
  [key: string]: unknown;
}

export interface EventPageData {
  items: EventRecord[];
  next_cursor: number;
  terminal: boolean;
}

export interface LogData {
  log_id: string;
  name: string;
  size: number;
  sha256: string;
}

export interface NotificationData {
  notification_id: string;
  project_id: string;
  kind: RunKind;
  identifier: string;
  category: "waiting_review" | "completed" | "failure" | "cancelled";
  title: string;
  message: string;
  created_at: string;
  read_at: string | null;
  delivery: Record<string, string>;
}

export interface NotificationSettingsData {
  in_app: boolean;
  browser: boolean;
  email_configured: boolean;
  webhook_configured: boolean;
}

export interface HealthData {
  status: string;
  environment: string;
  version: string;
}

export interface HarnessCapabilitiesData {
  status: "healthy" | "unavailable" | string;
  project_id: string;
  reason?: string;
  knowledge_base_path?: string;
  mcp_registry?: string;
  mcp_read?: Record<string, unknown>;
  mcp_archive?: Record<string, unknown>;
  skill_count?: number;
  archive_backlog?: number;
  knowledge_actor_id?: string;
  checked_at?: string;
}

export interface HarnessMetricsData {
  project_id: string;
  task_success_rate: number | null;
  completed_tasks: number;
  layer_failure_counts: Record<string, number>;
  repair_rounds: number;
  knowledge_hit_rate: number | null;
  planned_tasks: number;
  planner_manual_edit_count: number;
  commit_success_rate: number | null;
  archive_backlog: number;
}

export interface ApiResponse<T> {
  success: boolean;
  data: T | null;
  message: string;
  request_id: string | null;
}
