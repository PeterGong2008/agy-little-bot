"""Realistic mock `agy` CLI implementing the headless stream-json protocol for tests."""

from __future__ import annotations

import json
import os
import sys
import time
import uuid


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _parse_args(argv: list[str]) -> dict:
    parsed: dict = {
        "subcmd": None,
        "input_format": "text",
        "output_format": "text",
        "prompt": None,
        "conversation": None,
        "mode": None,
        "danger": False,
        "sandbox": False,
        "model": None,
        "effort": None,
        "agent": None,
    }
    if argv and argv[0] in {"models", "agents"}:
        parsed["subcmd"] = argv[0]
        return parsed

    i = 0
    while i < len(argv):
        a = argv[i]
        if a in {"-p", "--print", "--prompt"} and i + 1 < len(argv):
            parsed["prompt"] = argv[i + 1]
            i += 2
        elif a == "--input-format" and i + 1 < len(argv):
            parsed["input_format"] = argv[i + 1]
            i += 2
        elif a == "--output-format" and i + 1 < len(argv):
            parsed["output_format"] = argv[i + 1]
            i += 2
        elif a == "--conversation" and i + 1 < len(argv):
            parsed["conversation"] = argv[i + 1]
            i += 2
        elif a == "--mode" and i + 1 < len(argv):
            parsed["mode"] = argv[i + 1]
            i += 2
        elif a == "--model" and i + 1 < len(argv):
            parsed["model"] = argv[i + 1]
            i += 2
        elif a == "--effort" and i + 1 < len(argv):
            parsed["effort"] = argv[i + 1]
            i += 2
        elif a == "--agent" and i + 1 < len(argv):
            parsed["agent"] = argv[i + 1]
            i += 2
        elif a == "--dangerously-skip-permissions":
            parsed["danger"] = True
            i += 1
        elif a == "--sandbox":
            parsed["sandbox"] = True
            i += 1
        elif a in {"--print-timeout", "--add-dir"} and i + 1 < len(argv):
            i += 2
        else:
            i += 1
    return parsed


