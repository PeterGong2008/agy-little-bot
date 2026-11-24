"""Telegram Bot application, command handlers, inline keyboards, and streaming UX."""

from __future__ import annotations

import asyncio
import contextlib
import html
import io
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, RetryAfter, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .agy_client import AgyRunner
from .config import BotConfig
from .formatting import (
    TELEGRAM_MAX_MESSAGE_LEN,
    format_token_count,
    render_streaming_preview,
    render_telegram_html_chunks,
    render_turn_footer,
)
from .media import collect_turn_images, should_send_as_photo
from .models import (
    VALID_EFFORT_LEVELS,
    VALID_PERMISSION_MODES,
    VALID_RUNNER_MODES,
    SessionKey,
    SessionState,
    TurnProgress,
    TurnResult,
)
from .storage import SessionStore

logger = logging.getLogger(__name__)

# AGY agent-level slash commands that are safe to pass through as prompts in headless mode
PASSTHROUGH_AGY_COMMANDS: tuple[str, ...] = (
    "goal",
    "plan",
    "compact",
    "boost",
    "grill_me",
    "learn",
    "btw",
    "swarm",
    "teamwork",
)

PERM_DESCRIPTIONS: dict[str, str] = {
    "default": "默认（遵循 settings.json / CLI 默认）",
    "auto": "Auto（自动批准工作区编辑与安全/沙箱命令）",
    "accept-edits": "Accept-Edits（自动批准文件编辑）",
    "plan": "Plan（只读调研与规划）",
    "danger": "Danger（--dangerously-skip-permissions 全自动）",
}

_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._\-\u4e00-\u9fff]+")


def _sanitize_filename(name: str, fallback: str = "upload.bin") -> str:
    base = Path(name).name.strip() if name else ""
    cleaned = _SAFE_FILENAME_RE.sub("_", base).strip("._")
    if not cleaned:
        return fallback
    return cleaned[:120]


def _extract_session_key(update: Update) -> SessionKey | None:
    chat = update.effective_chat
    if chat is None:
        return None
    msg = update.effective_message
    thread_id = 0
    if msg is not None and chat.type != "private":
        tid = getattr(msg, "message_thread_id", None)
        if isinstance(tid, int) and tid > 0:
            thread_id = tid
    return SessionKey(chat_id=chat.id, thread_id=thread_id)


def _format_timestamp(ts: float) -> str:
    if ts <= 0:
        return "-"
    return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")


