"""Deterministic in-session conversation compaction.

Adapted from the AgenticCRE pattern (``compaction.py``), itself modelled
on claw-code's ``compact.rs``. Pure aggregation, no LLM-driven
summarisation. Builds a structured ``<summary>`` blob from older turns;
recent ``K`` turns kept verbatim. tool_use ↔ tool_result pair integrity
preserved via boundary walk-back so Anthropic's API never sees an
orphaned ``tool_result``.

Simplifications vs the reference:

* No CRE-specific signal extraction (URLs, captcha sitekeys, parcel
  PINs, ``[KEY DECISION]`` markers) — none apply to our domain.
* Summary blob is shorter: role counts, tools used, recent user
  requests, last assistant statement, condensed timeline.
* Default trigger threshold lowered to 20k (env-overridable) because
  our conversations are shorter and we want compaction to actually
  exercise during multi-tool runs.

Public surface:

* :class:`CompactionConfig` — triggers + caps
* :class:`CompactedMessages` — result shape
* :func:`should_compact` — decide whether to run on this turn
* :func:`compact_messages` — perform the compaction
* :func:`estimate_tokens` — ``len(text) // 4 + 1`` heuristic
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass
from typing import Any, Dict, List, Set

logger = logging.getLogger(__name__)


Message = Dict[str, Any]


COMPACT_CONTINUATION_PREAMBLE = (
    "This session is being continued from a previous conversation that ran "
    "out of context. The summary below covers the earlier portion."
)
COMPACT_RECENT_MESSAGES_NOTE = "Recent messages are preserved verbatim."
COMPACT_DIRECT_RESUME_INSTRUCTION = (
    "Continue from where the conversation left off without asking further "
    "questions. Resume directly — do not acknowledge the summary, do not "
    "recap, do not preface with continuation text."
)


@dataclass
class CompactionConfig:
    """Settings governing in-session compaction."""

    preserve_recent_messages: int = 4
    """Tail message count kept verbatim. Walk-back may include 1-2 more
    to keep tool_use/tool_result pairs intact."""

    max_estimated_tokens: int = 4_000
    """Compactable-portion floor. Don't pay the summary cost on a tiny
    history."""

    auto_trigger_input_tokens: int = 20_000
    """Cumulative session input-token threshold."""

    per_tool_result_char_cap: int = 8192
    """Truncation cap for individual tool-result bodies in the summary."""


@dataclass
class CompactedMessages:
    """Result of :func:`compact_messages`."""

    messages: List[Message]
    removed_count: int
    summary_text: str
    estimated_tokens_saved: int
    walk_back_steps: int = 0
    integrity_backstop_fired: bool = False


def estimate_tokens(content: Any) -> int:
    """``len(rendered) // 4 + 1`` heuristic. Matches claw-code's estimator."""
    if isinstance(content, str):
        return len(content) // 4 + 1
    if isinstance(content, list):
        total = 0
        for block in content:
            if not isinstance(block, dict):
                continue
            for value in block.values():
                if isinstance(value, str):
                    total += len(value)
                elif isinstance(value, dict):
                    total += len(str(value))
        return total // 4 + 1
    return 0


