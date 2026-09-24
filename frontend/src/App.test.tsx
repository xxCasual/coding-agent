import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { App } from "./App";
import { conversationMessages } from "./components/ChatPanel";
import { ReportPanel } from "./components/ReportPanel";
import { eventDedupeKey, parseSseChunk } from "./sse";
import type { ChatMessage, RunRecord, SessionSummary } from "./types";

const workspace = { workspace_id: "demo", display_name: "Demo", path: "/tmp/demo" };
const session = { session_id: "sess-1", workspace_id: "demo", created_at: "2026-09-10" };
const baseRun: RunRecord = {
  run_id: "run-1", session_id: "sess-1", status: "queued", requirement: "fix tests", profile_id: "deepseek",
  cancel_requested: false, needs_attention: false, created_at: "2026-09-10", updated_at: "2026-09-10",
  verification: { status: "unverified" }, usage: { input_tokens: null, output_tokens: null }, review: { mode: "default", status: null, findings: [] },
};
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const packet = (seq: number, type: string, payload = {}, message = "", runId = "run-1") => `id: ${seq}\nevent: ${type}\ndata: ${JSON.stringify({ run_id: runId, seq, type, payload, message })}\n\n`;
const sse = (text: string) => new Response(text, { headers: { "Content-Type": "text/event-stream" } });

function fixture(initialRuns: RunRecord[] = [], initialSessions: SessionSummary[] = []) {
  const state = { runs: initialRuns, sessions: initialSessions, messages: [] as ChatMessage[], trace: "", workspaces: [workspace] };
  const posts: { url: string; body: Record<string, string>; key?: string }[] = [];
  let override: ((url: string, init?: RequestInit) => Response | Promise<Response> | undefined) | undefined;
  const fetcher = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (init?.method === "POST") posts.push({ url, body: init.body ? JSON.parse(String(init.body)) : {}, key: (init.headers as Record<string, string>)?.["Idempotency-Key"] });
    const intercepted = override?.(url, init);
    if (intercepted !== undefined) return intercepted;
    if (url === "/api/workspaces") return json(state.workspaces);
    if (url === "/api/sessions" && init?.method === "POST") { state.sessions.push(session); return json(session); }
    if (url.startsWith("/api/sessions?")) return json({ items: state.sessions, next_cursor: null });
    const detail = url.match(/^\/api\/sessions\/([^/?]+)(\?|$)/);
    if (detail) return json({ ...state.sessions.find(item => item.session_id === detail[1]), messages: state.messages.filter(item => item.session_id === detail[1]), active_run_id: state.runs.find(run => run.session_id === detail[1] && !["succeeded", "failed", "cancelled"].includes(run.status))?.run_id || null, next_cursor: null });
    if (url.includes("/sessions/") && url.includes("/runs")) {
      const id = url.split("/")[3];
      if (init?.method === "POST") {
        const body = JSON.parse(String(init.body));
        const run = { ...baseRun, run_id: `run-${state.runs.length + 1}`, session_id: id, requirement: body.requirement, task_mode: body.task_mode, review_target: body.review_target, status: "succeeded" as const };
        state.runs.push(run);
        state.messages.push({ id: body.message_id, message_id: body.message_id, session_id: id, run_id: run.run_id, role: "user", content: body.requirement });
        return json(run);
      }
      return json({ items: state.runs.filter(run => run.session_id === id), next_cursor: null });
    }
    if (url.includes("/events")) return sse(state.trace);
    if (url.endsWith("/artifacts")) return json([]);
    const run = state.runs.find(item => url.startsWith(`/api/runs/${item.run_id}`));
    if (url.endsWith("/cancel") && run) { run.cancel_requested = true; return json(run); }
    if (url.endsWith("/messages") && run) {
      const body = JSON.parse(String(init?.body));
      const stored = { id: body.message_id, message_id: body.message_id, run_id: run.run_id, session_id: run.session_id, role: "user", content: body.content };
      state.messages.push(stored); return json(stored);
    }
    if (url.includes("/approvals/") || url.endsWith("/resume")) return json({ ok: true });
    if (run) return json(run);
    return json({ detail: { message: `Unexpected request ${url}` } }, 404);
  });
  vi.stubGlobal("fetch", fetcher);
  return { state, posts, fetcher, override: (handler: NonNullable<typeof override>) => { override = handler; } };
}
const type = (value = "fix tests") => fireEvent.change(screen.getByRole("textbox", { name: "任务需求" }), { target: { value } });
async function ready() { await waitFor(() => expect(screen.getByRole("textbox", { name: "任务需求" })).toBeEnabled()); }
async function send(value = "fix tests") { type(value); fireEvent.click(screen.getByRole("button", { name: "发送" })); await waitFor(() => expect(screen.getByRole("textbox", { name: "任务需求" })).toHaveValue("")); }

