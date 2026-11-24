"""SQLite persistence layer for per-chat sessions and conversation history."""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

from .config import BotConfig
from .models import ConversationRecord, SessionKey, SessionState


class SessionStore:
    """Thread-safe SQLite store for chat session settings and conversation history."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            str(self._db_path),
            check_same_thread=False,
            timeout=10.0,
        )
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute("PRAGMA synchronous=NORMAL;")
            self._conn.execute("PRAGMA busy_timeout=5000;")
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    chat_id INTEGER NOT NULL,
                    thread_id INTEGER NOT NULL DEFAULT 0,
                    workspace TEXT NOT NULL,
                    conversation_id TEXT,
                    permission_mode TEXT NOT NULL DEFAULT 'default',
                    sandbox INTEGER NOT NULL DEFAULT 0,
                    model TEXT,
                    effort TEXT,
                    agent TEXT,
                    verbose INTEGER NOT NULL DEFAULT 1,
                    runner_mode TEXT NOT NULL DEFAULT 'persistent',
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (chat_id, thread_id)
                );

                CREATE TABLE IF NOT EXISTS conversations (
                    conversation_id TEXT PRIMARY KEY,
                    chat_id INTEGER NOT NULL,
                    thread_id INTEGER NOT NULL DEFAULT 0,
                    workspace TEXT NOT NULL,
                    title TEXT NOT NULL,
                    num_turns INTEGER NOT NULL DEFAULT 0,
                    total_tokens INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_conversations_chat_updated
                    ON conversations (chat_id, thread_id, updated_at DESC);
                """
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def get_or_create_session(self, key: SessionKey, config: BotConfig) -> SessionState:
        """Load session state for (chat_id, thread_id), or create with defaults."""
        with self._lock:
            row = self._conn.execute(
                """
                SELECT chat_id, thread_id, workspace, conversation_id, permission_mode,
                       sandbox, model, effort, agent, verbose, runner_mode, updated_at
                FROM sessions
                WHERE chat_id = ? AND thread_id = ?
                """,
                (key.chat_id, key.thread_id),
            ).fetchone()

            if row is not None:
                return self._row_to_session(row)

            now = time.time()
            state = SessionState(
                chat_id=key.chat_id,
                thread_id=key.thread_id,
                workspace=config.default_workspace,
                conversation_id=None,
                permission_mode=config.default_permission_mode,
                sandbox=config.default_sandbox,
                model=config.default_model,
                effort=config.default_effort,
                agent=config.default_agent,
                verbose=config.default_verbose,
                runner_mode=config.runner_mode,
                updated_at=now,
            )
            self._upsert_session_locked(state)
            return state

    def save_session(self, state: SessionState) -> None:
        state.updated_at = time.time()
        with self._lock:
            self._upsert_session_locked(state)

    def _upsert_session_locked(self, state: SessionState) -> None:
        self._conn.execute(
            """
            INSERT INTO sessions (
                chat_id, thread_id, workspace, conversation_id, permission_mode,
                sandbox, model, effort, agent, verbose, runner_mode, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id, thread_id) DO UPDATE SET
                workspace = excluded.workspace,
                conversation_id = excluded.conversation_id,
                permission_mode = excluded.permission_mode,
                sandbox = excluded.sandbox,
                model = excluded.model,
                effort = excluded.effort,
                agent = excluded.agent,
                verbose = excluded.verbose,
                runner_mode = excluded.runner_mode,
                updated_at = excluded.updated_at
            """,
            (
                state.chat_id,
                state.thread_id,
                state.workspace,
                state.conversation_id,
                state.permission_mode,
                1 if state.sandbox else 0,
                state.model,
                state.effort,
                state.agent,
                1 if state.verbose else 0,
                state.runner_mode,
                state.updated_at,
            ),
        )
        self._conn.commit()

    def record_conversation_turn(
        self,
        *,
        conversation_id: str,
        key: SessionKey,
        workspace: str,
        prompt: str,
        num_turns: int,
        total_tokens: int,
    ) -> None:
        """Create or update a conversation history entry."""
        if not conversation_id:
            return
        now = time.time()
        clean_title = " ".join(prompt.strip().split())[:100] or "(untitled)"
        with self._lock:
            existing = self._conn.execute(
                "SELECT num_turns, total_tokens FROM conversations WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
            if existing is None:
                self._conn.execute(
                    """
                    INSERT INTO conversations (
                        conversation_id, chat_id, thread_id, workspace,
                        title, num_turns, total_tokens, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        conversation_id,
                        key.chat_id,
                        key.thread_id,
                        workspace,
                        clean_title,
                        max(1, num_turns),
                        max(0, total_tokens),
                        now,
                        now,
                    ),
                )
            else:
                new_turns = max(int(existing["num_turns"]) + 1, num_turns)
                new_tokens = max(int(existing["total_tokens"]), total_tokens)
                self._conn.execute(
                    """
                    UPDATE conversations
                    SET workspace = ?, num_turns = ?, total_tokens = ?, updated_at = ?
                    WHERE conversation_id = ?
                    """,
                    (workspace, new_turns, new_tokens, now, conversation_id),
                )
            self._conn.commit()

    def list_conversations(self, key: SessionKey, limit: int = 10) -> list[ConversationRecord]:
        """List most recent conversations for a chat/topic."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT conversation_id, chat_id, thread_id, workspace, title,
                       num_turns, total_tokens, created_at, updated_at
                FROM conversations
                WHERE chat_id = ? AND thread_id = ?
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (key.chat_id, key.thread_id, limit),
            ).fetchall()
            return [self._row_to_conversation(r) for r in rows]

    def get_conversation(self, conversation_id: str) -> ConversationRecord | None:
        """Look up a conversation by exact ID or unique prefix."""
        clean = conversation_id.strip()
        if not clean:
            return None
        with self._lock:
            row = self._conn.execute(
                """
                SELECT conversation_id, chat_id, thread_id, workspace, title,
                       num_turns, total_tokens, created_at, updated_at
                FROM conversations
                WHERE conversation_id = ?
                """,
                (clean,),
            ).fetchone()
            if row is not None:
                return self._row_to_conversation(row)

            # Prefix lookup if at least 6 chars
            if len(clean) >= 6:
                rows = self._conn.execute(
                    """
                    SELECT conversation_id, chat_id, thread_id, workspace, title,
                           num_turns, total_tokens, created_at, updated_at
                    FROM conversations
                    WHERE conversation_id LIKE ?
                    ORDER BY updated_at DESC
                    LIMIT 2
                    """,
                    (f"{clean}%",),
                ).fetchall()
                if len(rows) == 1:
                    return self._row_to_conversation(rows[0])
            return None

    @staticmethod
    def _row_to_session(row: sqlite3.Row) -> SessionState:
        return SessionState(
            chat_id=int(row["chat_id"]),
            thread_id=int(row["thread_id"]),
            workspace=str(row["workspace"]),
            conversation_id=row["conversation_id"],
            permission_mode=str(row["permission_mode"]),
            sandbox=bool(row["sandbox"]),
            model=row["model"],
            effort=row["effort"],
            agent=row["agent"],
            verbose=bool(row["verbose"]),
            runner_mode=str(row["runner_mode"]),
            updated_at=float(row["updated_at"]),
        )

    @staticmethod
    def _row_to_conversation(row: sqlite3.Row) -> ConversationRecord:
        return ConversationRecord(
            conversation_id=str(row["conversation_id"]),
            chat_id=int(row["chat_id"]),
            thread_id=int(row["thread_id"]),
            workspace=str(row["workspace"]),
            title=str(row["title"]),
            num_turns=int(row["num_turns"]),
            total_tokens=int(row["total_tokens"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )
