"""Tests for SQLite SessionStore persistence and conversation history."""

from __future__ import annotations

from pathlib import Path

from agy_tg_bot.config import BotConfig
from agy_tg_bot.models import SessionKey
from agy_tg_bot.storage import SessionStore


def _make_config(tmp_path: Path) -> BotConfig:
    return BotConfig(
        telegram_bot_token="999:test",
        allowed_user_ids=frozenset({1}),
        allowed_chat_ids=frozenset(),
        telegram_proxy_url=None,
        agy_bin="agy",
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
        turn_timeout_seconds=60.0,
        session_idle_timeout_seconds=300.0,
        max_concurrent_runs=2,
        stream_edit_interval_seconds=1.0,
        default_verbose=True,
        long_output_file_threshold=12000,
        max_upload_mb=20,
        db_path=str(tmp_path / "bot.db"),
        log_level="INFO",
    )


def test_session_persistence_and_topic_isolation(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    store = SessionStore(cfg.db_path)
    try:
        k1 = SessionKey(chat_id=-1001, thread_id=10)
        k2 = SessionKey(chat_id=-1001, thread_id=20)

        s1 = store.get_or_create_session(k1, cfg)
        s1.conversation_id = "conv-aaaa-1111"
        s1.permission_mode = "auto"
        s1.sandbox = True
        s1.model = "gemini-4-neon-high"
        store.save_session(s1)

        s2 = store.get_or_create_session(k2, cfg)
        assert s2.conversation_id is None
        assert s2.permission_mode == "default"

        reloaded = store.get_or_create_session(k1, cfg)
        assert reloaded.conversation_id == "conv-aaaa-1111"
        assert reloaded.permission_mode == "auto"
        assert reloaded.sandbox is True
        assert reloaded.model == "gemini-4-neon-high"
    finally:
        store.close()


def test_conversation_history_and_prefix_lookup(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    store = SessionStore(cfg.db_path)
    try:
        key = SessionKey(chat_id=123, thread_id=0)
        store.record_conversation_turn(
            conversation_id="055a398f-db14-4c5f-abbb-1bf03f8120a7",
            key=key,
            workspace=str(tmp_path),
            prompt="Check nginx logs for 502 errors",
            num_turns=1,
            total_tokens=1500,
        )
        store.record_conversation_turn(
            conversation_id="055a398f-db14-4c5f-abbb-1bf03f8120a7",
            key=key,
            workspace=str(tmp_path),
            prompt="Follow up question",
            num_turns=2,
            total_tokens=3200,
        )
        store.record_conversation_turn(
            conversation_id="9ec58bfd-4d67-4f5e-83a5-9d907e9c6b1f",
            key=key,
            workspace=str(tmp_path),
            prompt="Refactor auth middleware",
            num_turns=1,
            total_tokens=900,
        )

        items = store.list_conversations(key, limit=5)
        assert len(items) == 2
        assert items[0].conversation_id == "9ec58bfd-4d67-4f5e-83a5-9d907e9c6b1f"
        assert items[1].num_turns == 2
        assert items[1].total_tokens == 3200
        assert "nginx" in items[1].title

        # Prefix lookup
        found = store.get_conversation("055a398f")
        assert found is not None
        assert found.conversation_id == "055a398f-db14-4c5f-abbb-1bf03f8120a7"
    finally:
        store.close()