def _run_turn(
    *,
    prompt: str,
    conv_id: str,
    turn_num: int,
    cum_usage: dict[str, int],
    cum_duration: float,
    parsed: dict,
    history: list[str],
) -> tuple[float, bool]:
    """Execute one simulated turn and emit step_update + result events."""
    step_base = (turn_num - 1) * 4

    _emit(
        {
            "event": "step_update",
            "step_update": {
                "conversation_id": conv_id,
                "step_index": step_base,
                "state": "DONE",
                "step_type": "user_input",
            },
        }
    )

    if "TRIGGER_SLOW" in prompt:
        _emit(
            {
                "event": "step_update",
                "step_update": {
                    "conversation_id": conv_id,
                    "step_index": step_base + 1,
                    "state": "ACTIVE",
                    "step_type": "agent_response",
                    "text_delta": "Working slowly...",
                },
            }
        )
        for _ in range(100):
            time.sleep(0.1)

    if "TRIGGER_ERROR" in prompt:
        _emit(
            {
                "event": "result",
                "result": {
                    "conversation_id": conv_id,
                    "status": "ERROR",
                    "response": "",
                    "error": "Simulated agent error",
                    "duration_seconds": cum_duration + 0.1,
                    "num_turns": turn_num,
                    "usage": dict(cum_usage),
                },
            }
        )
        return cum_duration + 0.1, True

    if "TRIGGER_BIG_LINE" in prompt:
        big_payload = "X" * 150_000  # > asyncio default 64 KiB StreamReader limit
        _emit(
            {
                "event": "step_update",
                "step_update": {
                    "conversation_id": conv_id,
                    "step_index": step_base + 1,
                    "state": "DONE",
                    "step_type": "tool",
                    "tool_name": "run_command",
                    "duration_seconds": 0.12,
                    "tool_info": {
                        "name": "run_command",
                        "parameters": {"CommandLine": "cat huge.log"},
                        "output": big_payload,
                    },
                },
            }
        )

    if "TRIGGER_PERM_DENIED" in prompt:
        sys.stderr.write(
            "notice: tool run_command was soft-denied because it requires approval; "
            "add command(rm) to permissions.allow or pass --dangerously-skip-permissions\n"
        )
        sys.stderr.flush()

    if "TRIGGER_TOOL" in prompt:
        _emit(
            {
                "event": "step_update",
                "step_update": {
                    "conversation_id": conv_id,
                    "step_index": step_base + 1,
                    "state": "ACTIVE",
                    "step_type": "tool",
                    "tool_name": "run_command",
                    "tool_info": {
                        "name": "run_command",
                        "parameters": {"CommandLine": "git status"},
                    },
                },
            }
        )
        _emit(
            {
                "event": "step_update",
                "step_update": {
                    "conversation_id": conv_id,
                    "step_index": step_base + 1,
                    "state": "DONE",
                    "step_type": "tool",
                    "tool_name": "run_command",
                    "duration_seconds": 0.08,
                    "tool_info": {
                        "name": "run_command",
                        "parameters": {"CommandLine": "git status"},
                        "output": "On branch main\n",
                    },
                },
            }
        )

    history.append(prompt)
    meta = f"pid={os.getpid()} perm={parsed['mode'] or ('danger' if parsed['danger'] else 'default')} sandbox={parsed['sandbox']} model={parsed['model'] or 'default'}"
    if "RECALL" in prompt and len(history) >= 2:
        reply = f"Previous prompt was: {history[-2]} ({meta})"
    else:
        reply = f"Echo[{turn_num}]: {prompt} ({meta})"

    # Stream two text_delta chunks
    half = max(1, len(reply) // 2)
    _emit(
        {
            "event": "step_update",
            "step_update": {
                "conversation_id": conv_id,
                "step_index": step_base + 2,
                "state": "ACTIVE",
                "step_type": "agent_response",
                "text_delta": reply[:half],
            },
        }
    )
    _emit(
        {
            "event": "step_update",
            "step_update": {
                "conversation_id": conv_id,
                "step_index": step_base + 2,
                "state": "DONE",
                "step_type": "agent_response",
                "text_delta": reply[half:] + "\n",
                "duration_seconds": 0.25,
            },
        }
    )

    cum_usage["input_tokens"] += 100
    cum_usage["output_tokens"] += 25
    cum_usage["thinking_tokens"] += 10
    cum_usage["total_tokens"] += 125
    cum_duration += 0.3

    _emit(
        {
            "event": "result",
            "result": {
                "conversation_id": conv_id,
                "status": "SUCCESS",
                "response": reply + "\n",
                "duration_seconds": round(cum_duration, 3),
                "num_turns": turn_num,
                "usage": dict(cum_usage),
            },
        }
    )

    should_exit = "TRIGGER_CRASH_AFTER_TURN" in prompt
    return cum_duration, should_exit


def main() -> int:
    parsed = _parse_args(sys.argv[1:])

    if parsed["subcmd"] == "models":
        print("gemini-4-neon-xhigh            Gemini 4 Neon (XHigh)")
        print("gemini-3.8-flash-medium        Gemini 3.8 Flash (Medium)")
        return 0

    if parsed["subcmd"] == "agents":
        print("default   General purpose coding agent")
        print("reviewer  Strict code review agent")
        return 0

    if parsed["prompt"] == "/usage":
        print("Plan: Google AI Pro\nRemaining quota: 92%")
        return 0

    conv_id = parsed["conversation"] or str(uuid.uuid4())
    perm_mode = "always-proceed" if parsed["danger"] else (parsed["mode"] or "request-review")
    cum_usage = {
        "input_tokens": 0,
        "output_tokens": 0,
        "thinking_tokens": 0,
        "cache_read_tokens": 0,
        "total_tokens": 0,
    }
    cum_duration = 0.0
    history: list[str] = []

    if parsed["input_format"] == "stream-json":
        emitted_init = False
        turn_num = 0
        for raw_line in sys.stdin:
            line = raw_line.strip()
            if not line:
                continue
            msg = json.loads(line)
            if not emitted_init:
                _emit(
                    {
                        "event": "init",
                        "conversation_id": conv_id,
                        "init": {
                            "cwd": os.getcwd(),
                            "tools": ["run_command", "read_file", "edit_file"],
                            "permission_mode": perm_mode,
                        },
                    }
                )
                emitted_init = True
            content = msg.get("message", {}).get("content", "")
            turn_num += 1
            cum_duration, should_exit = _run_turn(
                prompt=str(content),
                conv_id=conv_id,
                turn_num=turn_num,
                cum_usage=cum_usage,
                cum_duration=cum_duration,
                parsed=parsed,
                history=history,
            )
            if should_exit:
                return 0
        return 0

    # Oneshot `-p` mode
    _emit(
        {
            "event": "init",
            "conversation_id": conv_id,
            "init": {
                "cwd": os.getcwd(),
                "tools": ["run_command", "read_file", "edit_file"],
                "permission_mode": perm_mode,
            },
        }
    )
    _run_turn(
        prompt=str(parsed["prompt"] or ""),
        conv_id=conv_id,
        turn_num=1,
        cum_usage=cum_usage,
        cum_duration=cum_duration,
        parsed=parsed,
        history=history,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
