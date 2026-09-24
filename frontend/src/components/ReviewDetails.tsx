import type { ReviewState, RunEvent, RunRecord, Usage } from "../types";
import { reviewActivity, reviewLabel, reviewVersionLabel, targetLabel, usageLabel } from "../utils";
import { FindingsPanel } from "./FindingsPanel";

// Events retain earlier delegations; run.review remains the authoritative latest report.
export function reviewRounds(run: RunRecord, events: RunEvent[]): ReviewState[] {
  const rounds = new Map<string, ReviewState>();
  for (const event of events) {
    if (event.run_id && event.run_id !== run.run_id) continue;
    const id = event.payload?.subtask_id;
    if (typeof id !== "string") continue;
    if (event.type === "delegate.started" || event.type === "delegate.completed") {
      rounds.set(id, { ...rounds.get(id), ...(event.payload as ReviewState),
        status: event.type === "delegate.started" ? "running" : String(event.payload?.status),
      });
    } else if (event.type === "review.disposition" && rounds.has(id)) {
      const round = rounds.get(id)!;
      const item = event.payload as unknown as NonNullable<ReviewState["dispositions"]>[number];
      round.dispositions = [...(round.dispositions || []).filter(old => old.finding_id !== item.finding_id), item];
    }
  }
  if (run.review?.subtask_id) rounds.set(run.review.subtask_id, { ...rounds.get(run.review.subtask_id), ...run.review });
  return [...rounds.values()].map(round => round.status === "running" && !["queued", "running", "waiting_approval"].includes(run.status)
    ? { ...round, status: "interrupted" } : round);
}

function totalUsage(rounds: ReviewState[]): Usage | null {
  if (!rounds.length) return null;
  const sum = (field: "input_tokens" | "output_tokens") => rounds.every(round => round.usage?.[field] != null)
    ? rounds.reduce((value, round) => value + round.usage![field]!, 0) : null;
  // Cost may use different price configurations; per-round estimates remain visible below.
  return { input_tokens: sum("input_tokens"), output_tokens: sum("output_tokens") };
}

export function ReviewDetails({ run, events }: { run: RunRecord; events: RunEvent[] }) {
  const review = run.review;
  const target = review?.target || run.review_target;
  const rounds = reviewRounds(run, events);
  const enabled = review?.mode !== "off" && review?.enabled !== false;
  return <section className="review-details" aria-label="Reviewer 协作">
    <h3>Reviewer · {reviewActivity(run, events)}</h3>
    {enabled && <>
      <p>{targetLabel(target)}{target?.paths?.length ? ` · ${target.paths.join("、")}` : ""}</p>
      {target?.focus && <p>重点：{target.focus}</p>}
      <p className="metadata">报告轮次 {review?.subtask_id || "尚未产生"} · 审查版本 {target?.workspace_revision || review?.workspace_revision || "未知"}<br />基线 {target?.baseline || "未知 / 不适用"}</p>
      {review?.workspace_revision && <p className="review-notice">{reviewVersionLabel(run)}</p>}
      <p>本轮审查累计用量：{usageLabel(review?.usage)}</p>
      <p className="metadata">已加载审查累计：{usageLabel(totalUsage(rounds))}。每次审查已包含模型各轮用量，也计入任务总用量，不应重复相加；费用按下方各轮记录展示。</p>
      {review?.warnings?.map((warning, index) => <p className="error" key={index}>{warning}</p>)}
      <details className="review-coverage"><summary>目标范围与实际读取</summary>
        <p>目标选中文件：{review?.selected_paths?.join("、") || "暂无记录"}</p>
        <p>已提供内容的文件：{review?.coverage?.join("、") || "暂无记录"}（不代表全文审查）</p>
        <p>实际提供给 Reviewer 的读取记录：</p>
        {review?.read_records?.length ? <ul>{review.read_records.map((record, index) => <li key={index}>{record.path}{record.start_line != null ? `:${record.start_line}-${record.end_line ?? "?"}` : ""} · {record.source || "上下文"}</li>)}</ul> : <p>暂无读取记录。</p>}
        <p>未检查 / 限制：</p>{review?.unchecked?.length ? <ul>{review.unchecked.map((reason, index) => <li key={index}>{reason}</li>)}</ul> : <p>暂无已记录限制；不等于全仓覆盖。</p>}
      </details>
    </>}
    <FindingsPanel review={review} isLoading={false} taskMode={run.task_mode} />
    {rounds.length > 0 && <details className="review-history"><summary>审查轮次记录（{rounds.length}）</summary>
      {rounds.map(round => <article key={round.subtask_id}>
        <strong>{round.subtask_id} · {reviewLabel("default", round.status)}</strong>
        <p>{targetLabel(round.target)} · {round.target?.paths?.join("、") || "目标全部路径"}{round.target?.focus ? ` · 重点：${round.target.focus}` : ""}</p>
        <p>发现问题：{round.findings?.length ?? round.findings_count ?? "未知"}</p>
        <p className="metadata">版本 {round.target?.workspace_revision || round.workspace_revision || "未知"} · {usageLabel(round.usage)}</p>
        {round.dispositions?.map(item => <p key={item.finding_id}>{item.finding_id} · {item.status === "fixed" ? "Agent 标记已修复" : "不采纳"}：{item.reason}</p>)}
      </article>)}
    </details>}
  </section>;
}
