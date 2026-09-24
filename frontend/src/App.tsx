import { useMemo, useState } from "react";
import { ArrowUp, Code2, FolderGit2, MessageSquare, Plus, Square } from "lucide-react";
import { ChatPanel, conversationMessages } from "./components/ChatPanel";
import { ReportPanel } from "./components/ReportPanel";
import { useCodingSession } from "./hooks/useCodingSession";
import { isTerminalRunStatus, statusLabel, currentVerificationLabel, taskModeLabel, reviewActivity, reviewVersionLabel } from "./utils";

export function App() {
  const app = useCodingSession();
  const [details, setDetails] = useState(false);
  const current = app.activeRun || app.selectedRun;
  const active = app.activeRun;
  const cancelling = Boolean(active?.cancel_requested);
  const sessionOptions = app.sessions.filter(item => item.workspace_id === app.workspaceId).sort((a, b) => b.created_at.localeCompare(a.created_at));
  const displayedMessages = useMemo(() => conversationMessages(app.runs, app.messages, app.events), [app.runs, app.messages, app.events]);
  const selectedWorkspace = app.workspaces.find(item => item.workspace_id === app.workspaceId);
  const locked = app.busy || app.controlBusy;
  const pending = active?.pending_approval;
  const hint = app.loading ? "正在加载…" : !app.workspaceId ? "请先登记并选择项目" : app.blocked ? (cancelling ? "等待服务确认取消，草稿会保留" : "请先处理任务提示，草稿会保留") : active ? "发送补充约束，Agent 将在后续步骤处理" : "描述需求，开始这一轮对话";
  return <main className="app-shell">
    <aside className="sidebar" aria-label="项目与会话">
      <div className="brand"><span className="brand-icon"><Code2 size={20} /></span><strong>Coding Agent</strong><span className="demo-badge">DEMO</span></div>
      <label className="sidebar-label" htmlFor="workspace-id">项目</label>
      <div className="workspace-select"><FolderGit2 size={16} /><select id="workspace-id" value={app.workspaceId} disabled={locked || app.loading} onChange={event => { app.changeWorkspace(event.target.value); setDetails(false); }}>
        {!app.workspaces.length && <option value="">暂无项目</option>}{app.workspaces.map(item => <option key={item.workspace_id} value={item.workspace_id}>{item.display_name}</option>)}
      </select></div>
      <button className="new-chat secondary" disabled={locked || app.loading || !app.workspaceId} onClick={() => { app.newSession(); setDetails(false); }}><Plus size={17} />新建对话</button>
      <p className="sidebar-label history-heading">最近会话 <span>{sessionOptions.length}</span></p>
      <nav className="session-list" aria-label="会话列表">{sessionOptions.map(item => <button className={`session-item ${app.sessionId === item.session_id ? "selected" : ""}`} key={item.session_id} disabled={locked} aria-current={app.sessionId === item.session_id ? "page" : undefined} title={item.session_id} onClick={() => { void app.openSession(item.session_id); setDetails(false); }}>
        <MessageSquare size={15} /><span><strong>{item.created_at.slice(0, 10)} 的对话</strong><small>{item.session_id.slice(0, 12)}</small></span>
      </button>)}{!sessionOptions.length && <p className="sidebar-empty">还没有对话。<br />从第一条需求开始。</p>}</nav>
      {app.partialSessions && <p className="sidebar-empty">仅展示当前已加载的会话。</p>}
      <div className="sidebar-footer"><span className="status-dot" />本地工作区<span>代码在隔离副本中执行</span></div>
    </aside>
    <section className="main-panel">
      <header className="topbar"><div className="topbar-title"><FolderGit2 size={17} /><strong>{selectedWorkspace?.display_name || "新对话"}</strong><span className="topbar-divider">/</span><span>{app.sessionId ? "编码会话" : "新对话"}</span></div>
        <div className="run-status" role="status"><span className={`status-dot ${active ? "working" : ""}`} />{cancelling ? "取消中" : current ? statusLabel(current.status) : "等待输入"}{app.connection === "reconnecting" ? " · 重连中" : app.connection === "offline" ? " · 已断线" : ""}</div>
      </header>
      {!app.loading && !app.workspaces.length && <div className="banner">没有已登记 workspace。请在服务端登记项目后刷新。<button className="secondary" onClick={() => void app.bootstrap()}>刷新</button></div>}
      {app.partialHistory && <p className="history-notice">仅展示当前已加载的历史，长会话记录可能不完整。</p>}
      <ChatPanel messages={displayedMessages} loading={app.loading} sessionKey={`${app.workspaceId}:${app.sessionId}`} />
      <div className="bottom-area">
        {current && <div className="collaboration-summary" aria-label="任务进展" aria-live="polite">
          <strong>{taskModeLabel(current.task_mode)}</strong>
          <span>Reviewer · {reviewActivity(current, app.events[current.run_id] || [])}</span>
          <span>{currentVerificationLabel(current)}</span>
          {current.review?.workspace_revision && <small>{reviewVersionLabel(current)}</small>}
          <small>{current.task_mode === "review" ? "交付审查报告，发现问题也可完成；不自动修复。" : current.task_mode === "plan" ? "交付实施计划；验证方案尚未执行。" : "主 Agent 实现 → Reviewer 反馈 → 处理问题 → 验证 → 交付"}</small>
        </div>}
        {active && <div className="task-activity"><span className="status-dot working" /><span>{active.dispatch_pending ? "任务正在排队，等待执行服务接收。" : active.status === "running" ? "Agent 正在执行，回复将在模型返回后显示。" : statusLabel(active.status)}</span></div>}
        {active && (pending || active.pending_approval_id) && <div className="action-bar" aria-label="审批">
          <div><strong>需要你的批准</strong><p>{String(pending?.intent?.name || "工具操作")} {pending?.param_summary || ""}</p><small>版本 {pending?.workspace_revision || active.workspace_revision || "未知"}</small></div>
          <button disabled={locked || cancelling} onClick={() => void app.control("allow")}>允许</button><button className="secondary" disabled={locked || cancelling} onClick={() => void app.control("deny")}>拒绝</button>
        </div>}
        {active && (active.needs_attention || active.status === "needs_attention") && <div className="action-bar"><div><strong>需要人工核对</strong><p>{active.attention?.reason || "请核对当前文件状态后继续。"}</p><small>{active.attention?.affected_files?.join(", ")}</small></div><button disabled={locked || cancelling} onClick={() => void app.control("accept_and_continue")}>接受当前状态继续验证</button><button className="secondary" disabled={locked || cancelling} onClick={() => void app.control("end_task")}>结束任务</button></div>}
        {active?.status === "interrupted" && !active.needs_attention && <div className="action-bar"><p>任务已中断，可以恢复执行。</p><button disabled={locked || cancelling} onClick={() => void app.control("continue")}>继续</button></div>}
        {app.runs.length > 0 && <div className="details-section"><button className="details-toggle" aria-expanded={details} onClick={() => setDetails(!details)}><span>{details ? "▾" : "▸"} 任务详情</span><span>{app.selectedRun ? currentVerificationLabel(app.selectedRun) : "正在读取状态"}</span></button>
          {details && <div className="details-body"><label htmlFor="run-id">查看任务</label><select id="run-id" value={app.selectedId || ""} onChange={event => app.selectRun(event.target.value)}>{app.runs.map((run, index) => <option key={run.run_id} value={run.run_id}>第 {index + 1} 轮 · {run.requirement.slice(0, 36)} · {statusLabel(run.status)}</option>)}</select>{app.selectedRun ? <ReportPanel key={app.selectedRun.run_id} run={app.selectedRun} events={app.events[app.selectedRun.run_id] || []} /> : <p>正在读取任务详情…</p>}</div>}
        </div>}
        {app.error && <div className="banner error" role="alert">{app.error}{!app.busy && <button className="secondary" onClick={() => app.sessionId ? void app.openSession(app.sessionId) : void app.bootstrap()}>重新加载</button>}</div>}
        {app.notice && !app.error && <p className="notice" role="status">{app.notice}</p>}
        <form className="composer" onSubmit={event => { event.preventDefault(); void app.send(); }}>
          <div className="mode-controls">
            <label htmlFor="task-mode">任务模式</label>
            <select id="task-mode" value={app.taskMode} disabled={locked || app.loading || Boolean(app.activeId)} onChange={event => app.setTaskMode(event.target.value as typeof app.taskMode)}>
              <option value="develop">开发</option><option value="review">只审查</option><option value="plan">只规划</option>
            </select>
            <small>{app.activeId ? "本轮模式已固定" : app.taskMode === "develop" ? "下一轮：实现、验证并交付改动" : app.taskMode === "review" ? "下一轮：只读检查并返回报告" : "下一轮：只读分析并返回实施计划"}</small>
          </div>
          {app.taskMode === "review" && <fieldset className="review-inputs" disabled={locked || app.loading || Boolean(app.activeId)}>
            <legend>审查范围</legend>
            <label htmlFor="review-kind">目标</label><select id="review-kind" value={app.reviewTarget?.kind} onChange={event => app.setTargetKind(event.target.value as typeof app.targetKind)}>
              <option value="workspace_changes">原仓已有未提交改动</option><option value="paths">指定文件或目录</option><option value="run_changes">本任务产生的改动</option>
            </select>
            <label htmlFor="review-paths">路径（每行一项，相对仓库）</label><textarea id="review-paths" rows={2} placeholder={app.reviewTarget?.kind === "paths" ? "例如 src/api.py，必填" : "留空表示目标内全部路径"} value={app.activeId ? (app.reviewTarget?.paths || []).join("\n") : app.reviewPaths} onChange={event => app.setReviewPaths(event.target.value)} />
            <label htmlFor="review-focus">审查重点（可选）</label><input id="review-focus" value={app.activeId ? app.reviewTarget?.focus || "" : app.reviewFocus} onChange={event => app.setReviewFocus(event.target.value)} />
            {app.reviewTarget?.kind === "run_changes" && <small>只审查任务不会修改源码，因此本任务改动通常为空；已有代码请选择指定路径。</small>}
          </fieldset>}
          <label className="sr-only" htmlFor="chat-message">任务需求</label><textarea id="chat-message" rows={3} placeholder="想一起做点什么？描述需求，或粘贴报错…" value={app.draft} disabled={app.loading || app.busy || !app.workspaceId} onChange={event => app.setDraft(event.target.value)} onKeyDown={event => {
            if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing && event.keyCode !== 229) { event.preventDefault(); void app.send(); }
          }} />
          <div className="composer-footer"><span>{hint}</span><div>{active && !isTerminalRunStatus(active.status) && <button type="button" className="secondary stop-button" disabled={locked || cancelling} onClick={() => void app.control("cancel")}><Square size={12} />{cancelling ? "取消中" : "取消"}</button>}<button className="send-button" type="submit" aria-label={app.busy ? "发送中" : "发送"} disabled={!app.draft.trim() || !app.workspaceId || app.blocked || app.invalidScope || locked}><ArrowUp size={19} /></button></div></div>
        </form>
        <p className="composer-note">Enter 发送 · Shift + Enter 换行<span>关闭页面不会取消任务</span></p>
      </div>
    </section>
  </main>;
}
