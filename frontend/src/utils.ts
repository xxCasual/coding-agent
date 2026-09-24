import type { Finding, RunStatus, Severity, UiStatus, Usage, Verification, TaskMode, ReviewTarget, RunRecord, RunEvent } from "./types";
import { TERMINAL_RUN_STATUSES } from "./types";

export const POLL_DELAY_MS = 2000;

const STATUS_LABELS: Record<string, string> = {
  idle: "等待输入",
  submitting: "提交中",
  queued: "排队中",
  running: "执行中",
  waiting_approval: "等待审批",
  interrupted: "已中断",
  needs_attention: "待核对",
  succeeded: "流程已结束",
  failed: "失败",
  cancelled: "已取消",
};

const SEVERITY_ORDER: Record<Severity, number> = {
  critical: 0,
  high: 1,
  medium: 2,
  low: 3,
  info: 4,
};

export function statusLabel(status: UiStatus | RunStatus | string): string {
  return STATUS_LABELS[status] ?? status;
}

export function isTerminalRunStatus(status: string | null | undefined): boolean {
  return TERMINAL_RUN_STATUSES.includes(status as RunStatus);
}

export function sortFindings(findings: Finding[]): Finding[] {
  return [...findings].sort((a, b) => {
    const severity = (SEVERITY_ORDER[a.severity] ?? 9) - (SEVERITY_ORDER[b.severity] ?? 9);
    if (severity !== 0) return severity;
    if (a.is_blocking !== b.is_blocking) return a.is_blocking ? -1 : 1;
    return b.confidence - a.confidence || a.file_path.localeCompare(b.file_path);
  });
}

export function findingKey(finding: Finding): string {
  return [finding.finding_id, finding.file_path, finding.start_line, finding.end_line, finding.title].join(":");
}

export function confidenceLabel(value: number): string {
  return `confidence ${Math.round(value * 100)}%`;
}

export function copyTextFallback(text: string): void {
  const textArea = document.createElement("textarea");
  textArea.value = text;
  textArea.setAttribute("readonly", "");
  textArea.style.position = "fixed";
  textArea.style.opacity = "0";
  textArea.style.pointerEvents = "none";
  document.body.append(textArea);
  textArea.select();
  document.execCommand("copy");
  textArea.remove();
}

export function makeMessageId(prefix: string): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return `${prefix}-${crypto.randomUUID()}`;
  }
  return `${prefix}-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

export function verificationLabel(verification: Verification | null | undefined): string {
  const status = verification?.status;
  if (!status || status === "unverified") return "未验证";
  if (status === "not_applicable") return "不适用";
  if (status === "passed") return "验证通过";
  if (status === "failed") return "验证失败";
  return status;
}

export function usageLabel(usage: Usage | null | undefined): string {
  if (!usage) return "未知";
  const input = usage.input_tokens;
  const output = usage.output_tokens;
  if (input == null && output == null && usage.estimated_cost == null) return "未知";
  const tokens = `in ${input ?? "未知"} / out ${output ?? "未知"}`;
  if (usage.estimated_cost == null) return `${tokens}；费用未知`;
  const asOf = usage.price_as_of ? `（估价日期 ${usage.price_as_of}）` : "";
  return `${tokens}；约 ${usage.estimated_cost}${asOf}`;
}

export function reviewLabel(mode: string | undefined, status: string | null | undefined): string {
  if (mode === "off") return "未启用";
  if (status === "unavailable") return "未完成";
  if (!status || status === "pending") return "待开始";
  if (status === "running") return "执行中";
  if (status === "interrupted") return "已中断，未完成";
  if (status === "completed") return "已完成";
  if (status === "failed") return "失败";
  return status;
}

export function taskModeLabel(mode?: TaskMode): string {
  return mode === "review" ? "只审查" : mode === "plan" ? "只规划" : "开发";
}

export function targetLabel(target?: ReviewTarget | null): string {
  if (!target) return "范围尚未确定";
  return { workspace_changes: "原仓创建任务时的未提交改动", run_changes: "本任务相对输入基线的改动", paths: "指定路径的源码快照" }[target.kind];
}

export function currentVerificationLabel(run: RunRecord): string {
  const verification = run.verification;
  if (verification?.status === "passed" || verification?.status === "failed") {
    if (!run.workspace_revision || !verification.workspace_revision) return "验证版本未知";
    if (verification.workspace_revision !== run.workspace_revision) return "验证已过期，当前版本未验证";
  }
  return verificationLabel(verification);
}

export function reviewVersionLabel(run: RunRecord): string {
  const review = run.review;
  if (!review?.workspace_revision) return "审查版本未知";
  if (review.target?.kind === "workspace_changes") return "审查针对原仓输入快照，不代表当前任务改动已审查";
  if (!run.workspace_revision) return "当前代码版本未知";
  return review.workspace_revision === run.workspace_revision ? "审查对应当前代码版本" : "审查已过期，不代表当前版本";
}

export function reviewActivity(run: RunRecord, events: RunEvent[]): string {
  if (run.review?.mode === "off" || run.review?.enabled === false) return "未启用";
  const last = events.filter(event => (!event.run_id || event.run_id === run.run_id) && event.type === "delegate.started").at(-1);
  if (last && last.payload?.subtask_id !== run.review?.subtask_id) {
    const completed = events.find(event => event.type === "delegate.completed" && event.payload?.subtask_id === last.payload?.subtask_id);
    if (completed) return `${reviewLabel("default", String(completed.payload?.status))} · 正在同步报告`;
    return ["queued", "running", "waiting_approval"].includes(run.status) ? "执行中" : "未完成";
  }
  return reviewLabel(run.review?.mode, run.review?.status);
}