// An open stream exercises updates while the HTTP connection is still alive.
function openStream(signal?: AbortSignal | null) {
  let controller: ReadableStreamDefaultController<Uint8Array>;
  const body = new ReadableStream<Uint8Array>({ start(value) { controller = value; } });
  signal?.addEventListener("abort", () => { try { controller.close(); } catch { /* already closed */ } }, { once: true });
  return { response: new Response(body), close: () => controller.close(), push: (text: string) => controller.enqueue(new TextEncoder().encode(text)) };
}

describe("chat MVP", () => {
  it("shows the registry hint without enabling send when there are no projects", async () => {
    const f = fixture(); f.state.workspaces = [];
    render(<App />);
    expect(await screen.findByText(/没有已登记 workspace/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "发送" })).toBeDisabled();
  });

  it("creates a conversation and starts another run for the same text after completion", async () => {
    const f = fixture(); render(<App />); await ready();
    await send(); await screen.findByText("流程已结束");
    await send(); await waitFor(() => expect(f.state.runs).toHaveLength(2));
    expect(f.posts.filter(post => post.url === "/api/sessions")).toHaveLength(1);
    const runs = f.posts.filter(post => post.url.endsWith("/runs"));
    expect(runs[0].key).toBeTruthy(); expect(runs[1].key).not.toBe(runs[0].key);
    expect(screen.getAllByText("fix tests")).toHaveLength(2);
  });

  it("preserves a failed draft and reuses its idempotency key on retry", async () => {
    const f = fixture(); let fail = true;
    f.override((url, init) => { if (url.endsWith("/runs") && init?.method === "POST" && fail) { fail = false; return json({ detail: { message: "暂时无法提交" } }, 503); } });
    render(<App />); await ready(); type(); fireEvent.click(screen.getByRole("button", { name: "发送" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("输入已保留"); expect(screen.getByRole("textbox")).toHaveValue("fix tests");
    fireEvent.click(screen.getByRole("button", { name: "发送" }));
    await screen.findByText("流程已结束");
    const posts = f.posts.filter(post => post.url.endsWith("/runs"));
    expect(posts[1].key).toBe(posts[0].key); expect(posts[1].body.message_id).toBe(posts[0].body.message_id);
    expect(f.state.runs).toHaveLength(1);
  });

  it("appends to an active task and does not create a second run", async () => {
    const f = fixture([{ ...baseRun }], [session]); render(<App />); await ready(); await screen.findByRole("button", { name: "取消" });
    await send("保留现有接口");
    expect(f.posts.find(post => post.url.endsWith("/messages"))?.body.content).toBe("保留现有接口");
    expect(f.posts.some(post => post.url.endsWith("/runs"))).toBe(false);
    expect(await screen.findByText("保留现有接口")).toBeInTheDocument();
  });

  it("renders model text and approval before an open event stream closes", async () => {
    const run = { ...baseRun };
    const f = fixture([run], [session]); let stream: ReturnType<typeof openStream> | undefined;
    f.override((url, init) => { if (url.includes("/events")) { stream = openStream(init?.signal); return stream.response; } });
    render(<App />); await waitFor(() => expect(stream).toBeDefined());
    run.status = "waiting_approval";
    run.pending_approval = { approval_id: "intent:c1", intent: { name: "apply_patch" }, param_summary: "更新 app.py", workspace_revision: "rev1" };
    await act(async () => { stream!.push(packet(1, "model.text_delta", { text: "我会先检查测试。" }) + packet(2, "model.done") + packet(3, "approval.requested")); });
    expect(await screen.findByText("我会先检查测试。")).toBeInTheDocument();
    fireEvent.click(await screen.findByRole("button", { name: "允许" }));
    await waitFor(() => expect(f.posts.some(post => post.url === "/api/approvals/intent%3Ac1/decision" && post.body.decision === "allow")).toBe(true));
  });

  it("shows cancellation pending until the service confirms the terminal state", async () => {
    fixture([{ ...baseRun }], [session]); render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "取消" }));
    expect(await screen.findByText("已请求取消，等待服务确认退出。")).toBeInTheDocument();
    expect(screen.queryByText("已取消")).not.toBeInTheDocument();
    type("继续修复"); expect(screen.getByRole("button", { name: "发送" })).toBeDisabled();
  });

  it("blocks sending during attention and passes the revision to explicit recovery", async () => {
    const f = fixture([{ ...baseRun, status: "needs_attention", needs_attention: true, workspace_revision: "rev-1", attention: { call_id: "call1", reason: "请核对文件" } }], [session]);
    render(<App />); await screen.findByText("请核对文件"); type("继续");
    expect(screen.getByRole("button", { name: "发送" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "接受当前状态继续验证" }));
    await waitFor(() => expect(f.posts.some(post => post.url.endsWith("/resume") && post.body.workspace_revision === "rev-1" && post.body.call_id === "call1")).toBe(true));
  });

  it("ignores delayed session responses after selecting another conversation", async () => {
    const other = { ...session, session_id: "sess-2", created_at: "2026-09-09" };
    const f = fixture([], [session, other]); let resolveOld: (response: Response) => void = () => {};
    f.override(url => { if (url.startsWith("/api/sessions/sess-1?")) return new Promise<Response>(resolve => { resolveOld = resolve; }); });
    render(<App />);
    fireEvent.click(await screen.findByTitle("sess-2")); await ready();
    await act(async () => resolveOld(json({ ...session, active_run_id: null, messages: [{ message_id: "old", run_id: "run1", role: "assistant", content: "旧会话内容" }], next_cursor: null })));
    expect(screen.getByTitle("sess-2")).toHaveAttribute("aria-current", "page");
    expect(screen.queryByText("旧会话内容")).not.toBeInTheDocument();
    await send("新会话需求"); expect(f.posts.find(post => post.url.endsWith("/runs"))?.url).toBe("/api/sessions/sess-2/runs");
  });

  it("prevents sending into a session whose state failed to load", async () => {
    const f = fixture([], [session]);
    f.override(url => url.startsWith("/api/sessions/sess-1?") ? json({ detail: { message: "会话读取失败" } }, 503) : undefined);
    render(<App />); await screen.findByRole("alert"); type("继续编码");
    expect(screen.getByRole("button", { name: "发送" })).toBeDisabled();
    expect(f.posts).toHaveLength(0);
  });

  it("supports multiline input and ignores Enter while composing Chinese", async () => {
    const f = fixture(); render(<App />); await ready(); type("实现接口\n保持兼容");
    const input = screen.getByRole("textbox");
    fireEvent.keyDown(input, { key: "Enter", isComposing: true, keyCode: 229 });
    fireEvent.keyDown(input, { key: "Enter", shiftKey: true }); expect(f.posts).toHaveLength(0);
    fireEvent.keyDown(input, { key: "Enter" }); await screen.findByText("流程已结束");
    expect(f.posts.find(post => post.url.endsWith("/runs"))?.body.requirement).toBe("实现接口\n保持兼容");
  });

  it("replays historical model text once and keeps verification separate from completion", async () => {
    const f = fixture([{ ...baseRun, status: "succeeded" }], [session]);
    f.state.messages = [{ id: "assistant", message_id: "assistant", session_id: session.session_id, run_id: "run-1", role: "assistant", content: "修改已完成" }];
    f.state.trace = packet(1, "model.text_delta", { text: "修改已完成" }) + packet(1, "model.text_delta", { text: "修改已完成" }) + packet(2, "model.done");
    render(<App />); await screen.findByText("流程已结束");
    expect(within(screen.getByRole("log")).getAllByText("修改已完成")).toHaveLength(1);
    expect(screen.getByRole("button", { name: /任务详情/ })).toHaveTextContent("未验证");
    fireEvent.click(screen.getByRole("button", { name: /任务详情/ }));
    expect(await screen.findByText("用量：未知")).toBeInTheDocument();
  });

  it("reconnects with the last sequence and deduplicates replayed events", async () => {
    const run = { ...baseRun };
    const f = fixture([run], [session]);
    let stream: ReadableStreamDefaultController<Uint8Array>;
    let connected = false;
    f.override((url, init) => {
      if (!url.includes("/events")) return;
      if (!connected) {
        connected = true;
        return new Response(new ReadableStream<Uint8Array>({ start(controller) { stream = controller; } }));
      }
      expect(url).toContain("after=2");
      expect((init?.headers as Record<string, string>)["Last-Event-ID"]).toBe("2");
      return sse(packet(1, "model.text_delta", { text: "第一段" }) + packet(2, "model.done") + packet(3, "model.text_delta", { text: "第二段" }) + packet(4, "model.done"));
    });
    render(<App />); await waitFor(() => expect(connected).toBe(true));
    await act(async () => stream.enqueue(new TextEncoder().encode(packet(1, "model.text_delta", { text: "第一段" }) + packet(2, "model.done"))));
    await screen.findByText("第一段");
    await act(async () => stream.error(new Error("disconnect")));
    expect(await screen.findByText("第二段", {}, { timeout: 3500 })).toBeInTheDocument();
    expect(within(screen.getByRole("log")).getAllByText("第一段")).toHaveLength(1);
  });

  it("ignores a late artifact response after selecting a different artifact", async () => {
    const f = fixture([{ ...baseRun, status: "succeeded" }], [session]);
    let resolvePatch: (response: Response) => void = () => {};
    f.override(url => {
      if (url.endsWith("/artifacts")) return json([{ artifact_id: "a", kind: "patch", summary: "改动" }, { artifact_id: "b", kind: "log", summary: "测试" }]);
      if (url.endsWith("/artifacts/a")) return new Promise<Response>(resolve => { resolvePatch = resolve; });
      if (url.endsWith("/artifacts/b")) return new Response("测试输出 B");
    });
    render(<App />); await screen.findByText("流程已结束");
    fireEvent.click(screen.getByRole("button", { name: /任务详情/ }));
    await screen.findByRole("option", { name: "patch · 改动" });
    fireEvent.change(screen.getByLabelText("查看产物或轨迹"), { target: { value: "a" } });
    fireEvent.change(screen.getByLabelText("查看产物或轨迹"), { target: { value: "b" } });
    await screen.findByText("测试输出 B");
    await act(async () => resolvePatch(new Response("旧产物 A")));
    expect(screen.getByText("测试输出 B")).toBeInTheDocument(); expect(screen.queryByText("旧产物 A")).not.toBeInTheDocument();
  });

  it("does not duplicate persisted assistant text when projecting event text", () => {
    const messages: ChatMessage[] = [{ id: "u", run_id: "run-1", role: "user", content: "fix tests" }, { id: "a", run_id: "run-1", role: "assistant", content: "done" }];
    expect(conversationMessages([baseRun], messages, { "run-1": [{ type: "model.text_delta", seq: 1, payload: { text: "done" } }, { type: "model.done", seq: 2 }] }).map(item => item.content)).toEqual(["fix tests", "done"]);
  });
});