def _message_text(msg: Message) -> str:
    """Render a message to flat string for token / signal extraction."""
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: List[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            parts.append(block.get("text") or "")
        elif btype == "thinking":
            parts.append(block.get("thinking") or "")
        elif btype == "tool_use":
            name = block.get("name") or "?"
            parts.append(f"[tool_use:{name}] {block.get('input') or {}}")
        elif btype == "tool_result":
            raw = block.get("content")
            if isinstance(raw, str):
                parts.append(raw)
            elif isinstance(raw, list):
                for sub in raw:
                    if isinstance(sub, dict) and "text" in sub:
                        parts.append(sub.get("text") or "")
    return "\n".join(parts)


def should_compact(
    messages: List[Message],
    cumulative_input_tokens: int,
    config: CompactionConfig,
) -> bool:
    """Decide whether compaction should run before the next LLM call."""
    if cumulative_input_tokens < config.auto_trigger_input_tokens:
        return False
    if len(messages) <= config.preserve_recent_messages:
        return False
    cut = len(messages) - config.preserve_recent_messages
    compactable = sum(estimate_tokens(_message_text(m)) for m in messages[:cut])
    return compactable >= config.max_estimated_tokens


# ── tool_use ↔ tool_result pair safety ─────────────────────────────────


def _collect_tool_use_ids(msg: Message) -> Set[str]:
    """Return the set of tool_use ids in ``msg`` (both LiteLLM + Anthropic shapes)."""
    ids: Set[str] = set()
    tool_calls = msg.get("tool_calls")
    if isinstance(tool_calls, list):
        for tc in tool_calls:
            if isinstance(tc, dict):
                tid = tc.get("id")
                if isinstance(tid, str):
                    ids.add(tid)
    content = msg.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                tid = block.get("id")
                if isinstance(tid, str):
                    ids.add(tid)
    return ids


def _collect_tool_result_ids(msg: Message) -> Set[str]:
    """Return the set of tool_use_ids the message's tool results reference."""
    ids: Set[str] = set()
    if msg.get("role") == "tool":
        tid = msg.get("tool_call_id")
        if isinstance(tid, str):
            ids.add(tid)
    content = msg.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                tid = block.get("tool_use_id")
                if isinstance(tid, str):
                    ids.add(tid)
    return ids


def _walk_back_for_pair_safety(messages: List[Message], cut: int) -> int:
    """Move the cut backward until every preserved tool_result is paired."""
    if cut <= 0 or cut >= len(messages):
        return cut
    while cut > 0:
        preserved_results: Set[str] = set()
        preserved_uses: Set[str] = set()
        for msg in messages[cut:]:
            preserved_results |= _collect_tool_result_ids(msg)
            preserved_uses |= _collect_tool_use_ids(msg)
        if not preserved_results or preserved_results <= preserved_uses:
            return cut
        cut -= 1
    return cut


def _has_orphan_tool_results(messages: List[Message]) -> bool:
    """Defensive: True iff any preserved tool_result lacks its tool_use."""
    all_results: Set[str] = set()
    all_uses: Set[str] = set()
    for msg in messages:
        all_results |= _collect_tool_result_ids(msg)
        all_uses |= _collect_tool_use_ids(msg)
    return not (all_results <= all_uses)


def _has_thinking(msg: Message) -> bool:
    """True iff the message carries reasoning in either the LiteLLM
    (``thinking_blocks`` field) or Anthropic-native (``thinking`` content
    block) shape."""
    if msg.get("thinking_blocks"):
        return True
    content = msg.get("content")
    if isinstance(content, list):
        return any(isinstance(b, dict) and b.get("type") == "thinking" for b in content)
    return False


def _tool_use_missing_thinking(messages: List[Message]) -> bool:
    """Defensive: True iff a preserved assistant message makes tool calls but
    carries no thinking block.

    With extended thinking, Anthropic requires the thinking block from a
    tool-calling turn to accompany it whenever that turn's tool_result is in
    the conversation. The preserved tail is kept verbatim — ``thinking_blocks``
    is a field on the assistant message, so it rides along with its tool_calls
    automatically — so this should never fire. It is the backstop if a future
    change starts rebuilding or stripping preserved messages.
    """
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        if _collect_tool_use_ids(msg) and not _has_thinking(msg):
            return True
    return False


# ── Summary blob ───────────────────────────────────────────────────────


def _role_counts(messages: List[Message]) -> Dict[str, int]:
    counts: Counter[str] = Counter()
    for m in messages:
        counts[m.get("role") or "?"] += 1
    return dict(counts)


def _collect_tool_names(messages: List[Message]) -> List[str]:
    """Distinct tool names invoked in the compacted portion, sorted."""
    names: Set[str] = set()
    for msg in messages:
        # LiteLLM shape.
        tool_calls = msg.get("tool_calls")
        if isinstance(tool_calls, list):
            for tc in tool_calls:
                fn = tc.get("function") if isinstance(tc, dict) else None
                if isinstance(fn, dict):
                    name = fn.get("name")
                    if isinstance(name, str):
                        names.add(name)
        # Anthropic-native shape.
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    name = block.get("name")
                    if isinstance(name, str):
                        names.add(name)
    return sorted(names)


def _last_user_requests(messages: List[Message], n: int, cap: int) -> List[str]:
    """Last ``n`` non-empty user-role messages, truncated to ``cap``."""
    out: List[str] = []
    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, list):
            non_tool = [
                b for b in content if isinstance(b, dict) and b.get("type") != "tool_result"
            ]
            if not non_tool:
                continue
        text = _message_text(msg).strip()
        if not text:
            continue
        if len(text) > cap:
            text = text[:cap] + "…"
        out.append(text)
        if len(out) >= n:
            break
    out.reverse()
    return out


def _build_timeline(messages: List[Message], line_cap: int = 100) -> List[str]:
    out: List[str] = []
    for i, msg in enumerate(messages):
        role = msg.get("role") or "?"
        text = _message_text(msg).replace("\n", " ").strip() or "(empty)"
        if len(text) > line_cap:
            text = text[:line_cap] + "…"
        out.append(f"[{i}] {role}: {text}")
    return out


