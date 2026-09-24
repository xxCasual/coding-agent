import { useCallback, useEffect, useRef, useState } from "react";
import * as api from "../api";
import type { ChatMessage, ConnectionStatus, RunEvent, RunRecord, RunSummary, SessionSummary, Workspace, TaskMode, ReviewTarget } from "../types";
import { isTerminalRunStatus } from "../utils";
import { errorText, type Submission } from "./sessionHelpers";
import { controlRun, sendRequirement } from "./useRunActions";
import { useRunMonitor } from "./useRunMonitor";

export function useCodingSession() {
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [workspaceId, setWorkspaceId] = useState("");
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [records, setRecords] = useState<Record<string, RunRecord>>({});
  const [activeId, setActiveId] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [events, setEvents] = useState<Record<string, RunEvent[]>>({});
  const [connection, setConnection] = useState<ConnectionStatus>("idle");
  const [draft, setDraft] = useState("");
  const [taskMode, setTaskMode] = useState<TaskMode>("develop");
  const [targetKind, setTargetKind] = useState<ReviewTarget["kind"]>("workspace_changes");
  const [reviewPaths, setReviewPaths] = useState("");
  const [reviewFocus, setReviewFocus] = useState("");
  const [loading, setLoading] = useState(true);
  const [sessionReady, setSessionReady] = useState(false);
  const [busy, setBusy] = useState(false);
  const [controlBusy, setControlBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [partialHistory, setPartialHistory] = useState(false);
  const [partialSessions, setPartialSessions] = useState(false);
  const selection = useRef(0);
  const request = useRef<AbortController | null>(null);
  const submission = useRef<Submission | null>(null);
  const sending = useRef(false);
  const controlling = useRef(false);
  const controlEpoch = useRef(0);
  const eventCache = useRef<Record<string, RunEvent[]>>({});

  const mergeMessages = useCallback((incoming: ChatMessage[]) => {
    setMessages(current => {
      const merged = new Map(current.map(message => [message.id, message]));
      incoming.forEach(message => merged.set(message.id, { ...merged.get(message.id), ...message }));
      return [...merged.values()];
    });
  }, []);

  const updateRun = useCallback((run: RunRecord) => {
    setRecords(current => ({ ...current, [run.run_id]: run }));
    setRuns(current => current.map(item => item.run_id === run.run_id ? { ...item, status: run.status } : item));
    if (isTerminalRunStatus(run.status)) setActiveId(current => current === run.run_id ? null : current);
  }, []);

  const reset = useCallback(() => {
    selection.current += 1;
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setSessionId(null); setSessionReady(false); setRuns([]); setRecords({}); setActiveId(null); setSelectedId(null);
    setMessages([]); setEvents({}); eventCache.current = {};
    setDraft(""); setError(""); setNotice(""); setPartialHistory(false); setConnection("idle");
    submission.current = null;
    setTaskMode("develop"); setTargetKind("workspace_changes"); setReviewPaths(""); setReviewFocus("");
    return { version: selection.current, controller };
  }, []);

  const openSession = useCallback(async (id: string) => {
    const { version, controller } = reset();
    setLoading(true);
    setSessionId(id);
    try {
      const [detail, page] = await Promise.all([api.getSession(id, controller.signal), api.listSessionRuns(id, controller.signal)]);
      if (version !== selection.current || controller.signal.aborted) return;
      setWorkspaceId(detail.workspace_id);
      setMessages(detail.messages);
      const sorted = (page.items as RunSummary[]).slice().sort((a, b) => (a.created_at ?? "").localeCompare(b.created_at ?? ""));
      setRuns(sorted);
      setPartialHistory(Boolean(detail.next_cursor || page.next_cursor));
      const latestId = detail.active_run_id || sorted.at(-1)?.run_id || null;
      if (latestId) {
        const latest = await api.getRun(latestId, controller.signal);
        if (version !== selection.current || controller.signal.aborted) return;
        setRecords({ [latestId]: latest });
        setTaskMode(latest.task_mode ?? "develop");
        setTargetKind(latest.review_target?.kind ?? "workspace_changes");
        setReviewPaths(latest.review_target?.paths?.join("\n") ?? "");
        setReviewFocus(latest.review_target?.focus ?? "");
      }
      setActiveId(detail.active_run_id);
      setSelectedId(latestId);
      setSessionReady(true);
    } catch (err) {
      if (version === selection.current && !api.isAbortError(err)) setError(errorText(err));
    } finally {
      if (version === selection.current) setLoading(false);
    }
  }, [reset]);

  const bootstrap = useCallback(async () => {
    const { version, controller } = reset();
    setLoading(true);
    try {
      const [listed, page] = await Promise.all([api.listWorkspaces(controller.signal), api.listSessions(controller.signal)]);
      if (version !== selection.current || controller.signal.aborted) return;
      setWorkspaces(listed); setSessions(page.items); setPartialSessions(Boolean(page.next_cursor));
      setWorkspaceId(listed[0]?.workspace_id || "");
      const first = page.items.filter(item => listed.some(w => w.workspace_id === item.workspace_id)).sort((a, b) => b.created_at.localeCompare(a.created_at))[0];
      if (first) await openSession(first.session_id);
    } catch (err) {
      if (version === selection.current && !api.isAbortError(err)) setError(errorText(err));
    } finally {
      if (version === selection.current) setLoading(false);
    }
  }, [openSession, reset]);

  useEffect(() => {
    void bootstrap();
    return () => { selection.current += 1; request.current?.abort(); };
  }, [bootstrap]);

  useRunMonitor({
    sessionId,
    activeId,
    selectedId,
    selection,
    controlEpoch,
    controlling,
    eventCache,
    mergeMessages,
    updateRun,
    setEvents,
    setConnection,
    setError,
    setNotice,
    setPartialHistory,
  });

  const activeRun = activeId ? records[activeId] : null;
  const blocked = loading || Boolean(sessionId && !sessionReady) || (Boolean(activeId) && !activeRun) || Boolean(activeRun && (activeRun.cancel_requested || activeRun.needs_attention || ["interrupted", "needs_attention"].includes(activeRun.status)));

  const mode = activeRun?.task_mode ?? (activeId ? "develop" : taskMode);
  const reviewTarget: ReviewTarget | undefined = mode === "review" ? (activeRun?.review_target ?? {
    kind: targetKind, paths: reviewPaths.split("\n").map(path => path.trim()).filter(Boolean), focus: reviewFocus.trim(),
  }) : undefined;
  const invalidScope = Boolean(reviewTarget?.kind === "paths" && !reviewTarget.paths?.length);
  const send = () => sendRequirement({
    taskMode: mode, reviewTarget,
    draft, workspaceId, sessionId, activeId, blocked: blocked || invalidScope, selection, sending, controlling, submission,
    mergeMessages, setBusy, setError, setNotice, setSessionId, setSessionReady, setSessions, setRuns,
    setActiveId, setSelectedId, setDraft,
  });

  const control = (action: "cancel" | "allow" | "deny" | "continue" | "accept_and_continue" | "end_task") =>
    controlRun(action, { activeRun, selection, sending, controlling, controlEpoch, updateRun, setControlBusy, setError, setNotice });

  return { workspaces, sessions, workspaceId, sessionId, runs, records, activeId, activeRun, selectedId,
    selectedRun: selectedId ? records[selectedId] : null, messages, events, connection, draft, setDraft,
    taskMode: mode, setTaskMode, targetKind, setTargetKind, reviewPaths, setReviewPaths, reviewFocus, setReviewFocus, reviewTarget, invalidScope,
    loading, busy, controlBusy, error, notice, partialHistory, partialSessions, blocked, send, control,
    selectRun: setSelectedId, openSession, bootstrap,
    newSession: () => { reset(); setLoading(false); },
    changeWorkspace: (id: string) => { reset(); setWorkspaceId(id); setLoading(false); },
  };
}
