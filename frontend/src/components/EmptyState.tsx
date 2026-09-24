import type { ReactNode } from "react";

interface EmptyStateProps {
  children: ReactNode;
  compact?: boolean;
}

export function EmptyState({ children, compact = false }: EmptyStateProps) {
  return <div className={compact ? "empty-state empty-state-compact" : "empty-state"}>{children}</div>;
}