describe("task modes and review collaboration", () => {
  const finding = { finding_id: "f1", hunk_id: "h1", file_path: "api.py", start_line: 8, end_line: 9, severity: "high" as const, category: "correctness", title: "缺少参数校验", evidence: "负数会返回成功", explanation: "缺少边界", suggestion: "检查输入", confidence: 0.9, is_blocking: true };
  const reviewed: RunRecord = { ...baseRun, status: "succeeded", task_mode: "develop", workspace_revision: "new", verification: { status: "passed", workspace_revision: "old" }, review: {
    enabled: true, mode: "default", status: "completed", subtask_id: "review-one", workspace_revision: "old",
    target: { kind: "paths", paths: ["api.py"], focus: "输入边界", workspace_revision: "old", baseline: "base" },
    selected_paths: ["api.py", "large.py"], coverage: ["api.py"], read_records: [{ path: "api.py", start_line: 1, end_line: 10, source: "read_file" }], unchecked: ["large.py: oversized"], findings: [finding],
    dispositions: [{ finding_id: "f1", status: "not_adopted", reason: "边界由调用方约束" }], usage: { input_tokens: 30, output_tokens: null },
  } };

  it("submits a path review with frozen scope and uses a new idempotency key when scope changes", async () => {
    const f = fixture(); let fail = true;
    f.override((url, init) => url.endsWith("/runs") && init?.method === "POST" && fail ? json({ detail: { message: "retry" } }, 503) : undefined);
    render(<App />); await ready();
    fireEvent.change(screen.getByLabelText("任务模式"), { target: { value: "review" } });
    fireEvent.change(screen.getByLabelText("目标"), { target: { value: "paths" } });
    type("检查输入"); expect(screen.getByRole("button", { name: "发送" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("路径（每行一项，相对仓库）"), { target: { value: "api.py\ntests/" } });
    fireEvent.change(screen.getByLabelText("审查重点（可选）"), { target: { value: "异常输入" } });
    fireEvent.click(screen.getByRole("button", { name: "发送" })); await screen.findByRole("alert");
    const first = f.posts.find(post => post.url.endsWith("/runs"))!;
    expect(first.body).toMatchObject({ task_mode: "review", review_target: { kind: "paths", paths: ["api.py", "tests/"], focus: "异常输入" } });
    fireEvent.change(screen.getByLabelText("审查重点（可选）"), { target: { value: "权限边界" } });
    fail = false; fireEvent.click(screen.getByRole("button", { name: "发送" })); await screen.findByText("流程已结束");
    expect(f.posts.filter(post => post.url.endsWith("/runs"))[1].key).not.toBe(first.key);
  });

  it("keeps active review mode and target locked while accepting a supplement", async () => {
    const f = fixture([{ ...baseRun, task_mode: "review", review_target: { kind: "paths", paths: ["api.py"], focus: "边界" } }], [session]);
    render(<App />); await screen.findByRole("button", { name: "取消" });
    expect(screen.getByLabelText("任务模式")).toHaveValue("review"); expect(screen.getByLabelText("任务模式")).toBeDisabled();
    expect(screen.getByLabelText("路径（每行一项，相对仓库）")).toHaveValue("api.py"); expect(screen.getByLabelText("目标")).toBeDisabled();
    await send("关注错误响应"); expect(f.posts.filter(post => post.url.endsWith("/messages"))).toHaveLength(1);
  });

  it("restores read-only mode when reopening a completed conversation", async () => {
    fixture([{ ...baseRun, status: "succeeded", task_mode: "plan", review: { mode: "off" } }], [session]);
    render(<App />); await ready();
    expect(screen.getByLabelText("任务模式")).toHaveValue("plan");
    expect(screen.getByLabelText("任务模式")).toBeEnabled();
  });

  it("ends planning at its report and develops only after an explicit new submission", async () => {
    const f = fixture(); render(<App />); await ready();
    fireEvent.change(screen.getByLabelText("任务模式"), { target: { value: "plan" } }); await send("先做方案");
    await screen.findByText("流程已结束");
    expect(f.posts.filter(post => post.url.endsWith("/runs"))).toHaveLength(1);
    expect(f.posts.find(post => post.url.endsWith("/runs"))?.body).toMatchObject({ task_mode: "plan", reviewer: "off" });
    fireEvent.change(screen.getByLabelText("任务模式"), { target: { value: "develop" } }); await send("按方案实施");
    await waitFor(() => expect(f.state.runs).toHaveLength(2));
    expect(f.posts.filter(post => post.url.endsWith("/runs"))[1].body.task_mode).toBe("develop");
  });

  it("keeps stale conclusions, partial coverage and Agent dispositions separate from current verification", async () => {
    fixture(); const { rerender } = render(<ReportPanel run={reviewed} events={[]} />);
    expect(screen.getByText("验证已过期，当前版本未验证")).toBeInTheDocument();
    expect(screen.getByText("审查已过期，不代表当前版本")).toBeInTheDocument();
    expect(screen.getAllByText(/不采纳/).length).toBeGreaterThan(0);
    expect(screen.getByText("理由：边界由调用方约束")).toBeInTheDocument();
    expect(screen.getByText("large.py: oversized")).toBeInTheDocument();
    expect(screen.getByText(/api.py:1-10/)).toBeInTheDocument();
    expect(screen.getByText(/本轮审查累计用量/)).toHaveTextContent("in 30 / out 未知");
    rerender(<ReportPanel run={{ ...reviewed, review: { ...reviewed.review, dispositions: [{ finding_id: "f1", status: "fixed", reason: "补上检查" }] } }} events={[]} />);
    expect(screen.getAllByText("Agent 标记已修复").length).toBeGreaterThan(0); expect(screen.queryByText("不采纳")).not.toBeInTheDocument();
    expect(screen.getByText("验证已过期，当前版本未验证")).toBeInTheDocument();
    rerender(<ReportPanel run={{ ...reviewed, task_mode: "review", review: { ...reviewed.review, target: { kind: "workspace_changes" } } }} events={[]} />);
    expect(screen.getByText(/审查针对原仓输入快照/)).toBeInTheDocument(); expect(screen.getByText("仅报告，不自动修复。")).toBeInTheDocument();
    rerender(<ReportPanel run={{ ...reviewed, status: "cancelled" }} events={[{ type: "delegate.started", seq: 3, payload: { subtask_id: "unfinished" } }]} />);
    expect(screen.getByText("unfinished · 已中断，未完成")).toBeInTheDocument();
  });

  it("updates Reviewer during an open stream and never labels an unavailable empty result clean", async () => {
    const run = { ...baseRun, status: "running" as const };
    const f = fixture([run], [session]); let stream: ReturnType<typeof openStream>;
    f.override((url, init) => { if (url.includes("/events")) { stream = openStream(init?.signal); return stream.response; } });
    render(<App />); await waitFor(() => expect(stream).toBeDefined());
    await act(async () => stream.push(packet(1, "delegate.started", { subtask_id: "sub-new", target: { kind: "paths", paths: ["api.py"] } })));
    expect(await screen.findByText("Reviewer · 执行中")).toBeInTheDocument();
    run.review = { ...run.review, subtask_id: "sub-new", status: "unavailable", findings: [] };
    await act(async () => stream.push(packet(2, "delegate.completed", { subtask_id: "sub-new", status: "unavailable", usage: null })));
    expect(await screen.findByText("Reviewer · 未完成")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /任务详情/ }));
    expect(screen.getByText("Reviewer 未完成，不能当作无问题。")).toBeInTheDocument();
  });

  it("reconnects review events without duplicating rounds or accepting another run's result", async () => {
    const run = { ...reviewed, status: "running" as const };
    const f = fixture([run], [session]); let stream: ReturnType<typeof openStream>; let connected = false;
    const start = packet(1, "delegate.started", { subtask_id: "review-one" });
    const complete = packet(2, "delegate.completed", { subtask_id: "review-one", status: "completed", usage: { input_tokens: 30, output_tokens: null } });
    f.override((url, init) => {
      if (!url.includes("/events")) return;
      if (!connected) { connected = true; stream = openStream(init?.signal); return stream.response; }
      expect(url).toContain("after=2");
      return sse(start + complete + packet(3, "delegate.started", { subtask_id: "foreign" }, "", "other-run"));
    });
    render(<App />); await waitFor(() => expect(connected).toBe(true));
    await act(async () => { stream.push(start + complete); stream.close(); });
    fireEvent.click(screen.getByRole("button", { name: /任务详情/ }));
    await waitFor(() => expect(f.fetcher.mock.calls.filter(([url]) => String(url).includes("after=2")).length).toBeGreaterThan(0), { timeout: 3500 });
    expect(screen.getByText("审查轮次记录（1）")).toBeInTheDocument(); expect(screen.queryByText(/foreign/)).not.toBeInTheDocument();
    expect(screen.getByText(/已加载审查累计/)).toHaveTextContent("in 30 / out 未知");
  });

  it("discards a delayed review response after switching tasks", async () => {
    const other = { ...baseRun, run_id: "run-2", status: "succeeded" as const, created_at: "2026-09-11", review: { mode: "off" } };
    const f = fixture([reviewed, other], [session]); let resolveOld: (response: Response) => void = () => {};
    f.override(url => url === "/api/runs/run-1" ? new Promise<Response>(resolve => { resolveOld = resolve; }) : undefined);
    render(<App />); await screen.findByText("流程已结束"); fireEvent.click(screen.getByRole("button", { name: /任务详情/ }));
    fireEvent.change(screen.getByLabelText("查看任务"), { target: { value: "run-1" } });
    await waitFor(() => expect(f.fetcher.mock.calls.some(([url]) => url === "/api/runs/run-1")).toBe(true));
    fireEvent.change(screen.getByLabelText("查看任务"), { target: { value: "run-2" } });
    await act(async () => resolveOld(json(reviewed)));
    expect(screen.getByText("此任务未启用 Reviewer。")).toBeInTheDocument(); expect(screen.queryByText("缺少参数校验")).not.toBeInTheDocument();
  });

  it("retains the delivered read-only report after model text streaming", () => {
    const messages = conversationMessages([baseRun], [], { "run-1": [
      { type: "model.text_delta", seq: 1, payload: { text: "准备审查" } }, { type: "model.done", seq: 2 },
      { type: "run.succeeded", seq: 3, payload: { task_mode: "review" }, message: "Review completed: 1 finding(s)." },
    ] });
    expect(messages.map(message => message.content)).toEqual(["fix tests", "准备审查", "Review completed: 1 finding(s)."]);
  });
});

describe("SSE parser", () => {
  it("preserves partial frames and scopes deduplication to a run", () => {
    const raw = packet(1, "model.text_delta", { text: "你好" });
    const first = parseSseChunk(raw.slice(0, 32));
    expect(first.events).toHaveLength(0);
    const second = parseSseChunk(first.rest + raw.slice(32));
    expect(second.events[0].payload?.text).toBe("你好");
    expect(eventDedupeKey("one", 1)).not.toBe(eventDedupeKey("two", 1));
  });
});
