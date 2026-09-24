import { AlertTriangle, CheckCircle2, Clock3, Hash, ListChecks, ShieldAlert } from "lucide-react";
import type { ReactNode } from "react";
import type { ConnectionStatus, RunRecord } from "../types";
import { reviewLabel, statusLabel, usageLabel, currentVerificationLabel } from "../utils";

interface StatusStripProps {
  run: RunRecord | null;
  connection: ConnectionStatus;
}

export function StatusStrip({ run, connection }: StatusStripProps) {
  const cancelling = Boolean(run?.cancel_requested && run.status !== "cancelled");
  const statusValue = cancelling ? "取消中" : run ? statusLabel(run.status) : "等待输入";
  const StatusIcon = run?.status === "failed" ? AlertTriangle : run?.status === "succeeded" ? CheckCircle2 : Clock3;
  const connectionLabel =
    connection === "connected" ? "已连接" : connection === "reconnecting" ? "重连中" : connection === "offline" ? "已断线" : "空闲";

  return (
    <section className="status-band" aria-live="polite">
      <StatusItem icon={<StatusIcon aria-hidden="true" size={18} />} label="当前状态" value={`${statusValue} · ${connectionLabel}`} />
      <StatusItem icon={<Hash aria-hidden="true" size={18} />} label="Run ID" value={run?.run_id ?? "-"} />
      <StatusItem
        icon={<ListChecks aria-hidden="true" size={18} />}
        label="验证"
        value={run ? currentVerificationLabel(run) : "-"}
      />
      <StatusItem
        icon={<ShieldAlert aria-hidden="true" size={18} />}
        label="用量 / Reviewer"
        value={run ? `${usageLabel(run.usage)}；${reviewLabel(run.review?.mode, run.review?.status)}` : "-"}
      />
    </section>
  );
}

function StatusItem({ icon, label, value }: { icon: ReactNode; label: string; value: string }) {
  return (
    <div className="status-item">
      <span className="status-icon">{icon}</span>
      <span className="status-label">{label}</span>
      <strong title={value}>{value}</strong>
    </div>
  );
}
