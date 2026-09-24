import { eventDedupeKey, parseSseChunk } from "./sse";
import type {
  Artifact,
  RunEvent,
  RunRecord,
  TaskMode,
  ReviewTarget,
  SessionDetail,
  SessionSummary,
  Workspace,
} from "./types";

export class ApiError extends Error {
  code?: string;
  details?: Record<string, unknown>;

  constructor(message: string, code?: string, details?: Record<string, unknown>) {
    super(message);
    this.name = "ApiError";
    this.code = code;
    this.details = details;
  }
}

export async function listWorkspaces(signal?: AbortSignal): Promise<Workspace[]> {
  return readJson<Workspace[]>(await fetch("/api/workspaces", { signal }));
}

export async function listSessions(signal?: AbortSignal): Promise<{ items: SessionSummary[]; next_cursor: string | null }> {
  return readJson(await fetch("/api/sessions?limit=50", { signal }));
}

export async function createSession(workspaceId: string, signal?: AbortSignal): Promise<SessionSummary> {
  return readJson(
    await fetch("/api/sessions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ workspace_id: workspaceId }),
      signal,
    }),
  );
}

export async function getSession(sessionId: string, signal?: AbortSignal): Promise<SessionDetail> {
  const payload = await readJson<SessionDetail & { messages?: Array<Record<string, string>> }>(
    await fetch(`/api/sessions/${encodeURIComponent(sessionId)}?limit=100`, { signal }),
  );
  return {
    ...payload,
    messages: (payload.messages ?? []).map((item) => ({
      id: item.message_id || item.id,
      message_id: item.message_id,
      session_id: item.session_id,
      run_id: item.run_id,
      role: item.role,
      content: item.content,
    })),
  };
}

export async function listSessionRuns(
  sessionId: string,
  signal?: AbortSignal,
): Promise<{ items: Array<{ run_id: string; status: string; requirement: string; created_at?: string }>; next_cursor: string | null }> {
  return readJson(await fetch(`/api/sessions/${encodeURIComponent(sessionId)}/runs?limit=50`, { signal }));
}

export async function createRun(
  sessionId: string,
  requirement: string,
  options: { idempotencyKey: string; messageId?: string; reviewer?: "default" | "off"; taskMode?: TaskMode; reviewTarget?: ReviewTarget; signal?: AbortSignal },
): Promise<{ run_id: string; session_id: string; status: string }> {
  return readJson(
    await fetch(`/api/sessions/${encodeURIComponent(sessionId)}/runs`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": options.idempotencyKey,
      },
      body: JSON.stringify({
        requirement,
        task_mode: options.taskMode ?? "develop",
        review_target: options.reviewTarget,
        reviewer: options.reviewer ?? "default",
        message_id: options.messageId,
      }),
      signal: options.signal,
    }),
  );
}

export async function getRun(runId: string, signal?: AbortSignal): Promise<RunRecord> {
  return readJson(await fetch(`/api/runs/${encodeURIComponent(runId)}`, { signal }));
}

export async function appendMessage(
  runId: string,
  content: string,
  options: { messageId: string; signal?: AbortSignal },
): Promise<{ message_id: string; run_id: string; role: string; content: string }> {
  return readJson(
    await fetch(`/api/runs/${encodeURIComponent(runId)}/messages`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content, message_id: options.messageId }),
      signal: options.signal,
    }),
  );
}

export async function cancelRun(runId: string, signal?: AbortSignal): Promise<RunRecord> {
  return readJson(
    await fetch(`/api/runs/${encodeURIComponent(runId)}/cancel`, { method: "POST", signal }),
  );
}

export async function resumeRun(
  runId: string,
  body: { action: "continue" | "accept_and_continue" | "end_task"; call_id?: string; workspace_revision?: string | null },
  signal?: AbortSignal,
): Promise<{ ok?: boolean; run_id?: string; status?: string }> {
  return readJson(
    await fetch(`/api/runs/${encodeURIComponent(runId)}/resume`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal,
    }),
  );
}

export async function decideApproval(
  approvalId: string,
  decision: "allow" | "deny",
  signal?: AbortSignal,
): Promise<{ ok?: boolean }> {
  return readJson(
    await fetch(`/api/approvals/${encodeURIComponent(approvalId)}/decision`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ decision }),
      signal,
    }),
  );
}

export async function listArtifacts(runId: string, signal?: AbortSignal): Promise<Artifact[]> {
  return readJson(await fetch(`/api/runs/${encodeURIComponent(runId)}/artifacts`, { signal }));
}

export async function getArtifactText(runId: string, artifactId: string, signal?: AbortSignal): Promise<string> {
  const response = await fetch(
    `/api/runs/${encodeURIComponent(runId)}/artifacts/${encodeURIComponent(artifactId)}`,
    { signal },
  );
  if (!response.ok) {
    throw new ApiError("无法读取产物。");
  }
  return response.text();
}

export async function consumeRunEvents(
  runId: string,
  afterSeq: number,
  options: {
    signal?: AbortSignal;
    seen: Set<string>;
    onEvent: (event: RunEvent) => void;
    onOpen?: () => void;
  },
): Promise<void> {
  const response = await fetch(`/api/runs/${encodeURIComponent(runId)}/events?after=${afterSeq}`, {
    headers: afterSeq ? { "Last-Event-ID": String(afterSeq) } : undefined,
    signal: options.signal,
  });
  if (!response.ok || !response.body) {
    throw new ApiError("事件流连接失败。");
  }
  options.onOpen?.();
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const parsed = parseSseChunk(buffer);
    buffer = parsed.rest;
    for (const event of parsed.events) {
      if (event.run_id && event.run_id !== runId) continue;
      const key = eventDedupeKey(event.run_id || runId, event.seq);
      if (key && options.seen.has(key)) continue;
      if (key) options.seen.add(key);
      options.onEvent(event);
    }
  }
}

async function readJson<T>(response: Response): Promise<T> {
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = typeof payload === "object" && payload !== null && "detail" in payload ? payload.detail : payload;
    if (typeof detail === "object" && detail !== null) {
      const message = "message" in detail ? String(detail.message) : "请求失败，请检查服务状态。";
      const code = "code" in detail ? String(detail.code) : undefined;
      const details =
        "details" in detail && typeof detail.details === "object" && detail.details !== null
          ? (detail.details as Record<string, unknown>)
          : undefined;
      throw new ApiError(message, code, details);
    }
    throw new ApiError(typeof detail === "string" ? detail : "请求失败，请检查服务状态。");
  }
  return payload as T;
}

export function isAbortError(error: unknown): boolean {
  return (error instanceof DOMException && error.name === "AbortError") || (error instanceof Error && error.name === "AbortError");
}

export function stableClientId(storageKey: string): string {
  try {
    const existing = sessionStorage.getItem(storageKey);
    if (existing) return existing;
    const created = makeUuid();
    sessionStorage.setItem(storageKey, created);
    return created;
  } catch {
    return makeUuid();
  }
}

export function makeUuid(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  return `id-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}
