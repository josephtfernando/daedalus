"""Benchmark local models on the two jobs Daedalus actually gives one.

    uv run python scripts/bench_local_models.py <model> [<model> ...]

1. the routing question (triage): a short classification with a fixed answer set
2. a tool call: does it emit a well-formed `files(op=read, path=...)` call

Both are measured the way the runtime makes them: an OpenAI-compatible
`/v1/chat/completions` call against the local runner.

Why this is a script and not a test: it needs a runner, a pulled model and a
few minutes of GPU-less CPU time. It is run when choosing the local rung of the
chain, and its output is the evidence for that choice. On 2026-11-16 it chose
`hf.co/insraq/Qwen3.5-4B-…-MTP-GGUF:Q4_K_M` over Huihui's abliterated 4B:
same routing (3/5), 2/2 exact tool calls against 1/2, and ~25% faster per call
(6.0s vs 7.0s routing, 12.9s vs 16.3s tool), both at 8.9 tok/s on a Ryzen
5700U's CPU.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:11434"

SEATS = """- daedalus (Daedalus): coordinator, owns priorities, the week, and anything unclaimed
- athena (Athena): learning, drills, study plans, technical depth
- odysseus (Odysseus): the job search, applications, interviews, market intel
- dave (Dave): shipping code, builds, debugging, releases
- icarus (Icarus): ideas, experiments, writing, the speculative"""

ROUTING = [
    ("drill me on eigenvectors for twenty minutes", "athena"),
    ("did that recruiter from Linear reply about the backend role", "odysseus"),
    ("why does the daemon keep dropping the discord gateway", "dave"),
    ("what does my week look like", "daedalus"),
    ("what if memory were a git repo instead of markdown", "icarus"),
]

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "files",
            "description": "Read, write, list, glob or grep files inside the permitted workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "op": {"type": "string", "enum": ["read", "write", "list", "glob", "grep", "mkdir"]},
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                    "pattern": {"type": "string"},
                },
                "required": ["op", "path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calendar",
            "description": "Read or change the internal calendar.",
            "parameters": {
                "type": "object",
                "properties": {
                    "op": {"type": "string", "enum": ["list", "add", "update", "delete"]},
                    "summary": {"type": "string"},
                    "start": {"type": "string"},
                },
                "required": ["op"],
            },
        },
    },
]

TOOL_CASES = [
    ("read the file ~/.daedalus/brief.md", "files", {"op": "read", "path": "~/.daedalus/brief.md"}),
    ("what is on my calendar", "calendar", {"op": "list"}),
]


def post(path: str, payload: dict, timeout: float = 300) -> dict:
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def routing_pass(model: str) -> tuple[int, float, list[str]]:
    right, wrong, seconds = 0, [], 0.0
    for message, expected in ROUTING:
        prompt = (
            "One message arrived for the owner's agent, which has the seats below. "
            "Name the single seat it belongs to.\n\nSeats:\n"
            f"{SEATS}\n"
            "Answer with the seat's key, then a colon, then one short clause saying why. "
            "Nothing else — no preamble, no alternatives.\n"
            "If the message spans seats, is about priorities or the week, or you are unsure, "
            f"answer daedalus: the coordinator keeps what does not clearly belong elsewhere.\n\n"
            f"Message: {message}"
        )
        started = time.monotonic()
        try:
            out = post(
                "/api/chat",
                {
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False,
                    "think": False,
                    "options": {"temperature": 0.0, "num_predict": 60},
                },
            )
            seconds += time.monotonic() - started
            answer = (out.get("message", {}).get("content") or "").strip()
        except Exception as exc:  # noqa: BLE001
            wrong.append(f"{message!r}: ERROR {exc}")
            continue
        head = answer.split(":", 1)[0].strip().lower().strip("*` .")
        if head == expected:
            right += 1
        else:
            wrong.append(f"{message!r} → {answer[:60]!r} (wanted {expected})")
    return right, seconds, wrong


def tool_pass(model: str) -> tuple[int, float, list[str], list[str]]:
    right, seconds, wrong, shapes = 0, 0.0, [], []
    for message, expected_name, expected_args in TOOL_CASES:
        started = time.monotonic()
        try:
            out = post(
                "/v1/chat/completions",
                {
                    "model": model,
                    "messages": [
                        {
                            "role": "system",
                            "content": "You are Daedalus, a chief-of-staff agent. Use a tool when one fits.",
                        },
                        {"role": "user", "content": message},
                    ],
                    "tools": TOOLS,
                    "temperature": 0.0,
                    # Every candidate here declares `thinking` with
                    # `default = true`, and a thinking model spends tokens
                    # before it emits a call. At 200 this benchmark scored
                    # lfm2.5-thinking 0/2 and gemma4:e2b 0/2 — both of which
                    # call the right tool with the right arguments at 1024.
                    # A truncation looks exactly like a refusal to use a tool,
                    # so the budget has to fit the thinking and the result has
                    # to say which one happened.
                    "max_tokens": 1024,
                },
            )
            seconds += time.monotonic() - started
            choice = out["choices"][0]
            message_out = choice["message"]
            call = (message_out.get("tool_calls") or [None])[0]
            thought = len(message_out.get("reasoning") or "")
            if not call:
                wrong.append(
                    f"{message!r}: no tool call (finish={choice.get('finish_reason')}, "
                    f"reasoning={thought}c) — {(message_out.get('content') or '')[:60]!r}"
                )
                continue
            fn = call["function"]
            args = json.loads(fn.get("arguments") or "{}")
            shapes.append(f"{fn['name']}({', '.join(f'{k}={v!r}' for k, v in args.items())})")
            if fn["name"] != expected_name:
                wrong.append(f"{message!r}: called {fn['name']}, wanted {expected_name}")
            elif any(args.get(k) != v for k, v in expected_args.items()):
                wrong.append(f"{message!r}: args {args} do not carry {expected_args}")
            else:
                right += 1
        except Exception as exc:  # noqa: BLE001
            wrong.append(f"{message!r}: ERROR {exc}")
    return right, seconds, wrong, shapes


def tokens_per_second(model: str) -> float | None:
    try:
        out = post(
            "/api/generate",
            {
                "model": model,
                "prompt": "Count from 1 to 60 in words, one per line.",
                "stream": False,
                "think": False,
                "options": {"num_predict": 160},
            },
        )
        count = out.get("eval_count") or 0
        nanos = out.get("eval_duration") or 0
        return round(count / (nanos / 1e9), 1) if nanos else None
    except Exception:  # noqa: BLE001
        return None


def main(models: list[str]) -> int:
    rows = []
    for model in models:
        print(f"\n=== {model}", flush=True)
        try:
            post("/api/show", {"model": model}, timeout=60)
        except Exception as exc:  # noqa: BLE001
            print(f"  not available: {exc}")
            continue
        rt, rsec, rwrong = routing_pass(model)
        tt, tsec, twrong, shapes = tool_pass(model)
        rate = tokens_per_second(model)
        rows.append(
            {
                "model": model,
                "routing": f"{rt}/{len(ROUTING)}",
                "routing_s": round(rsec / len(ROUTING), 1),
                "tools": f"{tt}/{len(TOOL_CASES)}",
                "tool_s": round(tsec / len(TOOL_CASES), 1),
                "tok_s": rate,
                "routing_misses": rwrong,
                "tool_misses": twrong,
                "shapes": shapes,
            }
        )
        print(json.dumps(rows[-1], indent=2), flush=True)
    print("\n=== summary")
    for row in rows:
        print(
            f"{row['model']}\n"
            f"  routing {row['routing']} at {row['routing_s']}s/call · "
            f"tools {row['tools']} at {row['tool_s']}s/call · {row['tok_s']} tok/s"
        )
    print(json.dumps(rows, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
