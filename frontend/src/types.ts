export type TaskMode = "develop" | "review" | "plan";

export interface ReviewTarget {
  kind: "run_changes" | "workspace_changes" | "paths";
  paths?: string[];
  focus?: string;
  baseline?: string;
  workspace_revision?: string;
}

export interface ReadRecord {
  path: string;
  source?: string;
  start_line?: number;
  end_line?: number;
}

export type RunStatus =
  | "queued"
  | "running"
  | "waiting_approval"
  | "interrupted"
  | "needs_attention"
  | "succeeded"
  | "failed"
  | "cancelled";

export type UiStatus = "idle" | "submitting" | RunStatus;

export type ConnectionStatus = "idle" | "connected" | "reconnecting" | "offline";

export type Severity = "critical" | "high" | "medium" | "low" | "info";

export interface Finding {
  finding_id: string;
  hunk_id: string;
  file_path: string;
  start_line: number;
  end_line: number;
  severity: Severity;
  category: string;
  title: string;
  evidence: string;
  explanation: string;
  suggestion: string;
  confidence: number;
  is_blocking: boolean;
}

export interface FindingDisposition {
  finding_id: string;
  status: "fixed" | "not_adopted";
  reason: string;
}

export interface Usage {
  profile_id?: string;
  input_tokens?: number | null;
  output_tokens?: number | null;
  model_latency_ms?: number | null;
  estimated_cost?: number | null;
  price_config_id?: string | null;
  price_as_of?: string | null;
}

export type VerificationStatus = "passed" | "failed" | "unverified" | "not_applicable";

export interface Verification {
  status?: VerificationStatus | string;
  command?: string | null;
  exit_code?: number | null;
  workspace_revision?: string | null;
  evidence_refs?: string[];
}

export interface ReviewState {
  findings_count?: number;
  target?: ReviewTarget | null;
  selected_paths?: string[];
  read_records?: ReadRecord[];
  coverage?: string[];
  unchecked?: string[];
  enabled?: boolean;
  mode?: string;
  status?: string | null;
  subtask_id?: string | null;
  workspace_revision?: string | null;
  findings?: Finding[];
  dispositions?: FindingDisposition[];
  usage?: Usage | null;
  warnings?: string[];
}

export interface ApprovalIntent {
  tool?: string;
  arguments?: Record<string, unknown>;
  reason?: string;
  [key: string]: unknown;
}

export interface PendingApproval {
  approval_id: string;
  intent: ApprovalIntent;
  param_summary?: string;
  workspace_revision?: string | null;
  patch_hash?: string | null;
  call_id?: string | null;
}

export interface Attention {
  reason?: string;
  call_id?: string | null;
  affected_files?: string[];
  action?: string;
}

export interface Workspace {
  workspace_id: string;
  display_name: string;
  path: string;
}

export interface SessionSummary {
  session_id: string;
  workspace_id: string;
  created_at: string;
}

export interface SessionDetail extends SessionSummary {
  active_run_id: string | null;
  messages: ChatMessage[];
  next_cursor: string | null;
}

export interface RunSummary {
  run_id: string;
  status: RunStatus;
  requirement: string;
  created_at?: string;
}

export interface RunRecord {
  task_mode?: TaskMode;
  review_target?: ReviewTarget | null;
  workspace_snapshot?: { source_run_id?: string | null; source_revision?: string; baseline_commit?: string } | null;
  run_id: string;
  session_id: string;
  status: RunStatus;
  requirement: string;
  profile_id: string;
  phase?: string | null;
  verification?: Verification | null;
  budget?: Record<string, unknown> | null;
  usage?: Usage | null;
  workspace_revision?: string | null;
  cancel_requested: boolean;
  dispatch_pending?: boolean;
  pending_approval_id?: string | null;
  pending_approval?: PendingApproval | null;
  needs_attention: boolean;
  attention?: Attention | null;
  review?: ReviewState | null;
  created_at: string;
  updated_at: string;
}

export interface Artifact {
  artifact_id: string;
  kind: string;
  summary: string;
  size_bytes: number;
  content_hash: string;
}

export interface RunEvent {
  type: string;
  message?: string;
  payload?: Record<string, unknown>;
  run_id?: string;
  seq?: number;
}

export interface ChatMessage {
  id: string;
  message_id?: string;
  session_id?: string;
  run_id?: string;
  role: "user" | "assistant" | "tool" | string;
  content: string;
  isError?: boolean;
  delivery?: "received";
}

export interface InlineStatus {
  tone: "neutral" | "success" | "error";
  message: string;
}

export const TERMINAL_RUN_STATUSES: readonly RunStatus[] = ["succeeded", "failed", "cancelled"];
