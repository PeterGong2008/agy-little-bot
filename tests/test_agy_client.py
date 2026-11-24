"""End-to-end async subprocess tests for AgyRunner using tests/fake_agy.py."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from agy_tg_bot.agy_client import AgyRunner
from agy_tg_bot.config import BotConfig
from agy_tg_bot.models import SessionState, TurnProgress

FAKE_AGY = Path(__file__).parent / "fake_agy.py"


def _make_config(tmp_path: Path, *, turn_timeout: float = 30.0) -> BotConfig:
    return BotConfig(
        telegram_bot_token="999:test",
        allowed_user_ids=frozenset({1}),
        allowed_chat_ids=frozenset(),
        telegram_proxy_url=None,
        agy_bin=sys.executable,
        default_workspace=str(tmp_path),
        allowed_workspace_roots=(),
        extra_dirs=(),
        runner_mode="persistent",
        default_permission_mode="default",
        allow_dangerous_mode=True,
        default_sandbox=False,
        default_model=None,
        default_effort=None,
        default_agent=None,
        disable_slash_commands=False,
        disable_auto_update=True,
        turn_timeout_seconds=turn_timeout,
        session_idle_timeout_seconds=300.0,
        max_concurrent_runs=2,
        stream_edit_interval_seconds=0.3,
        default_verbose=True,
        long_output_file_threshold=12000,
        max_upload_mb=20,
        db_path=str(tmp_path / "bot.db"),
        log_level="INFO",
    )


@pytest.mark.asyncio
async def test_persistent_multi_turn_reuse_and_delta_usage(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    runner = AgyRunner(cfg, cmd_prefix=[sys.executable, str(FAKE_AGY)])
    await runner.start()
    try:
        session = SessionState(chat_id=1, thread_id=0, workspace=str(tmp_path))
        seen_progress: list[TurnProgress] = []

        async def _cb(p: TurnProgress) -> None:
            seen_progress.append(p)

        r1 = await runner.run_turn(session, "TRIGGER_TOOL hello first", on_progress=_cb)
        assert r1.ok is True
        assert r1.conversation_id
        assert "Echo[1]: TRIGGER_TOOL hello first" in r1.response
        assert len(r1.tools) == 1
        assert r1.tools[0].name == "run_command"
        assert r1.tools[0].summary == "git status"
        assert r1.turn_usage.total_tokens == 125
        assert len(seen_progress) >= 2
        assert runner.has_live_worker(session.key)

        # Save conversation_id just like bot.py does and run turn 2 on the SAME warm process
        session.conversation_id = r1.conversation_id
        r2 = await runner.run_turn(session, "RECALL second turn")
        assert r2.ok is True
        assert r2.conversation_id == r1.conversation_id
        assert "Previous prompt was: TRIGGER_TOOL hello first" in r2.response
        # Cumulative is 250, per-turn delta must be 125!
        assert r2.usage.total_tokens == 250
        assert r2.turn_usage.total_tokens == 125
    finally:
        await runner.stop()


@pytest.mark.asyncio
async def test_large_ndjson_line_and_permission_denial_capture(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    runner = AgyRunner(cfg, cmd_prefix=[sys.executable, str(FAKE_AGY)])
    await runner.start()
    try:
        session = SessionState(chat_id=2, thread_id=0, workspace=str(tmp_path))
        res = await runner.run_turn(session, "TRIGGER_BIG_LINE TRIGGER_PERM_DENIED check")
        assert res.ok is True
        assert len(res.tools) == 1
        assert res.tools[0].name == "run_command"
        assert any("soft-denied" in d for d in res.permission_denials)
    finally:
        await runner.stop()


@pytest.mark.asyncio
async def test_oneshot_mode_and_cli_only_slash_guard(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    runner = AgyRunner(cfg, cmd_prefix=[sys.executable, str(FAKE_AGY)])
    await runner.start()
    try:
        # Persistent mode rejects CLI-only /model before touching subprocess
        s_pers = SessionState(chat_id=3, thread_id=0, workspace=str(tmp_path), runner_mode="persistent")
        bad = await runner.run_turn(s_pers, "/model")
        assert bad.ok is False
        assert "持久流式会话" in (bad.error or "")

        # Oneshot mode runs cleanly with flags
        s_one = SessionState(
            chat_id=4,
            thread_id=0,
            workspace=str(tmp_path),
            runner_mode="oneshot",
            permission_mode="auto",
            sandbox=True,
            model="gemini-4-neon-xhigh",
        )
        res = await runner.run_turn(s_one, "ping oneshot")
        assert res.ok is True
        assert "perm=auto" in res.response
        assert "sandbox=True" in res.response
        assert "model=gemini-4-neon-xhigh" in res.response
    finally:
        await runner.stop()


@pytest.mark.asyncio
async def test_cancel_turn_aborts_slow_run(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    runner = AgyRunner(cfg, cmd_prefix=[sys.executable, str(FAKE_AGY)])
    await runner.start()
    try:
        session = SessionState(chat_id=5, thread_id=0, workspace=str(tmp_path))
        task = asyncio.create_task(runner.run_turn(session, "TRIGGER_SLOW please wait"))

        # Wait until process is marked active
        for _ in range(50):
            if runner.is_running(session.key):
                break
            await asyncio.sleep(0.05)

        assert runner.is_running(session.key)
        canceled = await runner.cancel_turn(session.key)
        assert canceled is True

        res = await asyncio.wait_for(task, timeout=10.0)
        assert res.status == "CANCELED"
        assert not runner.has_live_worker(session.key)
    finally:
        await runner.stop()


@pytest.mark.asyncio
async def test_auto_respawn_after_worker_exits_between_turns(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    runner = AgyRunner(cfg, cmd_prefix=[sys.executable, str(FAKE_AGY)])
    await runner.start()
    try:
        session = SessionState(chat_id=6, thread_id=0, workspace=str(tmp_path))
        r1 = await runner.run_turn(session, "TRIGGER_CRASH_AFTER_TURN hello")
        assert r1.ok is True
        session.conversation_id = r1.conversation_id

        # Give fake_agy a moment to exit after turn 1
        await asyncio.sleep(0.15)

        # Turn 2 should transparently spawn a fresh worker resuming conversation_id
        r2 = await runner.run_turn(session, "after crash")
        assert r2.ok is True
        assert r2.conversation_id == r1.conversation_id
        assert "Echo[1]: after crash" in r2.response
    finally:
        await runner.stop()


@pytest.mark.asyncio
async def test_standalone_cli_helpers(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    runner = AgyRunner(cfg, cmd_prefix=[sys.executable, str(FAKE_AGY)])

    models, err = await runner.list_models(cwd=str(tmp_path))
    assert err is None
    assert ("gemini-4-neon-xhigh", "Gemini 4 Neon (XHigh)") in models
    assert ("gemini-4-hydrogen-xhigh", "Gemini 4 Hydrogen (XHigh)") in models
    assert ("claude-opus-5-5-high", "Claude Opus 5.5 (High)") in models
    assert ("gpt-oss-120b-medium", "GPT-OSS 120B (Medium)") in models
    assert len(models) == 7

    agents, err = await runner.list_agents(cwd=str(tmp_path))
    assert err is None
    assert ("reviewer", "Strict code review agent") in agents

    usage, err = await runner.get_usage_report(cwd=str(tmp_path))
    assert err is None
    assert "90%" in usage
    assert "62%" in usage


@pytest.mark.asyncio
async def test_turn_timeout_and_flag_change_respawn(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path, turn_timeout=0.5)
    runner = AgyRunner(cfg, cmd_prefix=[sys.executable, str(FAKE_AGY)])
    await runner.start()
    try:
        s_timeout = SessionState(chat_id=7, thread_id=0, workspace=str(tmp_path))
        res_t = await runner.run_turn(s_timeout, "TRIGGER_SLOW wait forever")
        assert res_t.status == "TIMEOUT"
        assert "超时" in (res_t.error or "")

        # Verify changing permission_mode respawns worker with new flag while keeping conversation_id
        s_flag = SessionState(chat_id=8, thread_id=0, workspace=str(tmp_path), permission_mode="default")
        r1 = await runner.run_turn(s_flag, "first")
        assert r1.ok and "perm=default" in r1.response
        s_flag.conversation_id = r1.conversation_id
        s_flag.permission_mode = "auto"
        r2 = await runner.run_turn(s_flag, "second")
        assert r2.ok and r2.conversation_id == r1.conversation_id
        assert "perm=auto" in r2.response
    finally:
        await runner.stop()
