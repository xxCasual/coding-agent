import { AlertCircle, CheckCircle2, Info } from "lucide-react";
import type { InlineStatus as InlineStatusModel } from "../types";

interface InlineStatusProps {
  status: InlineStatusModel;
}

export function InlineStatus({ status }: InlineStatusProps) {
  const Icon =
    status.tone === "error" ? AlertCircle : status.tone === "success" ? CheckCircle2 : Info;

  return (
    <p className={`inline-status inline-status-${status.tone}`} role="status">
      <Icon aria-hidden="true" size={16} />
      <span>{status.message}</span>
    </p>
  );
}
