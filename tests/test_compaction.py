"""Unit tests for service/compaction.py.

Compaction is the one module that is pure, deterministic logic with subtle
invariants — the tool_use/tool_result pair-safety walk-back and the integrity
backstops — so it is where unit tests genuinely earn their keep. No database
or LLM is involved; these tests are fast and hermetic.
"""

from __future__ import annotations

from service.compaction import (
    CompactionConfig,
    _collect_tool_result_ids,
    _collect_tool_use_ids,
    _has_orphan_tool_results,
    _has_thinking,
    _tool_use_missing_thinking,
    _walk_back_for_pair_safety,
    compact_messages,
    estimate_tokens,
    should_compact,
)

# ── message fixtures ───────────────────────────────────────────────────


def _user(text: str) -> dict:
    return {"role": "user", "content": text}


def _assistant(text: str = "", *, tool_calls=None, thinking=None) -> dict:
    """An assistant message in the LiteLLM shape the agent loop produces."""
    msg: dict = {"role": "assistant", "content": text}
    if tool_calls is not None:
        msg["tool_calls"] = [
            {"id": tid, "type": "function", "function": {"name": name, "arguments": "{}"}}
            for tid, name in tool_calls
        ]
    if thinking is not None:
        msg["thinking_blocks"] = [{"type": "thinking", "thinking": thinking, "signature": "sig"}]
    return msg


def _tool(call_id: str, text: str = "result") -> dict:
    return {"role": "tool", "tool_call_id": call_id, "name": "t", "content": text}


# ── estimate_tokens ────────────────────────────────────────────────────


def test_estimate_tokens_string():
    assert estimate_tokens("a" * 40) == 11  # 40 // 4 + 1


def test_estimate_tokens_non_text_is_zero():
    assert estimate_tokens(123) == 0
    assert estimate_tokens(None) == 0


def test_estimate_tokens_block_list():
    # sums every string value in each block: "text" (4) + "hello" (5) = 9
    assert estimate_tokens([{"type": "text", "text": "hello"}]) == 9 // 4 + 1


# ── should_compact ─────────────────────────────────────────────────────


def test_should_compact_false_below_token_trigger():
    cfg = CompactionConfig(auto_trigger_input_tokens=1000)
    assert should_compact([_user("hi")] * 10, 100, cfg) is False


def test_should_compact_false_too_few_messages():
    cfg = CompactionConfig(auto_trigger_input_tokens=10, preserve_recent_messages=4)
    assert should_compact([_user("hi")] * 3, 999, cfg) is False


def test_should_compact_true_when_over_thresholds():
    cfg = CompactionConfig(
        auto_trigger_input_tokens=10, preserve_recent_messages=2, max_estimated_tokens=5
    )
    assert should_compact([_user("x" * 100) for _ in range(6)], 999, cfg) is True


# ── tool_use / tool_result id collection ───────────────────────────────


def test_collect_tool_use_ids_litellm_shape():
    msg = _assistant(tool_calls=[("c1", "load_prices"), ("c2", "headline_search")])
    assert _collect_tool_use_ids(msg) == {"c1", "c2"}


def test_collect_tool_use_ids_anthropic_shape():
    msg = {"role": "assistant", "content": [{"type": "tool_use", "id": "c9", "name": "t"}]}
    assert _collect_tool_use_ids(msg) == {"c9"}


def test_collect_tool_result_ids_tool_role():
    assert _collect_tool_result_ids(_tool("c1")) == {"c1"}


def test_collect_tool_result_ids_anthropic_shape():
    msg = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c5"}]}
    assert _collect_tool_result_ids(msg) == {"c5"}


# ── pair-safety walk-back ──────────────────────────────────────────────


def test_walk_back_includes_tool_use_for_preserved_result():
    # [sys, task, assistant(c1), tool(c1), assistant(final)] — a naive cut=3
    # would preserve [tool(c1), assistant(final)], orphaning tool(c1). The
    # walk-back must move the cut back to 2 so c1's tool_use is also kept.
    msgs = [
        _user("sys"),
        _user("task"),
        _assistant(tool_calls=[("c1", "load_prices")], thinking="t"),
        _tool("c1"),
        _assistant("final"),
    ]
    assert _walk_back_for_pair_safety(msgs, 3) == 2


def test_walk_back_no_op_when_already_safe():
    msgs = [_user("a"), _user("b"), _assistant("c")]
    assert _walk_back_for_pair_safety(msgs, 2) == 2


# ── orphan detection ───────────────────────────────────────────────────


def test_has_orphan_tool_results_true():
    assert _has_orphan_tool_results([_tool("c1"), _assistant("x")]) is True


def test_has_orphan_tool_results_false_when_paired():
    msgs = [_assistant(tool_calls=[("c1", "t")], thinking="t"), _tool("c1")]
    assert _has_orphan_tool_results(msgs) is False


# ── thinking-block invariant ───────────────────────────────────────────


def test_has_thinking_litellm_blocks():
    assert _has_thinking(_assistant(thinking="reasoning")) is True


def test_has_thinking_anthropic_block():
    msg = {"role": "assistant", "content": [{"type": "thinking", "thinking": "r"}]}
    assert _has_thinking(msg) is True


def test_has_thinking_false_when_absent():
    assert _has_thinking(_assistant("just text")) is False


def test_tool_use_missing_thinking_flags_toolcall_without_thinking():
    assert _tool_use_missing_thinking([_assistant(tool_calls=[("c1", "t")])]) is True


def test_tool_use_missing_thinking_ok_with_thinking():
    msgs = [_assistant(tool_calls=[("c1", "t")], thinking="r")]
    assert _tool_use_missing_thinking(msgs) is False


def test_tool_use_missing_thinking_ignores_plain_assistant():
    assert _tool_use_missing_thinking([_assistant("plain answer")]) is False


# ── compact_messages end-to-end ────────────────────────────────────────


def test_compact_messages_noop_when_short():
    cfg = CompactionConfig(preserve_recent_messages=4)
    msgs = [_user("a"), _user("b")]
    result = compact_messages(msgs, cfg)
    assert result.removed_count == 0
    assert result.messages == msgs


def test_compact_messages_produces_summary_plus_verbatim_tail():
    cfg = CompactionConfig(preserve_recent_messages=2)
    msgs = [_user(f"m{i}") for i in range(8)]
    result = compact_messages(msgs, cfg)
    assert result.removed_count > 0
    assert "<summary>" in result.summary_text
    # a synthetic summary message, then the recent tail kept verbatim
    assert result.messages[0]["role"] == "user"
    assert result.messages[-2:] == msgs[-2:]


def test_compact_messages_keeps_tool_pairs_and_thinking_intact():
    cfg = CompactionConfig(preserve_recent_messages=2)
    msgs = [
        _user("sys"),
        _user("task"),
        _assistant("a1"),
        _assistant(tool_calls=[("c1", "load_prices")], thinking="reasoning"),
        _tool("c1"),
        _assistant("final answer"),
    ]
    result = compact_messages(msgs, cfg)
    # the cut would orphan tool(c1); the walk-back keeps its assistant turn,
    # which carries both the tool_calls and the thinking block
    assert _has_orphan_tool_results(result.messages) is False
    assert _tool_use_missing_thinking(result.messages) is False
    assert result.integrity_backstop_fired is False
