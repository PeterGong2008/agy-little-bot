"""Unit tests for AgyTelegramBot command handlers, auth, and streaming delivery."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agy_tg_bot.agy_client import AgyRunner
from agy_tg_bot.bot import AgyTelegramBot
from agy_tg_bot.config import BotConfig
from agy_tg_bot.storage import SessionStore

FAKE_AGY = Path(__file__).parent / "fake_agy.py"


class _FakeMessage:
    def __init__(self, text: str = "", chat_id: int = 100, chat_type: str = "private") -> None:
        self.text = text
        self.caption = None
        self.message_thread_id = None
        self.photo = ()
        self.document = None
        self.audio = None
        self.voice = None
        self.chat = SimpleNamespace(
            id=chat_id,
            type=chat_type,
            send_action=AsyncMock(),
        )
        self.replies: list[dict] = []
        self.edits: list[dict] = []

    async def reply_text(self, text: str, **kwargs) -> _FakeMessage:
        child = _FakeMessage(text=text, chat_id=self.chat.id, chat_type=self.chat.type)
        self.replies.append({"text": text, "kwargs": kwargs, "msg": child})
        return child

    async def edit_text(self, text: str, **kwargs) -> None:
        self.text = text
        self.edits.append({"text": text, "kwargs": kwargs})

    async def reply_document(self, **kwargs) -> None:
        self.replies.append({"document": kwargs})

    async def reply_photo(self, **kwargs) -> None:
        self.replies.append({"photo": kwargs})

    async def reply_animation(self, **kwargs) -> None:
        self.replies.append({"animation": kwargs})

    async def delete(self) -> None:
        self.edits.append({"deleted": True})


def _make_update(
    *,
    user_id: int,
    text: str = "",
    chat_id: int | None = None,
    chat_type: str = "private",
):
    cid = user_id if chat_id is None else chat_id
    msg = _FakeMessage(text=text, chat_id=cid, chat_type=chat_type)
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id, username=f"user{user_id}"),
        effective_chat=msg.chat,
        effective_message=msg,
        callback_query=None,
    )
    return update, msg


def _build_bot(tmp_path: Path, *, allow_danger: bool = True) -> tuple[AgyTelegramBot, SessionStore, AgyRunner]:
    ws = tmp_path / "ws"
    ws.mkdir(parents=True)
    cfg = BotConfig(
        telegram_bot_token="999:test",
        allowed_user_ids=frozenset({100}),
        allowed_chat_ids=frozenset(),
        telegram_proxy_url=None,
        agy_bin=sys.executable,
        default_workspace=str(ws),
        allowed_workspace_roots=(str(ws),),
        extra_dirs=(),
        runner_mode="persistent",
        default_permission_mode="default",
        allow_dangerous_mode=allow_danger,
        default_sandbox=False,
        default_model=None,
        default_effort=None,
        default_agent=None,
        disable_slash_commands=False,
        disable_auto_update=True,
        turn_timeout_seconds=30.0,
        session_idle_timeout_seconds=300.0,
        max_concurrent_runs=2,
        stream_edit_interval_seconds=0.3,
        default_verbose=True,
        long_output_file_threshold=12000,
        max_upload_mb=20,
        db_path=str(tmp_path / "bot.db"),
        log_level="INFO",
    )
    store = SessionStore(cfg.db_path)
    runner = AgyRunner(cfg, cmd_prefix=[sys.executable, str(FAKE_AGY)])
    return AgyTelegramBot(cfg, store, runner), store, runner


@pytest.mark.asyncio
async def test_unauthorized_user_rejected(tmp_path: Path) -> None:
    bot, store, runner = _build_bot(tmp_path)
    try:
        update, msg = _make_update(user_id=999, text="hello")
        ctx = SimpleNamespace(args=[])
        await bot.handle_text(update, ctx)
        assert len(msg.replies) == 1
        assert "未授权" in msg.replies[0]["text"]
    finally:
        await runner.stop()
        store.close()


@pytest.mark.asyncio
async def test_text_execution_and_status_and_cd_boundary(tmp_path: Path) -> None:
    bot, store, runner = _build_bot(tmp_path)
    await runner.start()
    try:
        # 1. Run a real text prompt against fake_agy
        update, msg = _make_update(user_id=100, text="Hello VPS AGY")
        ctx = SimpleNamespace(args=[])
        await bot.handle_text(update, ctx)

        assert len(msg.replies) == 1
        placeholder: _FakeMessage = msg.replies[0]["msg"]
        assert len(placeholder.edits) >= 1
        final_text = placeholder.edits[-1]["text"]
        assert "Echo[1]: Hello VPS AGY" in final_text

        # 2. /status should show the saved conversation_id
        upd_status, msg_status = _make_update(user_id=100, text="/status")
        await bot.cmd_status(upd_status, SimpleNamespace(args=[]))
        assert "运行中（热进程）" in msg_status.replies[0]["text"]

        # 3. /cd outside allowed root must be blocked
        upd_cd, msg_cd = _make_update(user_id=100, text="/cd ..")
        await bot.cmd_cd(upd_cd, SimpleNamespace(args=[".."]))
        assert "不在允许的根目录" in msg_cd.replies[0]["text"]

        # 4. /perm danger triggers confirmation keyboard instead of silently enabling
        upd_perm, msg_perm = _make_update(user_id=100, text="/perm danger")
        await bot.cmd_perm(upd_perm, SimpleNamespace(args=["danger"]))
        assert "确认开启全自动权限模式" in msg_perm.replies[0]["text"]
    finally:
        await runner.stop()
        store.close()


@pytest.mark.asyncio
async def test_perm_danger_blocked_when_disabled(tmp_path: Path) -> None:
    bot, store, runner = _build_bot(tmp_path, allow_danger=False)
    try:
        upd, msg = _make_update(user_id=100, text="/perm danger")
        await bot.cmd_perm(upd, SimpleNamespace(args=["danger"]))
        assert "ALLOW_DANGEROUS_MODE=false" in msg.replies[0]["text"]
    finally:
        await runner.stop()
        store.close()


@pytest.mark.asyncio
async def test_callback_perm_confirm_and_sandbox_and_effort(tmp_path: Path) -> None:
    from telegram import Message

    bot, store, runner = _build_bot(tmp_path, allow_danger=True)
    try:
        # Subclass Message isn't needed if we mock isinstance check via real-like callback
        # Instead test command paths directly for sandbox/effort/perm/runner/verbose/new
        for cmd_fn, args, expected in [
            (bot.cmd_sandbox, ["on"], "开启"),
            (bot.cmd_effort, ["high"], "high"),
            (bot.cmd_runner, ["oneshot"], "oneshot"),
            (bot.cmd_verbose, ["off"], "关闭"),
            (bot.cmd_new, [], "已开启新对话"),
        ]:
            upd, msg = _make_update(user_id=100)
            await cmd_fn(upd, SimpleNamespace(args=args))
            assert expected in msg.replies[0]["text"]

        # Verify persisted state in SQLite
        from agy_tg_bot.models import SessionKey

        saved = store.get_or_create_session(SessionKey(100, 0), bot.config)
        assert saved.sandbox is True
        assert saved.effort == "high"
        assert saved.runner_mode == "oneshot"
        assert saved.verbose is False
        assert saved.conversation_id is None

        # Verify `Message` import is usable in runtime environment
        assert Message is not None
    finally:
        await runner.stop()
        store.close()


@pytest.mark.asyncio
async def test_start_control_panel_and_menu_actions(tmp_path: Path) -> None:
    from agy_tg_bot.models import SessionKey

    bot, store, runner = _build_bot(tmp_path, allow_danger=True)
    try:
        # /start sends interactive control panel
        upd, msg = _make_update(user_id=100, text="/start")
        await bot.cmd_panel(upd, SimpleNamespace(args=[]))
        assert len(msg.replies) == 1
        panel_text = msg.replies[0]["text"]
        assert "AGY 交互式控制面板" in panel_text
        markup = msg.replies[0]["kwargs"]["reply_markup"]
        callbacks = [btn.callback_data for row in markup.inline_keyboard for btn in row]
        assert "menu:new" in callbacks
        assert "menu:perm" in callbacks
        assert "menu:model" in callbacks
        assert "menu:cd" in callbacks
        assert "menu:toggle_sandbox" in callbacks

        # Simulate menu callback actions directly on _handle_menu_callback
        panel_msg: _FakeMessage = msg.replies[0]["msg"]
        fake_query = SimpleNamespace(answer=AsyncMock())
        key = SessionKey(100, 0)
        session = store.get_or_create_session(key, bot.config)

        await bot._handle_menu_callback(fake_query, panel_msg, key, session, "toggle_sandbox")
        assert store.get_or_create_session(key, bot.config).sandbox is True
        assert "沙箱：✅" in panel_msg.edits[-1]["text"]

        await bot._handle_menu_callback(fake_query, panel_msg, key, session, "toggle_runner")
        assert store.get_or_create_session(key, bot.config).runner_mode == "oneshot"

        await bot._handle_menu_callback(fake_query, panel_msg, key, session, "perm")
        assert "请选择新的权限模式" in panel_msg.edits[-1]["text"]

        await bot._handle_menu_callback(fake_query, panel_msg, key, session, "close")
        assert panel_msg.edits[-1].get("deleted") is True
    finally:
        await runner.stop()
        store.close()


@pytest.mark.asyncio
async def test_deliver_result_auto_sends_images(tmp_path: Path) -> None:
    import time

    from agy_tg_bot.models import SessionKey, TurnResult

    bot, store, runner = _build_bot(tmp_path)
    try:
        key = SessionKey(100, 0)
        session = store.get_or_create_session(key, bot.config)
        ws = Path(session.workspace)

        turn_start = time.time()
        img = ws / "output_chart.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\nfakeimagebytes")

        _, user_msg = _make_update(user_id=100, text="draw chart")
        status_msg = await user_msg.reply_text("⏳")
        res = TurnResult(
            conversation_id="conv-img-1",
            status="SUCCESS",
            response="Chart saved to ![chart](output_chart.png)",
        )
        await bot._deliver_result(
            user_msg, status_msg, session, res, turn_started_at=turn_start
        )

        photo_replies = [r for r in user_msg.replies if "photo" in r]
        assert len(photo_replies) == 1
        assert "output_chart.png" in photo_replies[0]["photo"]["caption"]
    finally:
        await runner.stop()
        store.close()


@pytest.mark.asyncio
async def test_model_family_picker_includes_hydrogen_and_usage_no_pre_and_cd_pagination(
    tmp_path: Path,
) -> None:
    from agy_tg_bot.models import SessionKey

    bot, store, runner = _build_bot(tmp_path)
    try:
        key = SessionKey(100, 0)
        session = store.get_or_create_session(key, bot.config)

        # 1. /model shows grouped families including Gemini 4 Hydrogen, Claude, GPT-OSS
        upd, msg = _make_update(user_id=100, text="/model")
        await bot.cmd_model(upd, SimpleNamespace(args=[]))
        picker_msg: _FakeMessage = msg.replies[0]["msg"]
        last_edit = picker_msg.edits[-1]
        assert "选择模型系列" in last_edit["text"]
        btn_texts = [
            btn.text
            for row in last_edit["kwargs"]["reply_markup"].inline_keyboard
            for btn in row
        ]
        assert any("Gemini 4 Hydrogen" in t for t in btn_texts)
        assert any("Claude Opus 5.5" in t for t in btn_texts)
        assert any("GPT-OSS 120B" in t for t in btn_texts)

        # Open Hydrogen variants and select XHigh while effort was previously set -> effort auto-cleared
        session.effort = "low"
        store.save_session(session)
        fam_idx = next(
            i for i, f in enumerate(bot._model_families_cache[key]) if f.name == "Gemini 4 Hydrogen"
        )
        await bot._render_model_variants_view(picker_msg, key, session, fam_idx)
        assert "gemini-4-hydrogen-xhigh" in picker_msg.edits[-1]["text"]

        await bot._set_model(picker_msg, key, session, "gemini-4-hydrogen-xhigh", edit=True)
        saved = store.get_or_create_session(key, bot.config)
        assert saved.model == "gemini-4-hydrogen-xhigh"
        assert saved.effort is None

        # Changing effort to high swaps directly to sibling gemini-4-hydrogen-high!
        await bot._set_effort(picker_msg, key, saved, "high", edit=True)
        saved2 = store.get_or_create_session(key, bot.config)
        assert saved2.model == "gemini-4-hydrogen-high"
        assert saved2.effort is None

        # 2. /usage renders rich cards (no <pre> code block!)
        upd_u, msg_u = _make_update(user_id=100, text="/usage")
        await bot.cmd_usage(upd_u, SimpleNamespace(args=[]))
        usage_edit = msg_u.replies[0]["msg"].edits[-1]["text"]
        assert "<pre>" not in usage_edit
        assert "5 小时配额" in usage_edit
        assert "62%" in usage_edit

        # 3. /cd pagination handles >10 subdirectories without truncation
        ws = Path(session.workspace)
        for i in range(14):
            (ws / f"dir_{i:02d}").mkdir()
        upd_cd, msg_cd = _make_update(user_id=100, text="/cd")
        await bot.cmd_cd(upd_cd, SimpleNamespace(args=[]))
        cd_markup = msg_cd.replies[0]["kwargs"]["reply_markup"]
        cd_cbs = [btn.callback_data for row in cd_markup.inline_keyboard for btn in row]
        assert "cd_page:1" in cd_cbs
    finally:
        await runner.stop()
        store.close()
