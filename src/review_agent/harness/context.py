from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from review_agent.config import ModelProfile
from review_agent.harness.memory.models import LoadedMemories
from review_agent.harness.models import Message


@dataclass
class FragmentCache:
    """Path -> (content_hash, snippet); invalidated when file content changes."""

    _entries: dict[str, tuple[str, str]] = field(default_factory=dict)

    def get(self, path: Path, *, max_chars: int = 2000) -> str | None:
        if not path.is_file():
            self._entries.pop(str(path), None)
            return None
        content = path.read_text(encoding="utf-8", errors="replace")
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        key = str(path.resolve())
        cached = self._entries.get(key)
        if cached is not None and cached[0] == digest:
            return cached[1]
        snippet = content if len(content) <= max_chars else content[: max_chars - 20] + "\n...[truncated]..."
        self._entries[key] = (digest, snippet)
        return snippet

    def invalidate(self, path: Path | str) -> None:
        self._entries.pop(str(Path(path).resolve()), None)

    def clear(self) -> None:
        self._entries.clear()


@dataclass(frozen=True)
class AssembledContext:
    messages: list[Message]
    system_prompt: str
    estimated_tokens: int
    token_estimate_is_approximate: bool
    source_message_ids: list[str]
    truncated_message_ids: list[str]