class AgyTelegramBot:
    """Orchestrates Telegram updates, per-chat locks, SQLite state, and AGY CLI runs."""

    def __init__(
        self,
        config: BotConfig,
        store: SessionStore,
        runner: AgyRunner,
    ) -> None:
        self.config = config
        self.store = store
        self.runner = runner
        self._session_locks: dict[SessionKey, asyncio.Lock] = {}
        self._model_cache: dict[SessionKey, list[tuple[str, str]]] = {}
        self._cd_cache: dict[SessionKey, list[str]] = {}

    def _get_lock(self, key: SessionKey) -> asyncio.Lock:
        lock = self._session_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[key] = lock
        return lock

    async def _authorize(self, update: Update) -> bool:
        user = update.effective_user
        chat = update.effective_chat
        user_id = user.id if user else None
        chat_id = chat.id if chat else None
        if self.config.is_user_authorized(user_id, chat_id):
            return True

        username = f"@{user.username}" if (user and user.username) else "unknown"
        logger.warning(
            "Rejected unauthorized access: user_id=%s username=%s chat_id=%s",
            user_id,
            username,
            chat_id,
        )
        if update.callback_query is not None:
            with contextlib.suppress(TelegramError):
                await update.callback_query.answer("⛔ 无权操作此机器人", show_alert=True)
        elif update.effective_message is not None and chat is not None and chat.type == "private":
            with contextlib.suppress(TelegramError):
                await update.effective_message.reply_text(
                    f"⛔ 未授权访问。\n你的 Telegram User ID: {user_id}\n"
                    "请将该 ID 添加到 VPS 的 .env 中 ALLOWED_USER_IDS 后重启服务。"
                )
        return False

    def build_application(self) -> Application[Any, Any, Any, Any, Any, Any]:
        """Construct and wire the python-telegram-bot Application."""
        builder = (
            ApplicationBuilder()
            .token(self.config.telegram_bot_token)
            .concurrent_updates(True)
            .post_init(self._post_init)
            .post_shutdown(self._post_shutdown)
        )
        if self.config.telegram_proxy_url:
            builder = builder.proxy(self.config.telegram_proxy_url).get_updates_proxy(
                self.config.telegram_proxy_url
            )

        app = builder.build()

        # Core commands
        app.add_handler(CommandHandler(["start", "menu"], self.cmd_panel))
        app.add_handler(CommandHandler("help", self.cmd_help))
        app.add_handler(CommandHandler(["new", "clear"], self.cmd_new))
        app.add_handler(CommandHandler(["cancel", "stop"], self.cmd_cancel))
        app.add_handler(CommandHandler("status", self.cmd_status))
        app.add_handler(CommandHandler("cd", self.cmd_cd))
        app.add_handler(CommandHandler("ls", self.cmd_ls))
        app.add_handler(CommandHandler("get", self.cmd_get))
        app.add_handler(CommandHandler(["perm", "mode"], self.cmd_perm))
        app.add_handler(CommandHandler("sandbox", self.cmd_sandbox))
        app.add_handler(CommandHandler("runner", self.cmd_runner))
        app.add_handler(CommandHandler(["model", "models"], self.cmd_model))
        app.add_handler(CommandHandler("effort", self.cmd_effort))
        app.add_handler(CommandHandler(["agent", "agents"], self.cmd_agent))
        app.add_handler(CommandHandler("usage", self.cmd_usage))
        app.add_handler(CommandHandler("history", self.cmd_history))
        app.add_handler(CommandHandler("resume", self.cmd_resume))
        app.add_handler(CommandHandler("verbose", self.cmd_verbose))
        app.add_handler(CommandHandler("restart", self.cmd_restart))
        app.add_handler(CommandHandler(["pass", "skill"], self.cmd_pass))

        # Safe AGY agent slash commands forwarded as prompts
        app.add_handler(CommandHandler(list(PASSTHROUGH_AGY_COMMANDS), self.cmd_passthrough))

        # Inline button callbacks
        app.add_handler(CallbackQueryHandler(self.handle_callback))

        # Media attachments (photos, documents, audio, voice)
        app.add_handler(
            MessageHandler(
                filters.PHOTO | filters.Document.ALL | filters.AUDIO | filters.VOICE,
                self.handle_media,
            )
        )

        # Plain text prompts
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_text))

        # Global error handler
        app.add_error_handler(self._handle_error)

        return app

    async def _post_init(self, app: Application[Any, Any, Any, Any, Any, Any]) -> None:
        await self.runner.start()
        commands = [
            BotCommand("start", "打开交互式控制面板"),
            BotCommand("menu", "打开交互式控制面板"),
            BotCommand("help", "查看命令与使用说明"),
            BotCommand("new", "开启全新对话"),
            BotCommand("cancel", "中止当前正在执行的任务"),
            BotCommand("status", "查看当前会话、工作区与权限状态"),
            BotCommand("cd", "查看或切换 VPS 工作目录"),
            BotCommand("ls", "列出当前工作区文件"),
            BotCommand("get", "下载 VPS 文件到 Telegram"),
            BotCommand("perm", "切换 AGY 工具权限模式"),
            BotCommand("sandbox", "开关终端 OS 沙箱 (--sandbox)"),
            BotCommand("model", "查看/切换模型"),
            BotCommand("effort", "设置推理强度 (low..max)"),
            BotCommand("agent", "查看/切换自定义 Agent"),
            BotCommand("usage", "查看配额与用量"),
            BotCommand("history", "查看历史会话并一键恢复"),
            BotCommand("resume", "按 ID 恢复历史会话"),
            BotCommand("runner", "切换 persistent/oneshot 引擎模式"),
            BotCommand("verbose", "开关工具调用与 Token 摘要"),
            BotCommand("restart", "重启后台 agy 进程（保留上下文）"),
            BotCommand("pass", "透传任意指令或 /skill 给 agy"),
        ]
        try:
            await app.bot.set_my_commands(commands)
        except TelegramError:
            logger.warning("Failed to register Telegram bot command menu", exc_info=True)
        logger.info("AGY Telegram Bot initialized (runner_mode=%s)", self.config.runner_mode)

    async def _post_shutdown(self, app: Application[Any, Any, Any, Any, Any, Any]) -> None:
        logger.info("Shutting down AGY runner and SQLite store...")
        await self.runner.stop()
        self.store.close()

    async def _handle_error(
        self, update: object, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        logger.error("Unhandled Telegram update error", exc_info=context.error)

    # ------------------------------------------------------------------
    # Interactive Control Panel (/start & /menu)
    # ------------------------------------------------------------------

    @staticmethod
    def _back_to_panel_row() -> list[InlineKeyboardButton]:
        return [InlineKeyboardButton("⬅️ 返回主面板", callback_data="menu:home")]

    def _build_panel_view(
        self, key: SessionKey, session: SessionState
    ) -> tuple[str, InlineKeyboardMarkup]:
        worker_badge = "🟢 热进程" if self.runner.has_live_worker(key) else "⚪ 按需启动"
        busy_badge = "⚙️ 执行中" if self.runner.is_running(key) else "空闲"
        cid_short = session.conversation_id[:8] if session.conversation_id else "新会话"
        runner_label = "常驻流式" if session.runner_mode == "persistent" else "单次按需"

        text = (
            "🎛 <b>AGY 交互式控制面板</b>\n"
            f"• 状态：{busy_badge} | 进程：{worker_badge}\n"
            f"• 工作区：<code>{html.escape(session.workspace)}</code>\n"
            f"• 对话 ID：<code>{html.escape(cid_short)}</code>\n"
            f"• 模型：<code>{html.escape(session.model or 'CLI 默认')}</code>"
            f" | 强度：<code>{html.escape(session.effort or '默认')}</code>\n"
            f"• 权限：<code>{html.escape(session.permission_mode)}</code>"
            f" | 沙箱：{'✅' if session.sandbox else '❌'}"
            f" | 引擎：<code>{html.escape(session.runner_mode)}</code>\n"
            f"• 详细轨迹：{'✅' if session.verbose else '❌'}"
            f" | 自动发图：{'✅' if self.config.auto_send_images else '❌'}\n\n"
            "<i>点击下方按钮快速切换设置，或直接发送文字/图片开始对话。</i>"
        )

        rows: list[list[InlineKeyboardButton]] = [
            [
                InlineKeyboardButton("🆕 新建对话", callback_data="menu:new"),
                InlineKeyboardButton("🔄 刷新状态", callback_data="menu:home"),
            ],
            [
                InlineKeyboardButton("🧠 切换模型", callback_data="menu:model"),
                InlineKeyboardButton("⚡ 推理强度", callback_data="menu:effort"),
            ],
            [
                InlineKeyboardButton("🔐 权限模式", callback_data="menu:perm"),
                InlineKeyboardButton(
                    f"🛡 沙箱: {'开' if session.sandbox else '关'}",
                    callback_data="menu:toggle_sandbox",
                ),
            ],
            [
                InlineKeyboardButton("📂 切换目录", callback_data="menu:cd"),
                InlineKeyboardButton("📜 历史会话", callback_data="menu:history"),
            ],
            [
                InlineKeyboardButton(
                    f"⚙️ 引擎: {runner_label}",
                    callback_data="menu:toggle_runner",
                ),
                InlineKeyboardButton(
                    f"📝 详情: {'开' if session.verbose else '关'}",
                    callback_data="menu:toggle_verbose",
                ),
            ],
            [
                InlineKeyboardButton("📊 配额用量", callback_data="menu:usage"),
                InlineKeyboardButton("♻️ 重启进程", callback_data="menu:restart"),
            ],
            [
                InlineKeyboardButton("❓ 命令帮助", callback_data="menu:help"),
                InlineKeyboardButton("✖️ 关闭面板", callback_data="menu:close"),
            ],
        ]
        return text, InlineKeyboardMarkup(rows)

    def _build_help_text(self) -> str:
        return (
            "🤖 <b>AGY VPS Telegram Bot</b>\n"
            "直接发送任意文本、图片或文件即可与 VPS 本机 <code>agy</code> 交互；"
            "对话中生成或引用的图片将自动发送回 Telegram。\n\n"
            "<b>控制面板与会话</b>\n"
            "• /start 或 /menu — 打开交互式控制面板\n"
            "• /new — 开启全新对话\n"
            "• /cancel — 中止当前正在执行的任务\n"
            "• /status — 查看当前状态、工作区、模型与权限\n"
            "• /history — 查看历史会话并点击恢复\n"
            "• /resume <code>&lt;id&gt;</code> — 按 ID 恢复指定会话\n"
            "• /restart — 重启后台 agy 进程（保留对话 ID）\n\n"
            "<b>VPS 工作区与文件</b>\n"
            "• /cd <code>[路径]</code> — 切换或浏览工作目录\n"
            "• /ls <code>[路径]</code> — 查看目录内容\n"
            "• /get <code>&lt;路径&gt;</code> — 下载 VPS 任意文件到 Telegram\n"
            "• 直接发送图片/文件 — 自动保存到工作区 <code>.tg_inbox/</code> 并交给 agy 读取\n\n"
            "<b>权限与模型配置</b>\n"
            "• /perm <code>[default|auto|accept-edits|plan|danger]</code> — 切换工具权限\n"
            "• /sandbox <code>[on|off]</code> — 开关终端沙箱\n"
            "• /model <code>[slug|default]</code> — 列出/切换模型\n"
            "• /effort <code>[low|medium|high|xhigh|max|default]</code> — 设置推理强度\n"
            "• /agent <code>[name|default]</code> — 查看/切换自定义 Agent\n"
            "• /runner <code>[persistent|oneshot]</code> — 切换常驻流式 / 单次模式\n"
            "• /verbose <code>[on|off]</code> — 开关工具轨迹与 Token 摘要\n"
            "• /usage — 查看配额用量\n"
            "• /pass <code>&lt;内容&gt;</code> — 透传自定义 Skill（如 <code>/pass /my-skill</code>）\n\n"
            "<i>支持透传的 AGY 指令：/goal /plan /compact /boost /learn /btw /swarm /teamwork</i>"
        )

    async def cmd_panel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._authorize(update):
            return
        msg = update.effective_message
        key = _extract_session_key(update)
        if msg is None or key is None:
            return
        session = self.store.get_or_create_session(key, self.config)
        text, markup = self._build_panel_view(key, session)
        await self._safe_reply_html(msg, text, reply_markup=markup)

    # ------------------------------------------------------------------
    # Command handlers
    # ------------------------------------------------------------------

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._authorize(update):
            return
        msg = update.effective_message
        if msg is None:
            return
        markup = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🎛 打开交互式控制面板", callback_data="menu:home")]]
        )
        await self._safe_reply_html(msg, self._build_help_text(), reply_markup=markup)

    async def cmd_new(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._authorize(update):
            return
        msg = update.effective_message
        key = _extract_session_key(update)
        if msg is None or key is None:
            return
        if self.runner.is_running(key):
            await self.runner.cancel_turn(key)
        await self.runner.close_worker(key, graceful=True)
        session = self.store.get_or_create_session(key, self.config)
        session.conversation_id = None
        self.store.save_session(session)
        await self._safe_reply_html(
            msg,
            f"🆕 已开启新对话。\n工作区：<code>{html.escape(session.workspace)}</code>",
        )

    async def cmd_cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._authorize(update):
            return
        msg = update.effective_message
        key = _extract_session_key(update)
        if msg is None or key is None:
            return
        canceled = await self.runner.cancel_turn(key)
        if canceled:
            await self._safe_reply_html(msg, "🛑 已发送中止信号，正在停止当前任务。")
        else:
            await self._safe_reply_html(msg, "ℹ️ 当前没有正在执行的任务。")

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._authorize(update):
            return
        msg = update.effective_message
        key = _extract_session_key(update)
        if msg is None or key is None:
            return
        session = self.store.get_or_create_session(key, self.config)
        worker_state = "🟢 运行中（热进程）" if self.runner.has_live_worker(key) else "⚪ 按需启动"
        busy_state = "⚙️ 执行中" if self.runner.is_running(key) else "空闲"
        perm_desc = PERM_DESCRIPTIONS.get(session.permission_mode, session.permission_mode)
        cid = session.conversation_id or "（尚未开始）"

        text = (
            "📊 <b>AGY 会话状态</b>\n"
            f"• 状态：{busy_state} | 后台进程：{worker_state}\n"
            f"• 工作区：<code>{html.escape(session.workspace)}</code>\n"
            f"• 对话 ID：<code>{html.escape(cid)}</code>\n"
            f"• 权限模式：<code>{html.escape(session.permission_mode)}</code> — {html.escape(perm_desc)}\n"
            f"• 终端沙箱：{'✅ 开启 (--sandbox)' if session.sandbox else '❌ 关闭'}\n"
            f"• 模型：<code>{html.escape(session.model or 'CLI 默认')}</code>\n"
            f"• 推理强度：<code>{html.escape(session.effort or 'CLI 默认')}</code>\n"
            f"• Agent：<code>{html.escape(session.agent or '默认')}</code>\n"
            f"• 引擎模式：<code>{html.escape(session.runner_mode)}</code>"
            f"（全局热进程数：{self.runner.active_worker_count()}）\n"
            f"• 详细输出：{'✅ 开启' if session.verbose else '❌ 简洁'}"
        )
        await self._safe_reply_html(msg, text)

    async def cmd_cd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._authorize(update):
            return
        msg = update.effective_message
        key = _extract_session_key(update)
        if msg is None or key is None:
            return
        session = self.store.get_or_create_session(key, self.config)

        raw_arg = " ".join(context.args).strip() if context.args else ""
        if not raw_arg:
            await self._send_cd_browser(msg, key, session)
            return

        target = self._resolve_user_path(session.workspace, raw_arg)
        await self._apply_cd(msg, key, session, target)

    async def _send_cd_browser(
        self,
        msg: Message,
        key: SessionKey,
        session: SessionState,
        *,
        edit: bool = False,
        include_back: bool = True,
    ) -> None:
        cwd = Path(session.workspace)
        choices: list[tuple[str, str]] = []
        parent = cwd.parent
        if parent != cwd and self.config.is_path_allowed(parent):
            choices.append(("⬆️ .. (上级目录)", str(parent)))

        try:
            subdirs = sorted(
                (p for p in cwd.iterdir() if p.is_dir() and not p.name.startswith(".")),
                key=lambda p: p.name.lower(),
            )[:12]
        except OSError:
            subdirs = []

        for sub in subdirs:
            if self.config.is_path_allowed(sub):
                choices.append((f"📁 {sub.name}", str(sub)))

        self._cd_cache[key] = [path_str for _, path_str in choices]
        rows: list[list[InlineKeyboardButton]] = []
        for idx, (label, _) in enumerate(choices):
            btn = InlineKeyboardButton(label[:32], callback_data=f"cd:{idx}")
            if idx == 0 and label.startswith("⬆️"):
                rows.append([btn])
            elif rows and len(rows[-1]) < 2 and not rows[-1][0].text.startswith("⬆️"):
                rows[-1].append(btn)
            else:
                rows.append([btn])

        if include_back:
            rows.append(self._back_to_panel_row())

        markup = InlineKeyboardMarkup(rows) if rows else None
        text = (
            f"📂 当前工作目录：<code>{html.escape(session.workspace)}</code>\n"
            "点击下方目录快速切换，或输入 <code>/cd &lt;路径&gt;</code>。"
        )
        if edit:
            await self._safe_edit_html(msg, text, reply_markup=markup)
        else:
            await self._safe_reply_html(msg, text, reply_markup=markup)

    async def _apply_cd(
        self,
        msg: Message,
        key: SessionKey,
        session: SessionState,
        target: Path,
        *,
        edit: bool = False,
    ) -> None:
        back_markup = InlineKeyboardMarkup([self._back_to_panel_row()]) if edit else None
        if not self.config.is_path_allowed(target):
            err = f"⛔ 目标路径不在允许的根目录范围内：<code>{html.escape(str(target))}</code>"
            if edit:
                await self._safe_edit_html(msg, err, reply_markup=back_markup)
            else:
                await self._safe_reply_html(msg, err)
            return

        if not target.exists() or not target.is_dir():
            err = f"❌ 目录不存在：<code>{html.escape(str(target))}</code>"
            if edit:
                await self._safe_edit_html(msg, err, reply_markup=back_markup)
            else:
                await self._safe_reply_html(msg, err)
            return

        resolved = str(target.resolve())
        if resolved != session.workspace:
            await self.runner.close_worker(key, graceful=True)
            session.workspace = resolved
            # Switching workspace starts a fresh conversation context cleanly
            session.conversation_id = None
            self.store.save_session(session)

        if edit:
            # Refresh directory browser in-place so the user can keep navigating effortlessly
            await self._send_cd_browser(msg, key, session, edit=True, include_back=True)
            return

        reply = (
            f"📂 已切换工作区至：\n<code>{html.escape(session.workspace)}</code>\n"
            "<i>已重置对话上下文，下一条消息将在新目录启动 agy。</i>"
        )
        await self._safe_reply_html(msg, reply)

    async def cmd_ls(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._authorize(update):
            return
        msg = update.effective_message
        key = _extract_session_key(update)
        if msg is None or key is None:
            return
        session = self.store.get_or_create_session(key, self.config)
        raw_arg = " ".join(context.args).strip() if context.args else ""
        target = self._resolve_user_path(session.workspace, raw_arg) if raw_arg else Path(session.workspace)

        if not self.config.is_path_allowed(target):
            await self._safe_reply_html(msg, "⛔ 路径超出允许的工作区根目录范围。")
            return
        if not target.is_dir():
            await self._safe_reply_html(msg, f"❌ 目录不存在：<code>{html.escape(str(target))}</code>")
            return

        try:
            entries = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError as exc:
            await self._safe_reply_html(msg, f"❌ 读取目录失败：{html.escape(str(exc))}")
            return

        lines: list[str] = [f"📁 <b>{html.escape(str(target))}</b> ({len(entries)} 项)"]
        for entry in entries[:60]:
            if entry.is_dir():
                lines.append(f"  📂 <code>{html.escape(entry.name)}/</code>")
            else:
                try:
                    size = entry.stat().st_size
                    size_str = _human_size(size)
                except OSError:
                    size_str = "?"
                lines.append(f"  📄 <code>{html.escape(entry.name)}</code> ({size_str})")
        if len(entries) > 60:
            lines.append(f"<i>… 其余 {len(entries) - 60} 项未显示</i>")

        await self._safe_reply_html(msg, "\n".join(lines))

    async def cmd_get(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._authorize(update):
            return
        msg = update.effective_message
        key = _extract_session_key(update)
        if msg is None or key is None:
            return
        raw_arg = " ".join(context.args).strip() if context.args else ""
        if not raw_arg:
            await self._safe_reply_html(msg, "用法：<code>/get &lt;文件路径&gt;</code>（支持相对当前工作区路径）")
            return

        session = self.store.get_or_create_session(key, self.config)
        target = self._resolve_user_path(session.workspace, raw_arg)
        if not self.config.is_path_allowed(target):
            await self._safe_reply_html(msg, "⛔ 该文件不在允许访问的根目录范围内。")
            return
        if not target.exists() or not target.is_file():
            await self._safe_reply_html(msg, f"❌ 文件不存在：<code>{html.escape(str(target))}</code>")
            return

        size = target.stat().st_size
        if size > 50 * 1024 * 1024:
            await self._safe_reply_html(
                msg, f"❌ 文件过大（{_human_size(size)}），超出 Telegram Bot API 50MB 限制。"
            )
            return

        with target.open("rb") as fh:
            await msg.reply_document(
                document=fh,
                filename=target.name,
                caption=f"📎 {target}",
            )

    async def cmd_perm(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._authorize(update):
            return
        msg = update.effective_message
        key = _extract_session_key(update)
        if msg is None or key is None:
            return
        session = self.store.get_or_create_session(key, self.config)

        arg = context.args[0].strip().lower() if context.args else ""
        if not arg:
            markup = self._build_perm_keyboard(session.permission_mode)
            cur_desc = PERM_DESCRIPTIONS.get(session.permission_mode, session.permission_mode)
            await self._safe_reply_html(
                msg,
                f"🔐 当前权限模式：<b>{html.escape(session.permission_mode)}</b>\n"
                f"{html.escape(cur_desc)}\n\n请选择新的权限模式：",
                reply_markup=markup,
            )
            return

        if arg not in VALID_PERMISSION_MODES:
            await self._safe_reply_html(
                msg,
                f"❌ 无效权限模式。可选：<code>{' | '.join(VALID_PERMISSION_MODES)}</code>",
            )
            return

        if arg == "danger":
            if not self.config.allow_dangerous_mode:
                await self._safe_reply_html(
                    msg,
                    "⛔ 服务器配置已禁用全自动权限模式（<code>ALLOW_DANGEROUS_MODE=false</code>）。",
                )
                return
            await self._send_danger_confirm(msg, edit=False)
            return

        await self._set_permission_mode(msg, key, session, arg, edit=False)

    def _build_perm_keyboard(
        self, current: str, *, include_back: bool = True
    ) -> InlineKeyboardMarkup:
        rows: list[list[InlineKeyboardButton]] = []
        for mode in VALID_PERMISSION_MODES:
            if mode == "danger" and not self.config.allow_dangerous_mode:
                continue
            mark = "✅ " if mode == current else ""
            label = f"{mark}{mode}"
            if mode == "danger":
                label = f"{mark}⚠️ danger (skip-permissions)"
            rows.append([InlineKeyboardButton(label, callback_data=f"perm:{mode}")])
        if include_back:
            rows.append(self._back_to_panel_row())
        return InlineKeyboardMarkup(rows)

    async def _send_danger_confirm(self, msg: Message, *, edit: bool) -> None:
        text = (
            "⚠️ <b>确认开启全自动权限模式（danger）？</b>\n"
            "开启后将向 <code>agy</code> 传递 <code>--dangerously-skip-permissions</code>，"
            "所有文件写入与 Shell 命令都将自动批准执行，不再提示确认。"
        )
        markup = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("⚠️ 确认开启 Danger", callback_data="perm_confirm:danger"),
                    InlineKeyboardButton("取消", callback_data="perm_cancel"),
                ]
            ]
        )
        if edit:
            await self._safe_edit_html(msg, text, reply_markup=markup)
        else:
            await self._safe_reply_html(msg, text, reply_markup=markup)

    async def _set_permission_mode(
        self,
        msg: Message,
        key: SessionKey,
        session: SessionState,
        mode: str,
        *,
        edit: bool,
    ) -> None:
        if session.permission_mode != mode:
            session.permission_mode = mode
            self.store.save_session(session)
            # Restart worker on next turn so new flag takes effect while keeping conversation_id
            await self.runner.close_worker(key, graceful=True)

        desc = PERM_DESCRIPTIONS.get(mode, mode)
        text = (
            f"🔐 已切换权限模式为：<b>{html.escape(mode)}</b>\n"
            f"{html.escape(desc)}\n"
            "<i>对话上下文已保留，下一条消息将按新权限模式生效。</i>"
        )
        if edit:
            await self._safe_edit_html(
                msg, text, reply_markup=InlineKeyboardMarkup([self._back_to_panel_row()])
            )
        else:
            await self._safe_reply_html(msg, text)

    async def cmd_sandbox(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._authorize(update):
            return
        msg = update.effective_message
        key = _extract_session_key(update)
        if msg is None or key is None:
            return
        session = self.store.get_or_create_session(key, self.config)
        arg = context.args[0].strip().lower() if context.args else ""

        if not arg:
            markup = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            ("✅ " if session.sandbox else "") + "开启 (--sandbox)",
                            callback_data="sandbox:on",
                        ),
                        InlineKeyboardButton(
                            ("✅ " if not session.sandbox else "") + "关闭",
                            callback_data="sandbox:off",
                        ),
                    ]
                ]
            )
            await self._safe_reply_html(
                msg,
                f"🛡 当前终端沙箱：<b>{'开启' if session.sandbox else '关闭'}</b>",
                reply_markup=markup,
            )
            return

        if arg in {"on", "true", "1", "yes"}:
            new_val = True
        elif arg in {"off", "false", "0", "no"}:
            new_val = False
        else:
            await self._safe_reply_html(msg, "用法：<code>/sandbox on</code> 或 <code>/sandbox off</code>")
            return

        await self._set_sandbox(msg, key, session, new_val, edit=False)

    async def _set_sandbox(
        self, msg: Message, key: SessionKey, session: SessionState, enabled: bool, *, edit: bool
    ) -> None:
        if session.sandbox != enabled:
            session.sandbox = enabled
            self.store.save_session(session)
            await self.runner.close_worker(key, graceful=True)
        text = f"🛡 终端沙箱已{'开启 (--sandbox)' if enabled else '关闭'}。"
        if edit:
            await self._safe_edit_html(msg, text)
        else:
            await self._safe_reply_html(msg, text)

    async def cmd_runner(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._authorize(update):
            return
        msg = update.effective_message
        key = _extract_session_key(update)
        if msg is None or key is None:
            return
        session = self.store.get_or_create_session(key, self.config)
        arg = context.args[0].strip().lower() if context.args else ""
        if not arg:
            await self._safe_reply_html(
                msg,
                f"⚙️ 当前引擎模式：<b>{html.escape(session.runner_mode)}</b>\n"
                "• <code>persistent</code> — 常驻 stream-json 进程，连续对话低延迟\n"
                "• <code>oneshot</code> — 每轮按需启动 <code>agy -p</code>，空闲零内存\n\n"
                "用法：<code>/runner persistent</code> 或 <code>/runner oneshot</code>",
            )
            return
        if arg not in VALID_RUNNER_MODES:
            await self._safe_reply_html(msg, "可选模式：<code>persistent</code> | <code>oneshot</code>")
            return
        if session.runner_mode != arg:
            session.runner_mode = arg
            self.store.save_session(session)
            await self.runner.close_worker(key, graceful=True)
        await self._safe_reply_html(msg, f"⚙️ 已切换引擎模式为：<b>{html.escape(arg)}</b>")

    async def cmd_model(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._authorize(update):
            return
        msg = update.effective_message
        key = _extract_session_key(update)
        if msg is None or key is None:
            return
        session = self.store.get_or_create_session(key, self.config)

        if context.args:
            chosen = context.args[0].strip()
            new_model = None if chosen.lower() in {"default", "reset", "auto", "none"} else chosen
            await self._set_model(msg, key, session, new_model, edit=False)
            return

        status_msg = await self._safe_reply_html(msg, "🔍 正在查询 <code>agy models</code>…")
        await self._open_model_picker(status_msg, key, session, include_back=True)

    async def _open_model_picker(
        self,
        target_msg: Message,
        key: SessionKey,
        session: SessionState,
        *,
        include_back: bool = True,
    ) -> None:
        models, err = await self.runner.list_models(cwd=session.workspace)
        back_markup = InlineKeyboardMarkup([self._back_to_panel_row()]) if include_back else None
        if err:
            await self._safe_edit_html(
                target_msg,
                f"❌ 获取模型列表失败：\n<pre>{html.escape(err[:1500])}</pre>\n"
                "你仍可直接手动设置：<code>/model &lt;slug&gt;</code> 或 <code>/model default</code>",
                reply_markup=back_markup,
            )
            return

        self._model_cache[key] = models
        rows: list[list[InlineKeyboardButton]] = [
            [
                InlineKeyboardButton(
                    ("✅ " if session.model is None else "") + "CLI 默认模型",
                    callback_data="model:default",
                )
            ]
        ]
        for idx, (slug, label) in enumerate(models[:20]):
            mark = "✅ " if session.model == slug else ""
            rows.append([InlineKeyboardButton(f"{mark}{label}"[:48], callback_data=f"model:{idx}")])
        if include_back:
            rows.append(self._back_to_panel_row())

        cur = session.model or "CLI 默认"
        await self._safe_edit_html(
            target_msg,
            f"🧠 当前模型：<code>{html.escape(cur)}</code>\n点击下方按钮切换，或执行 <code>/model &lt;slug&gt;</code>：",
            reply_markup=InlineKeyboardMarkup(rows),
        )

    async def _set_model(
        self,
        msg: Message,
        key: SessionKey,
        session: SessionState,
        model: str | None,
        *,
        edit: bool,
    ) -> None:
        if session.model != model:
            session.model = model
            self.store.save_session(session)
            await self.runner.close_worker(key, graceful=True)
        label = model or "CLI 默认"
        text = f"🧠 已设置模型为：<code>{html.escape(label)}</code>"
        if edit:
            await self._safe_edit_html(
                msg, text, reply_markup=InlineKeyboardMarkup([self._back_to_panel_row()])
            )
        else:
            await self._safe_reply_html(msg, text)

    def _build_effort_keyboard(
        self, current: str | None, *, include_back: bool = True
    ) -> InlineKeyboardMarkup:
        buttons = [
            InlineKeyboardButton(
                ("✅ " if current is None else "") + "default",
                callback_data="effort:default",
            )
        ] + [
            InlineKeyboardButton(
                ("✅ " if current == lvl else "") + lvl,
                callback_data=f"effort:{lvl}",
            )
            for lvl in VALID_EFFORT_LEVELS
        ]
        rows = [buttons[:3], buttons[3:]]
        if include_back:
            rows.append(self._back_to_panel_row())
        return InlineKeyboardMarkup(rows)

    async def cmd_effort(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._authorize(update):
            return
        msg = update.effective_message
        key = _extract_session_key(update)
        if msg is None or key is None:
            return
        session = self.store.get_or_create_session(key, self.config)

        if not context.args:
            await self._safe_reply_html(
                msg,
                f"⚡ 当前推理强度：<code>{html.escape(session.effort or 'CLI 默认')}</code>",
                reply_markup=self._build_effort_keyboard(session.effort, include_back=True),
            )
            return

        arg = context.args[0].strip().lower()
        if arg in {"default", "reset", "none"}:
            new_effort = None
        elif arg in VALID_EFFORT_LEVELS:
            new_effort = arg
        else:
            await self._safe_reply_html(
                msg,
                f"❌ 无效推理强度。可选：<code>default | {' | '.join(VALID_EFFORT_LEVELS)}</code>",
            )
            return

        await self._set_effort(msg, key, session, new_effort, edit=False)

    async def _set_effort(
        self,
        msg: Message,
        key: SessionKey,
        session: SessionState,
        effort: str | None,
        *,
        edit: bool,
    ) -> None:
        if session.effort != effort:
            session.effort = effort
            self.store.save_session(session)
            await self.runner.close_worker(key, graceful=True)
        text = f"⚡ 推理强度已设为：<code>{html.escape(effort or 'CLI 默认')}</code>"
        if edit:
            await self._safe_edit_html(
                msg, text, reply_markup=InlineKeyboardMarkup([self._back_to_panel_row()])
            )
        else:
            await self._safe_reply_html(msg, text)

    async def cmd_agent(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._authorize(update):
            return
        msg = update.effective_message
        key = _extract_session_key(update)
        if msg is None or key is None:
            return
        session = self.store.get_or_create_session(key, self.config)

        if context.args:
            chosen = context.args[0].strip()
            new_agent = None if chosen.lower() in {"default", "reset", "none"} else chosen
            if session.agent != new_agent:
                session.agent = new_agent
                self.store.save_session(session)
                await self.runner.close_worker(key, graceful=True)
            await self._safe_reply_html(
                msg, f"🤖 Agent 已设为：<code>{html.escape(new_agent or '默认')}</code>"
            )
            return

        out, err = await self.runner.list_agents(cwd=session.workspace)
        if err:
            await self._safe_reply_html(
                msg,
                f"🤖 当前 Agent：<code>{html.escape(session.agent or '默认')}</code>\n"
                f"获取列表失败：<pre>{html.escape(err[:1000])}</pre>",
            )
            return
        body = html.escape(out[:2500] or "（未发现自定义 Agent）")
        await self._safe_reply_html(
            msg,
            f"🤖 当前 Agent：<code>{html.escape(session.agent or '默认')}</code>\n"
            f"<pre>{body}</pre>\n使用 <code>/agent &lt;name&gt;</code> 或 <code>/agent default</code> 切换。",
        )

    async def cmd_usage(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._authorize(update):
            return
        msg = update.effective_message
        key = _extract_session_key(update)
        if msg is None or key is None:
            return
        session = self.store.get_or_create_session(key, self.config)
        status_msg = await self._safe_reply_html(msg, "📊 正在查询 <code>agy -p /usage</code>…")
        await self._open_usage_view(status_msg, session, include_back=True)

    async def _open_usage_view(
        self, target_msg: Message, session: SessionState, *, include_back: bool = True
    ) -> None:
        back_markup = InlineKeyboardMarkup([self._back_to_panel_row()]) if include_back else None
        report, err = await self.runner.get_usage_report(cwd=session.workspace)
        if err:
            await self._safe_edit_html(
                target_msg,
                f"❌ 查询用量失败：\n<pre>{html.escape(err[:2000])}</pre>",
                reply_markup=back_markup,
            )
            return
        await self._safe_edit_html(
            target_msg,
            f"📊 <b>AGY 配额与用量</b>\n<pre>{html.escape(report[:3500])}</pre>",
            reply_markup=back_markup,
        )

    def _build_history_view(
        self, key: SessionKey, session: SessionState, *, include_back: bool = True
    ) -> tuple[str, InlineKeyboardMarkup | None]:
        records = self.store.list_conversations(key, limit=8)
        if not records:
            markup = InlineKeyboardMarkup([self._back_to_panel_row()]) if include_back else None
            return "📜 暂无历史会话记录。", markup

        lines = ["📜 <b>最近会话记录</b>（点击按钮直接恢复）："]
        rows: list[list[InlineKeyboardButton]] = []
        for rec in records:
            active_mark = "🟢 " if rec.conversation_id == session.conversation_id else ""
            short_id = rec.conversation_id[:8]
            ts = _format_timestamp(rec.updated_at)
            tok = format_token_count(rec.total_tokens) if rec.total_tokens else "-"
            lines.append(
                f"• {active_mark}<code>{html.escape(short_id)}</code> "
                f"({ts}, {rec.num_turns} 轮, {tok} tok)\n"
                f"  {html.escape(rec.title[:64])}"
            )
            btn_label = f"{'✅ ' if active_mark else ''}{short_id} · {rec.title[:24]}"
            rows.append(
                [InlineKeyboardButton(btn_label, callback_data=f"resume:{rec.conversation_id[:36]}")]
            )
        if include_back:
            rows.append(self._back_to_panel_row())
        return "\n".join(lines), InlineKeyboardMarkup(rows)

    async def cmd_history(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._authorize(update):
            return
        msg = update.effective_message
        key = _extract_session_key(update)
        if msg is None or key is None:
            return
        session = self.store.get_or_create_session(key, self.config)
        text, markup = self._build_history_view(key, session, include_back=True)
        await self._safe_reply_html(msg, text, reply_markup=markup)

    async def cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._authorize(update):
            return
        msg = update.effective_message
        key = _extract_session_key(update)
        if msg is None or key is None:
            return
        if not context.args:
            await self.cmd_history(update, context)
            return
        cid_input = context.args[0].strip()
        await self._apply_resume(msg, key, cid_input, edit=False)

    async def _apply_resume(
        self, msg: Message, key: SessionKey, cid_input: str, *, edit: bool
    ) -> None:
        session = self.store.get_or_create_session(key, self.config)
        rec = self.store.get_conversation(cid_input)
        target_cid = rec.conversation_id if rec else cid_input

        await self.runner.close_worker(key, graceful=True)
        session.conversation_id = target_cid
        if rec and Path(rec.workspace).is_dir() and self.config.is_path_allowed(Path(rec.workspace)):
            session.workspace = rec.workspace
        self.store.save_session(session)

        title_line = f"\n摘要：{html.escape(rec.title)}" if rec else ""
        text = (
            f"🔄 已切换至历史会话：<code>{html.escape(target_cid)}</code>{title_line}\n"
            f"工作区：<code>{html.escape(session.workspace)}</code>\n"
            "下一条消息将通过 <code>--conversation</code> 自动续接上下文。"
        )
        if edit:
            await self._safe_edit_html(
                msg, text, reply_markup=InlineKeyboardMarkup([self._back_to_panel_row()])
            )
        else:
            await self._safe_reply_html(msg, text)

    async def cmd_verbose(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._authorize(update):
            return
        msg = update.effective_message
        key = _extract_session_key(update)
        if msg is None or key is None:
            return
        session = self.store.get_or_create_session(key, self.config)
        if context.args:
            arg = context.args[0].strip().lower()
            if arg in {"on", "true", "1", "yes"}:
                session.verbose = True
            elif arg in {"off", "false", "0", "no"}:
                session.verbose = False
            else:
                await self._safe_reply_html(msg, "用法：<code>/verbose on</code> 或 <code>/verbose off</code>")
                return
        else:
            session.verbose = not session.verbose
        self.store.save_session(session)
        await self._safe_reply_html(
            msg, f"📝 详细工具轨迹与 Token 摘要已{'开启' if session.verbose else '关闭'}。"
        )

    async def cmd_restart(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._authorize(update):
            return
        msg = update.effective_message
        key = _extract_session_key(update)
        if msg is None or key is None:
            return
        if self.runner.is_running(key):
            await self.runner.cancel_turn(key)
        await self.runner.close_worker(key, graceful=True)
        session = self.store.get_or_create_session(key, self.config)
        cid_hint = (
            f"对话 ID <code>{html.escape(session.conversation_id[:8])}</code> 已保留。"
            if session.conversation_id
            else "当前无活跃对话 ID。"
        )
        await self._safe_reply_html(msg, f"♻️ 已重启后台 agy 进程。{cid_hint}")

    async def cmd_pass(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._authorize(update):
            return
        msg = update.effective_message
        key = _extract_session_key(update)
        if msg is None or key is None or not msg.text:
            return
        parts = msg.text.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            await self._safe_reply_html(
                msg, "用法：<code>/pass /skill-name 参数...</code> 或 <code>/pass 任意文本</code>"
            )
            return
        await self._execute_prompt(msg, key, parts[1].strip())

    async def cmd_passthrough(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Forward safe AGY slash commands like /goal, /plan, /compact directly to `agy`."""
        if not await self._authorize(update):
            return
        msg = update.effective_message
        key = _extract_session_key(update)
        if msg is None or key is None or not msg.text:
            return
        # Strip optional @botusername suffix from the command token, normalize /grill_me -> /grill-me
        raw = msg.text.strip()
        head, *rest = raw.split(maxsplit=1)
        cmd_clean = head.split("@", 1)[0].replace("_", "-")
        prompt = f"{cmd_clean} {rest[0]}".strip() if rest else cmd_clean
        await self._execute_prompt(msg, key, prompt)

    # ------------------------------------------------------------------
    # Inline Keyboard callback handler
    # ------------------------------------------------------------------

    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._authorize(update):
            return
        query = update.callback_query
        if query is None or query.data is None:
            return

        msg = query.message
        if not isinstance(msg, Message):
            with contextlib.suppress(TelegramError):
                await query.answer()
            return
        key = _extract_session_key(update)
        if key is None:
            with contextlib.suppress(TelegramError):
                await query.answer()
            return

        data = query.data
        if data == "cancel_run":
            canceled = await self.runner.cancel_turn(key)
            with contextlib.suppress(TelegramError):
                await query.answer(
                    "🛑 正在中止任务…" if canceled else "ℹ️ 当前无运行中的任务",
                    show_alert=False,
                )
            return

        session = self.store.get_or_create_session(key, self.config)

        # Interactive Control Panel callbacks (`menu:*`)
        if data.startswith("menu:"):
            await self._handle_menu_callback(query, msg, key, session, data.split(":", 1)[1])
            return

        with contextlib.suppress(TelegramError):
            await query.answer()

        if data.startswith("perm:"):
            mode = data.split(":", 1)[1]
            if mode == "danger":
                if not self.config.allow_dangerous_mode:
                    await self._safe_edit_html(
                        msg,
                        "⛔ 服务器配置已禁用 danger 模式。",
                        reply_markup=InlineKeyboardMarkup([self._back_to_panel_row()]),
                    )
                    return
                await self._send_danger_confirm(msg, edit=True)
                return
            if mode in VALID_PERMISSION_MODES:
                await self._set_permission_mode(msg, key, session, mode, edit=True)
            return

        if data == "perm_confirm:danger":
            if self.config.allow_dangerous_mode:
                await self._set_permission_mode(msg, key, session, "danger", edit=True)
            else:
                await self._safe_edit_html(
                    msg,
                    "⛔ 服务器配置已禁用 danger 模式。",
                    reply_markup=InlineKeyboardMarkup([self._back_to_panel_row()]),
                )
            return

        if data == "perm_cancel":
            await self._safe_edit_html(
                msg,
                f"已取消切换。当前权限模式仍为：<b>{html.escape(session.permission_mode)}</b>",
                reply_markup=InlineKeyboardMarkup([self._back_to_panel_row()]),
            )
            return

        if data.startswith("sandbox:"):
            val = data.split(":", 1)[1] == "on"
            await self._set_sandbox(msg, key, session, val, edit=True)
            return

        if data.startswith("effort:"):
            raw = data.split(":", 1)[1]
            lvl = None if raw == "default" else raw
            if lvl is None or lvl in VALID_EFFORT_LEVELS:
                await self._set_effort(msg, key, session, lvl, edit=True)
            return

        if data.startswith("model:"):
            raw = data.split(":", 1)[1]
            if raw == "default":
                await self._set_model(msg, key, session, None, edit=True)
                return
            try:
                idx = int(raw)
                cached = self._model_cache.get(key) or []
                slug = cached[idx][0]
            except (ValueError, IndexError):
                await self._safe_edit_html(
                    msg,
                    "⚠️ 模型列表已过期，请点击下方返回或重新发送 /model。",
                    reply_markup=InlineKeyboardMarkup([self._back_to_panel_row()]),
                )
                return
            await self._set_model(msg, key, session, slug, edit=True)
            return

        if data.startswith("resume:"):
            cid = data.split(":", 1)[1]
            await self._apply_resume(msg, key, cid, edit=True)
            return

        if data.startswith("cd:"):
            try:
                idx = int(data.split(":", 1)[1])
                path_str = (self._cd_cache.get(key) or [])[idx]
            except (ValueError, IndexError):
                await self._safe_edit_html(
                    msg,
                    "⚠️ 目录列表已过期，请点击下方返回或重新发送 /cd。",
                    reply_markup=InlineKeyboardMarkup([self._back_to_panel_row()]),
                )
                return
            await self._apply_cd(msg, key, session, Path(path_str), edit=True)
            return

    async def _handle_menu_callback(
        self,
        query: Any,
        msg: Message,
        key: SessionKey,
        session: SessionState,
        action: str,
    ) -> None:
        if action == "home":
            with contextlib.suppress(TelegramError):
                await query.answer("已刷新状态")
            text, markup = self._build_panel_view(key, session)
            await self._safe_edit_html(msg, text, reply_markup=markup)
            return

        if action == "new":
            if self.runner.is_running(key):
                await self.runner.cancel_turn(key)
            await self.runner.close_worker(key, graceful=True)
            session.conversation_id = None
            self.store.save_session(session)
            with contextlib.suppress(TelegramError):
                await query.answer("🆕 已开启全新对话")
            text, markup = self._build_panel_view(key, session)
            await self._safe_edit_html(msg, text, reply_markup=markup)
            return

        if action == "restart":
            if self.runner.is_running(key):
                await self.runner.cancel_turn(key)
            await self.runner.close_worker(key, graceful=True)
            with contextlib.suppress(TelegramError):
                await query.answer("♻️ 已重启后台进程（保留上下文）")
            text, markup = self._build_panel_view(key, session)
            await self._safe_edit_html(msg, text, reply_markup=markup)
            return

        if action == "toggle_sandbox":
            session.sandbox = not session.sandbox
            self.store.save_session(session)
            await self.runner.close_worker(key, graceful=True)
            with contextlib.suppress(TelegramError):
                await query.answer(f"🛡 终端沙箱已{'开启' if session.sandbox else '关闭'}")
            text, markup = self._build_panel_view(key, session)
            await self._safe_edit_html(msg, text, reply_markup=markup)
            return

        if action == "toggle_runner":
            session.runner_mode = "oneshot" if session.runner_mode == "persistent" else "persistent"
            self.store.save_session(session)
            await self.runner.close_worker(key, graceful=True)
            with contextlib.suppress(TelegramError):
                await query.answer(f"⚙️ 已切换引擎为 {session.runner_mode}")
            text, markup = self._build_panel_view(key, session)
            await self._safe_edit_html(msg, text, reply_markup=markup)
            return

        if action == "toggle_verbose":
            session.verbose = not session.verbose
            self.store.save_session(session)
            with contextlib.suppress(TelegramError):
                await query.answer(f"📝 详细轨迹已{'开启' if session.verbose else '关闭'}")
            text, markup = self._build_panel_view(key, session)
            await self._safe_edit_html(msg, text, reply_markup=markup)
            return

        if action == "perm":
            with contextlib.suppress(TelegramError):
                await query.answer()
            cur_desc = PERM_DESCRIPTIONS.get(session.permission_mode, session.permission_mode)
            await self._safe_edit_html(
                msg,
                f"🔐 当前权限模式：<b>{html.escape(session.permission_mode)}</b>\n"
                f"{html.escape(cur_desc)}\n\n请选择新的权限模式：",
                reply_markup=self._build_perm_keyboard(session.permission_mode, include_back=True),
            )
            return

        if action == "model":
            with contextlib.suppress(TelegramError):
                await query.answer("正在查询可用模型…")
            await self._safe_edit_html(msg, "🔍 正在查询 <code>agy models</code>…")
            await self._open_model_picker(msg, key, session, include_back=True)
            return

        if action == "effort":
            with contextlib.suppress(TelegramError):
                await query.answer()
            await self._safe_edit_html(
                msg,
                f"⚡ 当前推理强度：<code>{html.escape(session.effort or 'CLI 默认')}</code>\n请选择：",
                reply_markup=self._build_effort_keyboard(session.effort, include_back=True),
            )
            return

        if action == "cd":
            with contextlib.suppress(TelegramError):
                await query.answer()
            await self._send_cd_browser(msg, key, session, edit=True, include_back=True)
            return

        if action == "history":
            with contextlib.suppress(TelegramError):
                await query.answer()
            text, markup = self._build_history_view(key, session, include_back=True)
            await self._safe_edit_html(msg, text, reply_markup=markup)
            return

        if action == "usage":
            with contextlib.suppress(TelegramError):
                await query.answer("正在查询用量…")
            await self._safe_edit_html(msg, "📊 正在查询 <code>agy -p /usage</code>…")
            await self._open_usage_view(msg, session, include_back=True)
            return

        if action == "help":
            with contextlib.suppress(TelegramError):
                await query.answer()
            await self._safe_edit_html(
                msg,
                self._build_help_text(),
                reply_markup=InlineKeyboardMarkup([self._back_to_panel_row()]),
            )
            return

        if action == "close":
            with contextlib.suppress(TelegramError):
                await query.answer("面板已关闭")
            try:
                await msg.delete()
            except TelegramError:
                await self._safe_edit_html(
                    msg, "🎛 控制面板已关闭（发送 /start 或 /menu 可随时重新打开）。"
                )
            return

        with contextlib.suppress(TelegramError):
            await query.answer()

    # ------------------------------------------------------------------
    # Message & Media handlers
    # ------------------------------------------------------------------

    async def handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._authorize(update):
            return
        msg = update.effective_message
        key = _extract_session_key(update)
        if msg is None or key is None or not msg.text:
            return
        prompt = msg.text.strip()
        if not prompt:
            return
        await self._execute_prompt(msg, key, prompt)

    async def handle_media(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Download Telegram photo/document/audio/voice into <workspace>/.tg_inbox/ for AGY."""
        if not await self._authorize(update):
            return
        msg = update.effective_message
        key = _extract_session_key(update)
        if msg is None or key is None:
            return

        session = self.store.get_or_create_session(key, self.config)
        ws = Path(session.workspace)
        if not ws.is_dir():
            ws = Path(self.config.default_workspace)
            ws.mkdir(parents=True, exist_ok=True)

        file_id: str | None = None
        orig_name = "upload.bin"
        mime_type = "application/octet-stream"
        file_size: int | None = None

        if msg.photo:
            photo = msg.photo[-1]
            file_id = photo.file_id
            orig_name = "photo.jpg"
            mime_type = "image/jpeg"
            file_size = photo.file_size
        elif msg.document:
            doc = msg.document
            file_id = doc.file_id
            orig_name = doc.file_name or "document.bin"
            mime_type = doc.mime_type or "application/octet-stream"
            file_size = doc.file_size
        elif msg.audio:
            aud = msg.audio
            file_id = aud.file_id
            orig_name = aud.file_name or "audio.mp3"
            mime_type = aud.mime_type or "audio/mpeg"
            file_size = aud.file_size
        elif msg.voice:
            voice = msg.voice
            file_id = voice.file_id
            orig_name = "voice.ogg"
            mime_type = voice.mime_type or "audio/ogg"
            file_size = voice.file_size

        if not file_id:
            return

        max_bytes = self.config.max_upload_mb * 1024 * 1024
        if file_size and file_size > max_bytes:
            await self._safe_reply_html(
                msg,
                f"❌ 附件过大（{_human_size(file_size)}），上限为 {self.config.max_upload_mb} MB。",
            )
            return

        inbox = ws / ".tg_inbox"
        try:
            inbox.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            safe_name = _sanitize_filename(orig_name)
            dest = inbox / f"{ts}_{safe_name}"
            tg_file = await context.bot.get_file(file_id)
            await tg_file.download_to_drive(custom_path=str(dest))
        except Exception as exc:
            await self._safe_reply_html(msg, f"❌ 下载附件失败：{html.escape(str(exc))}")
            return

        caption = (msg.caption or "").strip()
        rel_path = dest.relative_to(ws)
        size_label = _human_size(dest.stat().st_size) if dest.exists() else "?"
        prompt_header = (
            f"[用户通过 Telegram 上传了附件，已保存到工作区路径: {rel_path} "
            f"(绝对路径: {dest}, MIME: {mime_type}, 大小: {size_label})]\n"
            "请先读取该文件再回答用户。"
        )
        prompt = f"{prompt_header}\n\n{caption}" if caption else f"{prompt_header}\n\n请分析此文件内容。"
        await self._execute_prompt(msg, key, prompt)

    # ------------------------------------------------------------------
    # Core execution & streaming delivery
    # ------------------------------------------------------------------

    async def _execute_prompt(self, msg: Message, key: SessionKey, prompt: str) -> None:
        lock = self._get_lock(key)
        if lock.locked():
            await self._safe_reply_html(
                msg, "📥 当前任务执行中，已加入队列（发送 /cancel 可中止当前任务）。"
            )

        async with lock:
            session = self.store.get_or_create_session(key, self.config)
            cancel_kb = InlineKeyboardMarkup(
                [[InlineKeyboardButton("🛑 中止任务", callback_data="cancel_run")]]
            )
            status_msg = await self._safe_reply_html(
                msg, "⏳ <b>AGY 思考中…</b>", reply_markup=cancel_kb
            )

            typing_stop = asyncio.Event()
            typing_task = asyncio.create_task(
                self._typing_heartbeat(msg, typing_stop),
                name=f"tg-typing-{key.label}",
            )

            edit_interval = self.config.stream_edit_interval_seconds
            last_edit_at = 0.0
            last_rendered = ""
            backoff_until = 0.0
            edit_lock = asyncio.Lock()

            async def _on_progress(progress: TurnProgress) -> None:
                nonlocal last_edit_at, last_rendered, backoff_until
                now = time.monotonic()
                if now < backoff_until or (now - last_edit_at) < edit_interval:
                    return
                if edit_lock.locked():
                    return
                async with edit_lock:
                    now = time.monotonic()
                    if now < backoff_until or (now - last_edit_at) < edit_interval:
                        return
                    rendered = render_streaming_preview(progress, verbose=session.verbose)
                    if rendered == last_rendered:
                        return
                    try:
                        await status_msg.edit_text(
                            rendered,
                            parse_mode=ParseMode.HTML,
                            reply_markup=cancel_kb,
                            disable_web_page_preview=True,
                        )
                        last_edit_at = time.monotonic()
                        last_rendered = rendered
                    except RetryAfter as exc:
                        wait_s = float(exc.retry_after) + 0.5
                        backoff_until = time.monotonic() + wait_s
                        logger.warning("Telegram edit rate-limited; backing off %.1fs", wait_s)
                    except BadRequest as exc:
                        if "not modified" not in str(exc).lower():
                            logger.debug("Streaming edit BadRequest: %s", exc)
                    except TelegramError as exc:
                        logger.debug("Streaming edit TelegramError: %s", exc)

            turn_started_wall = time.time()
            try:
                result = await self.runner.run_turn(session, prompt, on_progress=_on_progress)
            finally:
                typing_stop.set()
                typing_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await typing_task

            # Persist updated conversation_id and history
            if result.conversation_id:
                session.conversation_id = result.conversation_id
                self.store.save_session(session)
                self.store.record_conversation_turn(
                    conversation_id=result.conversation_id,
                    key=key,
                    workspace=session.workspace,
                    prompt=prompt,
                    num_turns=result.num_turns,
                    total_tokens=result.usage.total_tokens,
                )

            await self._deliver_result(
                msg,
                status_msg,
                session,
                result,
                turn_started_at=turn_started_wall,
            )

    async def _deliver_result(
        self,
        user_msg: Message,
        status_msg: Message,
        session: SessionState,
        result: TurnResult,
        *,
        turn_started_at: float = 0.0,
    ) -> None:
        footer_html = render_turn_footer(result, verbose=session.verbose)

        if result.status == "CANCELED":
            body = "🛑 <b>任务已取消</b>"
            if result.response.strip():
                snippet = html.escape(result.response.strip()[-1500:], quote=False)
                body += f"\n\n<i>取消前已输出内容：</i>\n{snippet}"
            await self._safe_edit_html(status_msg, body)
            return

        if not result.ok:
            title = "⏰ <b>任务超时</b>" if result.status == "TIMEOUT" else f"❌ <b>AGY 执行失败 ({html.escape(result.status)})</b>"
            err_block = (
                f"\n<pre>{html.escape((result.error or '未知错误')[:2400])}</pre>"
            )
            extra = ""
            if result.response.strip():
                extra = f"\n\n<i>部分输出：</i>\n{html.escape(result.response.strip()[-1000:])}"
            full_err = f"{title}{err_block}{extra}"
            if footer_html and len(full_err) + len(footer_html) + 2 <= TELEGRAM_MAX_MESSAGE_LEN:
                full_err = f"{full_err}\n\n{footer_html}"
            await self._safe_edit_html(status_msg, full_err)
            return

        raw_response = (result.response or "").strip()
        if not raw_response:
            empty_msg = "✅ <b>执行完成</b>（无文本回复）"
            if footer_html:
                empty_msg += f"\n\n{footer_html}"
            await self._safe_edit_html(status_msg, empty_msg)
            await self._send_turn_images(
                user_msg, session, result, turn_started_at=turn_started_at
            )
            return

        chunks = render_telegram_html_chunks(raw_response)
        if not chunks:
            await self._safe_edit_html(status_msg, "✅ 执行完成")
            await self._send_turn_images(
                user_msg, session, result, turn_started_at=turn_started_at
            )
            return

        # Deliver first chunk by editing the placeholder status message (removing cancel button)
        first_html, first_raw = chunks[0]
        footer_attached = False
        if len(chunks) == 1 and footer_html:
            if len(first_html) + len(footer_html) + 2 <= TELEGRAM_MAX_MESSAGE_LEN:
                first_html = f"{first_html}\n\n{footer_html}"
                footer_attached = True

        await self._safe_edit_html(status_msg, first_html, fallback_plain=first_raw)

        # Deliver remaining chunks as follow-up replies
        for idx, (chunk_html, chunk_raw) in enumerate(chunks[1:], start=1):
            is_last = idx == len(chunks) - 1
            text_to_send = chunk_html
            if is_last and footer_html and not footer_attached:
                if len(chunk_html) + len(footer_html) + 2 <= TELEGRAM_MAX_MESSAGE_LEN:
                    text_to_send = f"{chunk_html}\n\n{footer_html}"
                    footer_attached = True
            await self._safe_reply_html(user_msg, text_to_send, fallback_plain=chunk_raw)

        # Send footer separately if it didn't fit in the last chunk
        if footer_html and not footer_attached:
            await self._safe_reply_html(user_msg, footer_html)

        # Automatically send images generated or referenced during the turn
        await self._send_turn_images(
            user_msg, session, result, turn_started_at=turn_started_at
        )

        # Attach full Markdown document for very long outputs
        threshold = self.config.long_output_file_threshold
        if threshold > 0 and len(raw_response) >= threshold:
            buf = io.BytesIO(raw_response.encode("utf-8"))
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            buf.name = f"agy_response_{ts}.md"
            with contextlib.suppress(TelegramError):
                await user_msg.reply_document(
                    document=buf,
                    filename=buf.name,
                    caption="📄 完整 Markdown 回复附件",
                )

    async def _send_turn_images(
        self,
        user_msg: Message,
        session: SessionState,
        result: TurnResult,
        *,
        turn_started_at: float,
    ) -> None:
        if not self.config.auto_send_images:
            return
        images = collect_turn_images(
            response_text=result.response,
            tool_candidates=result.image_candidates,
            workspace=session.workspace,
            config=self.config,
            turn_started_at=turn_started_at or time.time(),
        )
        if not images:
            return

        ws_path = Path(session.workspace).expanduser().resolve()
        for img_path in images:
            try:
                caption_path = str(img_path.relative_to(ws_path))
            except ValueError:
                caption_path = img_path.name
            caption = f"🖼 {caption_path}"[:1000]
            await self._safe_send_image(user_msg, img_path, caption=caption)

    async def _safe_send_image(self, user_msg: Message, img_path: Path, *, caption: str) -> None:
        ext = img_path.suffix.lower()
        for attempt in range(2):
            try:
                if ext == ".gif":
                    try:
                        with img_path.open("rb") as fh:
                            await user_msg.reply_animation(
                                animation=fh,
                                filename=img_path.name,
                                caption=caption,
                            )
                        return
                    except TelegramError:
                        pass

                if should_send_as_photo(img_path):
                    try:
                        with img_path.open("rb") as fh:
                            await user_msg.reply_photo(photo=fh, caption=caption)
                        return
                    except RetryAfter:
                        raise
                    except TelegramError as exc:
                        logger.info(
                            "reply_photo failed for %s (%s); falling back to reply_document",
                            img_path,
                            exc,
                        )

                with img_path.open("rb") as fh:
                    await user_msg.reply_document(
                        document=fh,
                        filename=img_path.name,
                        caption=caption,
                    )
                return
            except RetryAfter as exc:
                if attempt == 0:
                    await asyncio.sleep(float(exc.retry_after) + 0.5)
                    continue
                logger.warning("Rate limited while sending image %s", img_path)
                return
            except (OSError, TelegramError) as exc:
                logger.warning("Failed to send image %s: %s", img_path, exc)
                return

    async def _typing_heartbeat(self, msg: Message, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            with contextlib.suppress(TelegramError):
                await msg.chat.send_action(ChatAction.TYPING)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=4.5)
            except asyncio.TimeoutError:
                continue

    # ------------------------------------------------------------------
    # Safe HTML send/edit helpers with plain-text fallback & RetryAfter
    # ------------------------------------------------------------------

    async def _safe_reply_html(
        self,
        msg: Message,
        html_text: str,
        *,
        fallback_plain: str | None = None,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> Message:
        for attempt in range(2):
            try:
                return await msg.reply_text(
                    html_text[:TELEGRAM_MAX_MESSAGE_LEN],
                    parse_mode=ParseMode.HTML,
                    reply_markup=reply_markup,
                    disable_web_page_preview=True,
                )
            except RetryAfter as exc:
                if attempt == 0:
                    await asyncio.sleep(float(exc.retry_after) + 0.5)
                    continue
                raise
            except BadRequest as exc:
                logger.warning("HTML reply failed (%s); falling back to plain text", exc)
                plain = (fallback_plain or _strip_html(html_text))[:TELEGRAM_MAX_MESSAGE_LEN]
                return await msg.reply_text(
                    plain,
                    reply_markup=reply_markup,
                    disable_web_page_preview=True,
                )
        return await msg.reply_text((fallback_plain or _strip_html(html_text))[:TELEGRAM_MAX_MESSAGE_LEN])

    async def _safe_edit_html(
        self,
        msg: Message,
        html_text: str,
        *,
        fallback_plain: str | None = None,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> None:
        for attempt in range(2):
            try:
                await msg.edit_text(
                    html_text[:TELEGRAM_MAX_MESSAGE_LEN],
                    parse_mode=ParseMode.HTML,
                    reply_markup=reply_markup,
                    disable_web_page_preview=True,
                )
                return
            except RetryAfter as exc:
                if attempt == 0:
                    await asyncio.sleep(float(exc.retry_after) + 0.5)
                    continue
                return
            except BadRequest as exc:
                if "not modified" in str(exc).lower():
                    return
                logger.warning("HTML edit failed (%s); falling back to plain text", exc)
                plain = (fallback_plain or _strip_html(html_text))[:TELEGRAM_MAX_MESSAGE_LEN]
                with contextlib.suppress(TelegramError):
                    await msg.edit_text(
                        plain,
                        reply_markup=reply_markup,
                        disable_web_page_preview=True,
                    )
                return
            except TelegramError as exc:
                logger.warning("Edit message failed: %s", exc)
                return

    @staticmethod
    def _resolve_user_path(current_workspace: str, raw_arg: str) -> Path:
        p = Path(raw_arg).expanduser()
        if not p.is_absolute():
            p = Path(current_workspace) / p
        return p.resolve()


def _strip_html(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def _human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024.0 or unit == "GB":
            return f"{size:.1f}{unit}" if unit != "B" else f"{int(size)}B"
        size /= 1024.0
    return f"{num_bytes}B"
