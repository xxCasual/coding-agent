import type { Dispatch, MutableRefObject, SetStateAction } from "react";
import * as api from "../api";
import type { ChatMessage, RunRecord, RunSummary, SessionSummary, TaskMode, ReviewTarget } from "../types";
import { errorText, type Submission } from "./sessionHelpers";

type SendArgs = {
  draft: string;
  taskMode: TaskMode;
  reviewTarget?: ReviewTarget;
  workspaceId: string;
  sessionId: string | null;
  activeId: string | null;
  blocked: boolean;
  selection: MutableRefObject<number>;
  sending: MutableRefObject<boolean>;
  controlling: MutableRefObject<boolean>;
  submission: MutableRefObject<Submission | null>;
  mergeMessages: (incoming: ChatMessage[]) => void;
  setBusy: Dispatch<SetStateAction<boolean>>;
  setError: Dispatch<SetStateAction<string>>;
  setNotice: Dispatch<SetStateAction<string>>;
  setSessionId: Dispatch<SetStateAction<string | null>>;
  setSessionReady: Dispatch<SetStateAction<boolean>>;
  setSessions: Dispatch<SetStateAction<SessionSummary[]>>;
  setRuns: Dispatch<SetStateAction<RunSummary[]>>;
  setActiveId: Dispatch<SetStateAction<string | null>>;
  setSelectedId: Dispatch<SetStateAction<string | null>>;
  setDraft: Dispatch<SetStateAction<string>>;
};

export async function sendRequirement({
  draft,
  taskMode,
  reviewTarget,
  workspaceId,
  sessionId,
  activeId,
  blocked,
  selection,
  sending,
  controlling,
  submission,
  mergeMessages,
  setBusy,
  setError,
  setNotice,
  setSessionId,
  setSessionReady,
  setSessions,
  setRuns,
  setActiveId,
  setSelectedId,
  setDraft,
}: SendArgs) {
  const content = draft.trim();
  if (!content || !workspaceId || blocked || sending.current || controlling.current) return;
  sending.current = true; setBusy(true); setError(""); setNotice("");
  const version = selection.current;
  const optionsKey = JSON.stringify({ taskMode, reviewTarget });
  let pending = submission.current;
  if (!pending || pending.content !== content || pending.sessionId !== sessionId || pending.optionsKey !== optionsKey) {
    pending = { content, optionsKey, sessionId, runId: activeId, key: api.makeUuid(), messageId: api.makeUuid() };
    submission.current = pending;
  }
  try {
    if (!pending.sessionId) {
      const created = await api.createSession(workspaceId);
      if (version !== selection.current) return;
      pending.sessionId = created.session_id; setSessionId(created.session_id); setSessionReady(true);
      setSessions(current => [created, ...current]);
    }
    let runId = pending.runId;
    if (runId) {
      const stored = await api.appendMessage(runId, content, { messageId: pending.messageId });
      if (version !== selection.current) return;
      mergeMessages([{ ...stored, id: stored.message_id, delivery: "received" }]);
      setNotice("补充需求已接收，将在后续执行步骤中处理。");
    } else {
      const created = await api.createRun(pending.sessionId, content, { idempotencyKey: pending.key, messageId: pending.messageId, reviewer: taskMode === "plan" ? "off" : "default", taskMode, reviewTarget });
      if (version !== selection.current) return;
      runId = created.run_id;
      mergeMessages([{ id: pending.messageId, message_id: pending.messageId, session_id: pending.sessionId, run_id: runId, role: "user", content }]);
      setRuns(current => current.some(run => run.run_id === runId) ? current : [...current, { run_id: runId!, status: created.status as RunSummary["status"], requirement: content }]);
      setActiveId(runId);
    }
    setSelectedId(runId); setDraft(""); submission.current = null;
  } catch (err) {
    if (version === selection.current) setError(`${errorText(err)} 输入已保留，可再次发送重试。`);
  } finally {
    sending.current = false; setBusy(false);
  }
}

type ControlArgs = {
  activeRun: RunRecord | null;
  selection: MutableRefObject<number>;
  sending: MutableRefObject<boolean>;
  controlling: MutableRefObject<boolean>;
  controlEpoch: MutableRefObject<number>;
  updateRun: (run: RunRecord) => void;
  setControlBusy: Dispatch<SetStateAction<boolean>>;
  setError: Dispatch<SetStateAction<string>>;
  setNotice: Dispatch<SetStateAction<string>>;
};

export async function controlRun(
  action: "cancel" | "allow" | "deny" | "continue" | "accept_and_continue" | "end_task",
  {
    activeRun,
    selection,
    sending,
    controlling,
    controlEpoch,
    updateRun,
    setControlBusy,
    setError,
    setNotice,
  }: ControlArgs,
) {
  if (!activeRun || controlling.current || sending.current) return;
  controlling.current = true; controlEpoch.current += 1; setControlBusy(true); setError("");
  const version = selection.current;
  try {
    if (action === "cancel") await api.cancelRun(activeRun.run_id);
    else if (action === "allow" || action === "deny") {
      const approval = activeRun.pending_approval?.approval_id || activeRun.pending_approval_id;
      if (!approval) throw new Error("审批信息尚未就绪，请稍后重试。");
      await api.decideApproval(approval, action);
    } else await api.resumeRun(activeRun.run_id, { action, call_id: activeRun.attention?.call_id ?? undefined, workspace_revision: activeRun.workspace_revision });
    if (version !== selection.current) return;
    const updated = await api.getRun(activeRun.run_id);
    if (version !== selection.current) return;
    updateRun(updated);
    setNotice(action === "cancel" ? (updated.status === "cancelled" ? "服务已确认取消。" : "已请求取消，等待服务确认退出。") : "操作已提交。");
  } catch (err) { if (version === selection.current) setError(errorText(err)); }
  finally { controlling.current = false; setControlBusy(false); }
}
