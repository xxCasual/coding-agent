import { useEffect, type Dispatch, type MutableRefObject, type SetStateAction } from "react";
import * as api from "../api";
import type { ChatMessage, ConnectionStatus, RunEvent, RunRecord } from "../types";
import { isTerminalRunStatus } from "../utils";
import { errorText } from "./sessionHelpers";

type MonitorArgs = {
  sessionId: string | null;
  activeId: string | null;
  selectedId: string | null;
  selection: MutableRefObject<number>;
  controlEpoch: MutableRefObject<number>;
  controlling: MutableRefObject<boolean>;
  eventCache: MutableRefObject<Record<string, RunEvent[]>>;
  mergeMessages: (incoming: ChatMessage[]) => void;
  updateRun: (run: RunRecord) => void;
  setEvents: Dispatch<SetStateAction<Record<string, RunEvent[]>>>;
  setConnection: Dispatch<SetStateAction<ConnectionStatus>>;
  setError: Dispatch<SetStateAction<string>>;
  setNotice: Dispatch<SetStateAction<string>>;
  setPartialHistory: Dispatch<SetStateAction<boolean>>;
};

export function useRunMonitor({
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
}: MonitorArgs) {
  useEffect(() => {
    if (!sessionId) return;
    const version = selection.current;
    const controller = new AbortController();
    const { signal } = controller;
    const valid = () => !signal.aborted && version === selection.current;
    const timers = new Set<ReturnType<typeof setTimeout>>();
    const sleep = (ms: number) => new Promise<void>(resolve => {
      const finish = () => { clearTimeout(timer); timers.delete(timer); signal.removeEventListener("abort", finish); resolve(); };
      const timer = setTimeout(finish, ms);
      timers.add(timer); signal.addEventListener("abort", finish, { once: true });
    });
    let refreshingMessages = false;
    const refreshMessages = async () => {
      if (refreshingMessages || !valid()) return;
      refreshingMessages = true;
      try {
        const detail = await api.getSession(sessionId, signal);
        if (valid()) { mergeMessages(detail.messages); if (detail.next_cursor) setPartialHistory(true); }
      } catch { /* The event transcript remains available during a message refresh failure. */ }
      finally { refreshingMessages = false; }
    };
    const monitor = async (id: string) => {
      let latest: RunRecord | null = null;
      let refreshing = false;
      const refresh = async () => {
        if (refreshing || !valid()) return;
        refreshing = true;
        const epoch = controlEpoch.current;
        try {
          const run = await api.getRun(id, signal);
          if (valid() && epoch === controlEpoch.current && !controlling.current) { latest = run; updateRun(run); }
        } catch (err) {
          if (valid()) { setConnection("offline"); setError(errorText(err)); }
        } finally { refreshing = false; }
      };
      const poll = async () => {
        while (valid() && (!latest || !isTerminalRunStatus(latest.status))) {
          await sleep(2000);
          if (!valid()) return;
          await refresh(); await refreshMessages();
        }
      };
      await refresh();
      if (!valid()) return;
      void poll();
      const cached = eventCache.current[id] || [];
      let seq = cached.reduce((max, event) => Math.max(max, event.seq ?? 0), 0);
      const seen = new Set(cached.filter(event => event.seq != null).map(event => `${id}:${event.seq}`));
      while (valid()) {
        try {
          if (id === activeId) setConnection("reconnecting");
          await api.consumeRunEvents(id, seq, { signal, seen,
            onOpen: () => { if (valid() && id === activeId) setConnection("connected"); },
            onEvent: event => {
              if (!valid()) return;
              seq = Math.max(seq, event.seq ?? 0);
              eventCache.current[id] = [...(eventCache.current[id] || []), event];
              setEvents({ ...eventCache.current });
              if (/^(run\.|approval\.|verification\.|delegate\.|review\.)/.test(event.type)) void refresh();
              if (event.type === "model.done" || event.type === "message.ingested") void refreshMessages();
              if (event.type === "message.ingested") setNotice("补充需求已纳入执行。");
            },
          });
          if (!valid()) return;
          await refresh(); await refreshMessages();
          const completed = latest as RunRecord | null;
          if (completed && isTerminalRunStatus(completed.status)) {
            if (id === activeId || !activeId) setConnection("idle");
            return;
          }
        } catch (err) {
          if (!valid() || api.isAbortError(err)) return;
          setConnection("reconnecting");
        }
        await sleep(1500);
      }
    };
    [...new Set([activeId, selectedId].filter((id): id is string => Boolean(id)))].forEach(id => void monitor(id));
    return () => { controller.abort(); timers.forEach(clearTimeout); };
  }, [sessionId, activeId, selectedId, selection, controlEpoch, controlling, eventCache, mergeMessages, updateRun, setEvents, setConnection, setError, setNotice, setPartialHistory]);
}
