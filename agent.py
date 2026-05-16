#!/usr/bin/env python3
"""Host-side CLI client for the anthelion agent service.

The 'agent script that exercises the full stack end-to-end' deliverable.
Stdlib only — no install step. POSTs to the FastAPI service running in
the agent container.

Usage:
    python agent.py "what were the most-traded S&P 500 tickers in 2015?"
    python agent.py "Apple headlines in Jun 2014" --json
    python agent.py "forecast next-10 returns for MSFT" --max-rounds-with-tool-calls 6
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Send a task to the anthelion agent service.",
    )
    parser.add_argument("task", help="The task / question for the agent.")
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
        "--timeout",
        type=int,
        default=600,
        help="Request timeout in seconds (default: 600).",
    )
    args = parser.parse_args()

    payload: dict = {"task": args.task}
    if args.max_rounds_with_tool_calls is not None:
        payload["max_rounds_with_tool_calls"] = args.max_rounds_with_tool_calls

    req = urllib.request.Request(
        f"{args.url.rstrip('/')}/run",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=args.timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {e.read().decode('utf-8', errors='replace')}", file=sys.stderr)
        return 2
    except urllib.error.URLError as e:
        print(f"Could not reach {args.url}: {e.reason}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(body, indent=2))
        return 0

    answer = (body.get("answer") or "").rstrip()
    print(answer)
    print()
    summary_parts = [
        f"task_id={body.get('task_id')}",
        f"turns={body.get('turns')}",
        f"tokens={body.get('input_tokens')}/{body.get('output_tokens')}",
        f"cost=${body.get('cost_usd', 0):.4f}",
    ]
    if body.get("compactions"):
        summary_parts.append(f"compactions={body['compactions']}")
    if body.get("hit_round_cap"):
        summary_parts.append("hit_round_cap")
    print("[" + "  ".join(summary_parts) + "]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
