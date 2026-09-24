import { AlertTriangle, MapPin } from "lucide-react";
import { useMemo } from "react";
import type { Finding, ReviewState, FindingDisposition, TaskMode } from "../types";
import { confidenceLabel, findingKey, sortFindings, reviewLabel } from "../utils";
import { EmptyState } from "./EmptyState";

interface FindingsPanelProps {
  review: ReviewState | null | undefined;
  isLoading: boolean;
  taskMode?: TaskMode;
}

export function FindingsPanel({ review, isLoading, taskMode }: FindingsPanelProps) {
  const findings = review?.findings ?? [];
  const sortedFindings = useMemo(() => sortFindings(findings), [findings]);
  const target = reviewLabelTarget(review);

  return (
    <section className="result-panel findings-panel" aria-labelledby="findings-title">
      <div className="panel-heading">
        <div>
          <p className="eyebrow">Reviewer Findings</p>
          <h2 id="findings-title">问题列表</h2>
        </div>
        <span id="review-target" className="target-label" title={target}>
          {target}
        </span>
      </div>

      {sortedFindings.length === 0 ? (
        <EmptyState>{emptyCopy(review, isLoading)}</EmptyState>
      ) : (
        <div id="findings-list" className="findings-list">
          {sortedFindings.map((finding) => (
            <FindingCard key={findingKey(finding)} finding={finding} disposition={review?.dispositions?.find(item => item.finding_id === finding.finding_id)} taskMode={taskMode} />
          ))}
        </div>
      )}
    </section>
  );
}

function emptyCopy(review: ReviewState | null | undefined, isLoading: boolean): string {
  if (isLoading) return "任务正在执行，审查结果会随后出现。";
  if (!review || review.mode === "off" || review.enabled === false) return "此任务未启用 Reviewer。";
  if (review.status === "unavailable") return "Reviewer 未完成，不能当作无问题。";
  if (!review.status) return "尚未产生审查结果。";
  if (review.status !== "completed") return "审查尚未完成，不能当作无问题。";
  return "本次审查没有发现问题；结论仅限已提供的内容，请结合未检查项阅读。";
}

function reviewLabelTarget(review: ReviewState | null | undefined): string {
  if (!review || review.mode === "off" || review.enabled === false) return "Reviewer off";
  if (review.status === "unavailable") return "未完成";
  return reviewLabel(review.mode, review.status);
}

function FindingCard({ finding, disposition, taskMode }: { finding: Finding; disposition?: FindingDisposition; taskMode?: TaskMode }) {
  return (
    <article className={`finding-card finding-card-${finding.severity}`}>
      <div className="finding-card__top">
        <div className="finding-title-block">
          <h3>{finding.title}</h3>
          <p className="finding-location">
            <MapPin aria-hidden="true" size={14} />
            <span>
              {finding.file_path}:{finding.start_line}-{finding.end_line}
            </span>
          </p>
        </div>
        <span className={`severity severity-${finding.severity}`}>{finding.severity}</span>
      </div>

      <div className="finding-meta">
        <span>{finding.category}</span>
        <span>{finding.is_blocking ? "blocking" : "non-blocking"}</span>
        <span>{confidenceLabel(finding.confidence)}</span>
      </div>

      <div className="finding-body">
        <FindingDetail label="证据" value={finding.evidence} />
        <FindingDetail label="解释" value={finding.explanation} />
        <FindingDetail label="建议" value={finding.suggestion} />
      </div>

      <div className="finding-footnote">
        {taskMode === "review" ? <span>仅报告，不自动修复。</span> : disposition ? <div>
          <strong>{disposition.status === "fixed" ? "Agent 标记已修复" : "不采纳"}</strong>
          <p>理由：{disposition.reason}</p>
          {disposition.status === "fixed" && <small>这是 Agent 的处理记录；是否验证通过以当前代码版本的验证结果为准。</small>}
        </div> : <><AlertTriangle aria-hidden="true" size={15} /><span>尚未记录处理结果</span></>}

      </div>
    </article>
  );
}

function FindingDetail({ label, value }: { label: string; value: string }) {
  return (
    <p>
      <strong>{label}: </strong>
      <span>{value || "-"}</span>
    </p>
  );
}