def _format_section(label: str, items: List[str]) -> str:
    if not items:
        return f"- {label}: (none)"
    bullets = "\n".join(f"  - {item}" for item in items)
    return f"- {label}:\n{bullets}"


def _build_summary(compacted: List[Message]) -> str:
    role_counts = _role_counts(compacted)
    tool_names = _collect_tool_names(compacted)
    last_user = _last_user_requests(compacted, n=2, cap=200)
    timeline = _build_timeline(compacted)

    current = ""
    for msg in reversed(compacted):
        text = _message_text(msg).strip()
        if text:
            current = text[-200:]
            break

    scope_line = (
        f"- Scope: {len(compacted)} earlier messages compacted ("
        + ", ".join(f"{r}={n}" for r, n in sorted(role_counts.items()))
        + ")"
    )

    return "\n".join(
        [
            "<summary>",
            "Conversation summary:",
            scope_line,
            f"- Tools called: {', '.join(tool_names) if tool_names else '(none)'}",
            _format_section("Recent user requests", last_user),
            (
                f"- Most recent assistant work: {current}"
                if current
                else "- Most recent assistant work: (none)"
            ),
            _format_section("Timeline", timeline),
            "</summary>",
        ]
    )


def compact_messages(
    messages: List[Message],
    config: CompactionConfig,
) -> CompactedMessages:
    """Compact an over-long conversation to ``summary + preserved_tail``."""
    if len(messages) <= config.preserve_recent_messages:
        return CompactedMessages(
            messages=list(messages),
            removed_count=0,
            summary_text="",
            estimated_tokens_saved=0,
        )

    raw_cut = len(messages) - config.preserve_recent_messages
    cut = _walk_back_for_pair_safety(messages, raw_cut)
    walk_back_steps = max(0, raw_cut - cut)
    compacted = messages[:cut]
    # The tail is preserved verbatim — whole message dicts, not rebuilt. That
    # is what keeps tool_use/tool_result pairs *and* extended-thinking blocks
    # (a field on the assistant message) intact across a compaction.
    preserved = messages[cut:]

    if not compacted:
        return CompactedMessages(
            messages=list(messages),
            removed_count=0,
            summary_text="",
            estimated_tokens_saved=0,
            walk_back_steps=walk_back_steps,
        )

    summary = _build_summary(compacted)
    preamble = "\n\n".join(
        [
            COMPACT_CONTINUATION_PREAMBLE,
            COMPACT_RECENT_MESSAGES_NOTE,
            COMPACT_DIRECT_RESUME_INSTRUCTION,
        ]
    )
    summary_msg: Message = {
        "role": "user",
        "content": [{"type": "text", "text": f"{preamble}\n\n{summary}"}],
    }
    new_messages: List[Message] = [summary_msg] + list(preserved)

    # Post-compaction integrity backstop. The walk-back + verbatim tail
    # preservation should make both of these unreachable; either firing means
    # a real regression. Anthropic rejects the conversation if a tool_result
    # is orphaned, or — under extended thinking — if a tool-calling turn is
    # missing its thinking block.
    backstop_reason = None
    if _has_orphan_tool_results(new_messages):
        backstop_reason = "walk-back left an orphan tool_result in the preserved tail"
    elif _tool_use_missing_thinking(new_messages):
        backstop_reason = "a preserved tool-calling turn lost its thinking block"
    if backstop_reason:
        logger.warning(
            "Compaction integrity backstop fired: %s. Returning original "
            "messages uncompacted to avoid API rejection.",
            backstop_reason,
        )
        return CompactedMessages(
            messages=list(messages),
            removed_count=0,
            summary_text="",
            estimated_tokens_saved=0,
            walk_back_steps=walk_back_steps,
            integrity_backstop_fired=True,
        )

    pre = sum(estimate_tokens(_message_text(m)) for m in messages)
    post = sum(estimate_tokens(_message_text(m)) for m in new_messages)
    saved = max(0, pre - post)

    logger.info(
        "compacted %d messages → 1 summary; ~%d tokens saved (pre=%d, post=%d, walk_back=%d)",
        len(compacted),
        saved,
        pre,
        post,
        walk_back_steps,
    )

    return CompactedMessages(
        messages=new_messages,
        removed_count=len(compacted),
        summary_text=summary,
        estimated_tokens_saved=saved,
        walk_back_steps=walk_back_steps,
    )
