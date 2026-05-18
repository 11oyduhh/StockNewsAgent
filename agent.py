#!/usr/bin/env python3
"""Host-side CLI client for the anthelion agent service.

The 'agent script that exercises the full stack end-to-end' deliverable.
Stdlib only — no install step. POSTs to the FastAPI service running in
the agent container.

Usage:
    python agent.py "what were the most-traded S&P 500 tickers in 2015?"
    python agent.py "Apple headlines in Jun 2014" --json
    python agent.py "how volatile was MSFT in 2015?" --trace   # live telemetry
    python agent.py --trace-only 762eecd0-b934-...     # inspect a past run

With --trace the run streams: each LLM call and tool call prints the moment
it completes. Ctrl-C closes the connection and the server aborts the run.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def _request(url: str, timeout: int, payload: dict | None = None) -> dict:
    """GET (payload is None) or POST a JSON request; return the decoded body."""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"} if data is not None else {},
        method="POST" if data is not None else "GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _human(n: int | None) -> str:
    """Token counts compactly: 12100 -> '12.1k'."""
    if n is None:
        return "—"
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def _lat(ms: int | None) -> str:
    return f"{ms}ms" if ms is not None else "—"


def _print_step(step: dict) -> None:
    """Print one telemetry step. Shared by the live stream (events keyed by
    `type`) and the --trace-only timeline (rows keyed by `kind`)."""
    turn = step.get("turn")
    kind = step.get("kind") or step.get("type")
    if kind == "llm_call":
        tok = f"{_human(step.get('input_tokens'))}/{_human(step.get('output_tokens'))}"
        cost = step.get("cost_usd")
        cost_str = f"${cost:.4f}" if cost else "—"
        print(
            f"  turn {turn}  llm_call   {step.get('model', '?'):26}"
            f" {tok:>12}  {cost_str:>9}  {_lat(step.get('latency_ms')):>8}"
        )
        reasoning = step.get("reasoning")
        if reasoning:
            print("             thinking ─")
            for line in reasoning.strip().splitlines():
                print(f"             │ {line}")
        tool_calls = step.get("tool_calls")
        if tool_calls:
            print(f"             → requests: {', '.join(tool_calls)}")
    elif kind == "tool_call":
        print(
            f"  turn {turn}  tool_call  {step.get('tool_name', '?'):26}"
            f" {'':>12}  {'':>9}  {_lat(step.get('latency_ms')):>8}"
        )
    if step.get("error"):
        print(f"             ! error: {step['error']}")


def _print_summary(body: dict) -> None:
    """Print the one-line run summary."""
    parts = [
        f"task_id={body.get('task_id')}",
        f"turns={body.get('turns')}",
        f"tokens={body.get('input_tokens')}/{body.get('output_tokens')}",
        f"cost=${body.get('cost_usd', 0):.4f}",
    ]
    if body.get("compactions"):
        parts.append(f"compactions={body['compactions']}")
    if body.get("hit_round_cap"):
        parts.append("hit_round_cap")
    print("[" + "  ".join(parts) + "]")


def print_trace(body: dict) -> None:
    """Print a past task's telemetry as an ordered timeline (--trace-only)."""
    print(f"\n── trace {body.get('task_id')} · {body.get('step_count')} steps ──")
    for step in body.get("steps", []):
        _print_step(step)


def _stream_run(url: str, payload: dict, timeout: int) -> int:
    """POST to /run/stream and print each telemetry step as it arrives."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    print("── live trace ──")
    result: dict | None = None
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        for raw in resp:
            line = raw.strip()
            if not line:
                continue
            event = json.loads(line)
            etype = event.get("type")
            if etype in ("llm_call", "tool_call"):
                _print_step(event)
            elif etype == "result":
                result = event
            elif etype == "error":
                print(f"\nagent run failed: {event.get('detail')}", file=sys.stderr)
                return 2
    if result is None:
        print("\n(stream ended without a result)", file=sys.stderr)
        return 2
    print()
    print((result.get("answer") or "").rstrip())
    print()
    _print_summary(result)
    return 0


def _fail(exc: Exception, url: str) -> int:
    if isinstance(exc, urllib.error.HTTPError):
        print(f"HTTP {exc.code}: {exc.read().decode('utf-8', errors='replace')}", file=sys.stderr)
    elif isinstance(exc, urllib.error.URLError):
        print(f"Could not reach {url}: {exc.reason}", file=sys.stderr)
    else:
        print(f"Error: {exc}", file=sys.stderr)
    return 2


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Send a task to the anthelion agent service.",
    )
    parser.add_argument("task", nargs="?", help="The task / question for the agent.")
    parser.add_argument(
        "--url",
        default=os.environ.get("ANTHELION_URL", "http://localhost:8000"),
        help="Base URL of the agent service (default: env ANTHELION_URL or http://localhost:8000).",
    )
    parser.add_argument(
        "--max-rounds-with-tool-calls",
        type=int,
        default=None,
        help="Override the agent's tool-use round budget for this run.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the raw JSON response instead of formatted output.",
    )
    parser.add_argument(
        "--trace",
        action="store_true",
        help="Stream the run: print each LLM/tool step (with reasoning) as it happens.",
    )
    parser.add_argument(
        "--trace-only",
        metavar="TASK_ID",
        help="Print the telemetry timeline for a past task and exit; no new run.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=600,
        help="Request timeout in seconds (default: 600).",
    )
    args = parser.parse_args()
    base = args.url.rstrip("/")

    # ── --trace-only: inspect a past run, no new task ──────────────────
    if args.trace_only:
        try:
            print_trace(_request(f"{base}/traces/{args.trace_only}", args.timeout))
        except (urllib.error.HTTPError, urllib.error.URLError) as exc:
            return _fail(exc, args.url)
        return 0

    if not args.task:
        parser.error("a task is required (or use --trace-only TASK_ID)")

    payload: dict = {"task": args.task}
    if args.max_rounds_with_tool_calls is not None:
        payload["max_rounds_with_tool_calls"] = args.max_rounds_with_tool_calls

    # ── --trace: stream the run, printing each step live ───────────────
    if args.trace and not args.json:
        try:
            return _stream_run(f"{base}/run/stream", payload, args.timeout)
        except KeyboardInterrupt:
            print(
                "\n(interrupted — connection closed; the server aborts the run)",
                file=sys.stderr,
            )
            return 130
        except (urllib.error.HTTPError, urllib.error.URLError) as exc:
            return _fail(exc, args.url)

    # ── Plain run ──────────────────────────────────────────────────────
    try:
        body = _request(f"{base}/run", args.timeout, payload)
    except (urllib.error.HTTPError, urllib.error.URLError) as exc:
        return _fail(exc, args.url)

    if args.json:
        print(json.dumps(body, indent=2))
        return 0

    print((body.get("answer") or "").rstrip())
    print()
    _print_summary(body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
