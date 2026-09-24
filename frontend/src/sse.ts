import type { RunEvent } from "./types";

export function eventDedupeKey(runId: string, seq: number | undefined): string | null {
  if (seq === undefined || seq === null || Number.isNaN(seq)) return null;
  return `${runId}:${seq}`;
}

export function parseSseChunk(buffer: string): { events: RunEvent[]; rest: string } {
  const events: RunEvent[] = [];
  let rest = buffer.replace(/\r\n/g, "\n");
  let split = rest.indexOf("\n\n");
  while (split >= 0) {
    const block = rest.slice(0, split);
    rest = rest.slice(split + 2);
    const parsed = parseSseBlock(block);
    if (parsed) events.push(parsed);
    split = rest.indexOf("\n\n");
  }
  return { events, rest };
}

export function parseSseBlock(block: string): RunEvent | null {
  if (!block.trim() || block.trimStart().startsWith(":")) {
    return null;
  }
  let eventName = "";
  let eventId = "";
  const dataLines: string[] = [];
  for (const line of block.split("\n")) {
    if (!line || line.startsWith(":")) continue;
    const idx = line.indexOf(":");
    const field = idx >= 0 ? line.slice(0, idx) : line;
    let value = idx >= 0 ? line.slice(idx + 1) : "";
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "event") eventName = value;
    else if (field === "id") eventId = value;
    else if (field === "data") dataLines.push(value);
  }
  if (dataLines.length === 0) return null;
  let payload: Record<string, unknown> = {};
  try {
    const parsed = JSON.parse(dataLines.join("\n"));
    payload = typeof parsed === "object" && parsed !== null ? parsed : { data: parsed };
  } catch {
    payload = { data: dataLines.join("\n") };
  }
  const seqRaw = payload.seq ?? eventId;
  const seq = typeof seqRaw === "number" ? seqRaw : Number.parseInt(String(seqRaw || ""), 10);
  return {
    type: String(payload.type || eventName || "message"),
    message: typeof payload.message === "string" ? payload.message : undefined,
    payload: (payload.payload as Record<string, unknown> | undefined) ?? payload,
    run_id: typeof payload.run_id === "string" ? payload.run_id : undefined,
    seq: Number.isFinite(seq) ? seq : undefined,
  };
}
