import { useEffect, useState } from "react";
import { Copy } from "lucide-react";
import { getArtifactText, isAbortError, listArtifacts } from "../api";
import type { Artifact, RunEvent, RunRecord } from "../types";
import { copyTextFallback, usageLabel, currentVerificationLabel, taskModeLabel } from "../utils";
import { ReviewDetails } from "./ReviewDetails";

export function ReportPanel({ run, events }: { run: RunRecord; events: RunEvent[] }) {
  const [artifacts, setArtifacts] = useState<Artifact[]>([]);
  const [selected, setSelected] = useState("");
  const [text, setText] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [copied, setCopied] = useState(false);
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setError("");
    void listArtifacts(run.run_id, controller.signal).then(items => {
      if (!controller.signal.aborted) setArtifacts(items);
    }).catch(err => { if (!isAbortError(err) && !controller.signal.aborted) setError("产物列表读取失败，请重试。"); });
    return () => controller.abort();
  }, [run.run_id, run.status, retry]);
  useEffect(() => {
    const controller = new AbortController();
    setText(""); setCopied(false); setError(""); setLoading(Boolean(selected));
    if (selected) void getArtifactText(run.run_id, selected, controller.signal).then(value => {
      if (!controller.signal.aborted) setText(value);
    }).catch(err => { if (!isAbortError(err) && !controller.signal.aborted) setError("产物读取失败，请重试。"); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [run.run_id, selected, retry]);
  const output = selected ? text : events.map(event => `#${event.seq ?? "-"} ${event.type}: ${event.type === "model.text_delta" && typeof event.payload?.text === "string" ? event.payload.text : event.message || ""}`).join("\n");
  return <div className="task-report">
    <p className="metadata">模式：{taskModeLabel(run.task_mode)} · 来源任务 {run.workspace_snapshot?.source_run_id || "登记原仓"} · 输入版本 {run.workspace_snapshot?.source_revision || "未知"}</p>
    <div className="verification-summary"><strong>{currentVerificationLabel(run)}</strong><span>用量：{usageLabel(run.usage)}</span></div>
    {run.verification?.command && <pre className="verification-command">{run.verification.command}{"\n"}退出码：{run.verification.exit_code ?? "未知"}</pre>}
    <ReviewDetails run={run} events={events} />
    <div className="report-toolbar"><label className="sr-only" htmlFor="artifact">查看产物或轨迹</label><select id="artifact" value={selected} onChange={event => setSelected(event.target.value)}>
      <option value="">执行轨迹</option>{artifacts.map(item => <option value={item.artifact_id} key={item.artifact_id}>{item.kind} · {item.summary}</option>)}
    </select>{selected && <a className="artifact-download" href={`/api/runs/${encodeURIComponent(run.run_id)}/artifacts/${encodeURIComponent(selected)}`} download>下载</a>}<button className="secondary" disabled={!output || loading} onClick={async () => {
      try { if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(output); else copyTextFallback(output); setCopied(true); }
      catch { setError("复制失败，请手动选择文本复制。"); }
    }}><Copy size={14} />{copied ? "已复制" : "复制"}</button></div>
    {error && <p className="error" role="alert">{error} <button className="secondary" onClick={() => setRetry(value => value + 1)}>重试</button></p>}
    <pre className="report-output">{loading ? "正在读取产物…" : output || "暂时没有内容。"}</pre>
    <p className="metadata">任务 {run.run_id} · 版本 {run.workspace_revision || "未知"}</p>
  </div>;
}
