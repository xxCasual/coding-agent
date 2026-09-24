export type Submission = {
  content: string;
  optionsKey: string;
  sessionId: string | null;
  runId: string | null;
  key: string;
  messageId: string;
};

export const errorText = (error: unknown) => (error instanceof Error ? error.message : "请求失败，请重试。");
