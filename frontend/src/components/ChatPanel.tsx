import { useEffect, useRef, useState } from "react";
import { ArrowDown, Code2 } from "lucide-react";
import type { ChatMessage, RunEvent, RunSummary } from "../types";

export function conversationMessages(runs: RunSummary[], messages: ChatMessage[], events: Record<string, RunEvent[]>): ChatMessage[] {
  return runs.flatMap(run => {
    const stored = messages.filter(message => message.run_id === run.run_id);
    const trace = events[run.run_id] || [];
    if (!trace.some(event => event.type === "model.text_delta")) {
      return stored.filter(message => (message.role === "user" || message.role === "assistant") && message.content.trim());
    }
    // Events have no assistant message ID. Use one source per run, never match by text.
    const first = stored.find(message => message.role === "user" && message.content === run.requirement);
    const result: ChatMessage[] = [{ id: first?.id || `requirement:${run.run_id}`, run_id: run.run_id, role: "user", content: run.requirement }];
    const included = new Set(first ? [first.id] : []);
    let assistant: ChatMessage | null = null;
    for (const event of trace) {
      if (event.type === "message.received") {
        const message = stored.find(item => item.id === event.payload?.message_id);
        if (message && !included.has(message.id)) { result.push(message); included.add(message.id); }
      }
      if (event.type === "model.text_delta") {
        if (!assistant) {
          assistant = { id: `event:${run.run_id}:${event.seq}`, run_id: run.run_id, role: "assistant", content: "" };
          result.push(assistant);
        }
        assistant.content += typeof event.payload?.text === "string" ? event.payload.text : event.message || "";
      }
      if (event.type === "model.done") assistant = null;
      if (event.type === "run.succeeded" && ["plan", "review"].includes(String(event.payload?.task_mode)) && event.message) {
        result.push({ id: `report:${run.run_id}:${event.seq}`, run_id: run.run_id, role: "assistant", content: event.message });
      }
    }
    // Locally acknowledged supplements may arrive before their SSE event.
    const received = new Set(trace.filter(event => event.type === "message.received").map(event => event.payload?.message_id));
    for (const message of stored) {
      if (message.role === "user" && !included.has(message.id) && (received.has(message.id) || message.delivery === "received")) result.push(message);
    }
    return result.filter(message => message.content.trim());
  });
}

export function ChatPanel({ messages, loading, sessionKey }: { messages: ChatMessage[]; loading: boolean; sessionKey: string }) {
  const log = useRef<HTMLDivElement>(null);
  const follow = useRef(true);
  const [unread, setUnread] = useState(false);
  useEffect(() => { follow.current = true; setUnread(false); }, [sessionKey]);
  useEffect(() => {
    if (follow.current && log.current) log.current.scrollTop = log.current.scrollHeight;
    else if (messages.length) setUnread(true);
  }, [messages]);
  return <div className="chat-area">
    <div className="chat-log" ref={log} role="log" aria-label="会话消息" aria-live="polite" onScroll={() => {
      if (!log.current) return;
      follow.current = log.current.scrollHeight - log.current.scrollTop - log.current.clientHeight < 72;
      if (follow.current) setUnread(false);
    }}>
      {loading ? <div className="welcome"><p>正在读取会话…</p></div> : !messages.length ? <div className="welcome">
        <div className="welcome-icon"><Code2 size={30} strokeWidth={1.5} /></div>
        <p className="eyebrow">LET’S BUILD SOMETHING</p>
        <h1>从一个想法开始。</h1>
        <p>描述你想实现的功能，或需要修复的问题。<br />在这里对话，查看改动，再一起迭代。</p>
        <div className="example-prompts"><span>实现一个接口</span><span>修复失败测试</span><span>解释项目代码</span></div>
      </div> : <div className="message-list">{messages.map(message => <article key={message.id} className={`chat-message chat-message-${message.role}`}>
        <span className="message-avatar">{message.role === "user" ? "你" : <Code2 size={17} />}</span>
        <div className="message-content"><strong>{message.role === "user" ? "需求" : "Coding Agent"}</strong><MessageContent content={message.content} /></div>
      </article>)}</div>}
    </div>
    {unread && <button className="new-messages secondary" onClick={() => { follow.current = true; setUnread(false); if (log.current) log.current.scrollTop = log.current.scrollHeight; }}><ArrowDown size={14} />有新消息</button>}
  </div>;
}

function MessageContent({ content }: { content: string }) {
  return <>{content.split(/(```[\s\S]*?(?:```|$))/g).filter(Boolean).map((part, index) => {
    if (!part.startsWith("```")) return <p key={index}>{part}</p>;
    const body = part.slice(3).replace(/```$/, "");
    const newline = body.indexOf("\n");
    const language = newline >= 0 ? body.slice(0, newline).trim() : "";
    return <div className="code-block" key={index}>{language && <span>{language}</span>}<pre><code>{newline >= 0 ? body.slice(newline + 1) : body}</code></pre></div>;
  })}</>;
}