class ContextAssembler:
    """Build model-facing context under profile budget without splitting tool pairs."""

    def __init__(
        self,
        workspace_root: str | Path,
        *,
        fragment_cache: FragmentCache | None = None,
        reserve_output_tokens: int = 4096,
        default_window_tokens: int = 32_000,
        optimize: bool = True,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.fragment_cache = fragment_cache or FragmentCache()
        self.reserve_output_tokens = reserve_output_tokens
        self.default_window_tokens = default_window_tokens
        self.optimize = optimize

    def assemble(
        self,
        *,
        history: list[Message],
        requirement: str,
        loaded_memory: LoadedMemories,
        tool_specs: list[dict],
        profile: ModelProfile | None = None,
        skill_hints: list[str] | None = None,
        failure_evidence: list[str] | None = None,
        changed_files: list[str] | None = None,
    ) -> AssembledContext:
        window = (
            profile.context_window_tokens
            if profile and profile.context_window_tokens
            else self.default_window_tokens
        )
        approximate = profile is None or profile.context_window_tokens is None
        budget = max(1024, window - self.reserve_output_tokens)

        snippets: list[str] = []
        if self.optimize:
            for rel in changed_files or []:
                path = self.workspace_root / rel
                snippet = self.fragment_cache.get(path)
                if snippet is not None:
                    snippets.append(f"### {rel}\n{snippet}")

        system = _system_prompt(
            tool_specs=tool_specs,
            memory=loaded_memory,
            requirement=requirement,
            skill_hints=skill_hints or [],
            failure_evidence=failure_evidence or [],
            snippets=snippets,
        )
        system_msg = Message(role="system", content=system, message_id="system")
        projected, source_ids, truncated_ids = _project_history(
            history,
            budget - _estimate_tokens(system),
            summarize=self.optimize,
        )
        messages = [system_msg, *projected]
        estimated = sum(_estimate_tokens(message.content) for message in messages)
        for message in messages:
            if message.tool_calls:
                estimated += _estimate_tokens(json.dumps([tc.model_dump() for tc in message.tool_calls]))
        return AssembledContext(
            messages=messages,
            system_prompt=system,
            estimated_tokens=estimated,
            token_estimate_is_approximate=approximate,
            source_message_ids=source_ids,
            truncated_message_ids=truncated_ids,
        )


def mark_truncated_observation(text: str, *, limit: int = 4000) -> str:
    if len(text) <= limit:
        return text
    if "artifacts=" in text or "see artifact" in text:
        return text[: limit - 40] + "\n...[truncated; see artifact_refs]...\n"
    return text[: limit - 40] + "\n...[truncated; artifact_pending]...\n"


def _project_history(
    history: list[Message],
    budget_tokens: int,
    *,
    summarize: bool = True,
) -> tuple[list[Message], list[str], list[str]]:
    """Keep newest messages; never split assistant tool_calls from their tool results."""
    if budget_tokens <= 0:
        return [], [], [m.message_id for m in history if m.message_id]

    pairs = _group_messages(history)
    selected_rev: list[list[Message]] = []
    used = 0
    truncated: list[str] = []
    for group in reversed(pairs):
        cost = sum(_message_cost(message) for message in group)
        if selected_rev and used + cost > budget_tokens:
            for message in group:
                if message.message_id:
                    truncated.append(message.message_id)
            continue
        selected_rev.append(group)
        used += cost
    selected = [message for group in reversed(selected_rev) for message in group]
    # If still over budget, summarize oldest kept user/assistant text while preserving tool pairs.
    if summarize and used > budget_tokens and selected:
        selected, more_truncated = _summarize_oldest(selected, budget_tokens)
        truncated.extend(more_truncated)
    source_ids = [message.message_id for message in selected if message.message_id]
    return selected, source_ids, truncated


def _group_messages(history: list[Message]) -> list[list[Message]]:
    groups: list[list[Message]] = []
    index = 0
    while index < len(history):
        message = history[index]
        if message.role == "assistant" and message.tool_calls:
            group = [message]
            needed = {
                (tc.provider_call_id or tc.call_id)
                for tc in message.tool_calls
                if (tc.provider_call_id or tc.call_id)
            }
            index += 1
            while index < len(history) and needed:
                nxt = history[index]
                if nxt.role == "tool" and nxt.provider_call_id in needed:
                    group.append(nxt)
                    needed.discard(nxt.provider_call_id)
                    index += 1
                    continue
                break
            groups.append(group)
            continue
        groups.append([message])
        index += 1
    return groups


def _summarize_oldest(
    messages: list[Message],
    budget_tokens: int,
) -> tuple[list[Message], list[str]]:
    truncated_ids: list[str] = []
    working = list(messages)
    while working and sum(_message_cost(m) for m in working) > budget_tokens:
        group = _group_messages(working)[0]
        if any(message.role == "tool" or (message.role == "assistant" and message.tool_calls) for message in group):
            # Keep tool pairs intact; drop whole oldest non-tool group if possible.
            non_tool_groups = [
                g
                for g in _group_messages(working)
                if not any(m.role == "tool" or (m.role == "assistant" and m.tool_calls) for m in g)
            ]
            if non_tool_groups:
                drop = non_tool_groups[0]
                source_ids = [m.message_id for m in drop if m.message_id]
                summary = Message(
                    role="user",
                    content=(
                        "[history_summary] "
                        + " | ".join((m.content or "")[:120] for m in drop)
                        + f" (source_message_ids={','.join(source_ids)})"
                    ),
                    message_id=f"summary-{source_ids[0] if source_ids else 'x'}",
                )
                truncated_ids.extend(source_ids)
                # Replace dropped messages with summary.
                drop_ids = {id(m) for m in drop}
                working = [summary] + [m for m in working if id(m) not in drop_ids]
                continue
        # Fallback: drop oldest group entirely but record ids.
        truncated_ids.extend(m.message_id for m in group if m.message_id)
        drop_ids = {id(m) for m in group}
        working = [m for m in working if id(m) not in drop_ids]
    return working, truncated_ids


def _message_cost(message: Message) -> int:
    cost = _estimate_tokens(message.content or "")
    if message.tool_calls:
        cost += _estimate_tokens(json.dumps([tc.model_dump() for tc in message.tool_calls], default=str))
    return cost


def _estimate_tokens(text: str) -> int:
    # Rough heuristic when provider usage is unavailable.
    return max(1, (len(text) + 3) // 4) if text else 0


def _system_prompt(
    *,
    tool_specs: list[dict],
    memory: LoadedMemories,
    requirement: str,
    skill_hints: list[str],
    failure_evidence: list[str],
    snippets: list[str],
) -> str:
    memory_lines: list[str] = []
    for entry in memory.entries:
        status = getattr(entry, "verification_status", "unverified")
        confirmed = getattr(entry, "user_confirmed", False)
        if status == "unverified" and not confirmed:
            memory_lines.append(
                f"[unverified memory/{entry.type.value}] {entry.name}: {entry.content}"
            )
        else:
            memory_lines.append(f"[memory/{entry.type.value}] {entry.name}: {entry.content}")
    skills = "\n".join(skill_hints) if skill_hints else "(none)"
    failures = "\n".join(failure_evidence) if failure_evidence else "(none)"
    code = "\n\n".join(snippets) if snippets else "(none)"
    return (
        "You are review-agent, a local coding harness agent.\n"
        "Use native tools when available. Prefer small verified steps.\n"
        "Unverified memories are hints, not project rules.\n"
        f"Current requirement:\n{requirement}\n\n"
        f"Available tools:\n{json.dumps(tool_specs, ensure_ascii=False, indent=2)}\n\n"
        f"Memory index:\n{memory.index_markdown or '(empty)'}\n\n"
        f"Relevant memories:\n{chr(10).join(memory_lines) or '(none)'}\n\n"
        f"Skill hints:\n{skills}\n\n"
        f"Failure / diff evidence:\n{failures}\n\n"
        f"Source snippets:\n{code}"
    )
