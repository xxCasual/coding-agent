import { Play, RefreshCw } from "lucide-react";
import type { InlineStatus as InlineStatusModel, Workspace } from "../types";
import { InlineStatus } from "./InlineStatus";

interface TaskFormProps {
  workspaces: Workspace[];
  workspaceId: string;
  onWorkspaceChange: (value: string) => void;
  requirement: string;
  onRequirementChange: (value: string) => void;
  reviewer: "default" | "off";
  onReviewerChange: (value: "default" | "off") => void;
  onSubmit: () => void;
  isBusy: boolean;
  status: InlineStatusModel;
}

export function TaskForm({
  workspaces,
  workspaceId,
  onWorkspaceChange,
  requirement,
  onRequirementChange,
  reviewer,
  onReviewerChange,
  onSubmit,
  isBusy,
  status,
}: TaskFormProps) {
  return (
    <form
      id="task-form"
      className="review-form"
      noValidate
      onSubmit={(event) => {
        event.preventDefault();
        onSubmit();
      }}
    >
      <label htmlFor="workspace-id">Workspace</label>
      <select
        id="workspace-id"
        name="workspace-id"
        value={workspaceId}
        disabled={isBusy || workspaces.length === 0}
        onChange={(event) => onWorkspaceChange(event.target.value)}
      >
        <option value="">{workspaces.length === 0 ? "没有已登记的 workspace" : "选择已登记 workspace"}</option>
        {workspaces.map((item) => (
          <option key={item.workspace_id} value={item.workspace_id}>
            {item.display_name} ({item.workspace_id})
          </option>
        ))}
      </select>

      <label htmlFor="requirement">任务需求</label>
      <textarea
        id="requirement"
        name="requirement"
        rows={4}
        placeholder="描述要修改或实现的内容"
        value={requirement}
        disabled={isBusy}
        aria-invalid={status.tone === "error"}
        onChange={(event) => onRequirementChange(event.target.value)}
      />

      <fieldset className="reviewer-toggle" disabled={isBusy}>
        <legend>Reviewer</legend>
        <label>
          <input
            type="radio"
            name="reviewer"
            value="default"
            checked={reviewer === "default"}
            onChange={() => onReviewerChange("default")}
          />
          默认启用
        </label>
        <label>
          <input
            type="radio"
            name="reviewer"
            value="off"
            checked={reviewer === "off"}
            onChange={() => onReviewerChange("off")}
          />
          关闭
        </label>
      </fieldset>

      <div className="input-row">
        <button id="submit-button" type="submit" disabled={isBusy || !workspaceId || requirement.trim().length === 0}>
          {isBusy ? <RefreshCw aria-hidden="true" size={18} className="spin-icon" /> : <Play aria-hidden="true" size={18} />}
          <span>{isBusy ? "处理中" : "创建任务"}</span>
        </button>
      </div>
      <InlineStatus status={status} />
    </form>
  );
}
