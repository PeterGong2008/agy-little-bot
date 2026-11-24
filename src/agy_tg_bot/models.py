"""Core domain models for AGY Telegram Bot."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

VALID_PERMISSION_MODES: tuple[str, ...] = (
    "default",
    "auto",
    "accept-edits",
    "plan",
    "danger",
)

VALID_RUNNER_MODES: tuple[str, ...] = ("persistent", "oneshot")

VALID_EFFORT_LEVELS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")


@dataclass(frozen=True, slots=True)
class SessionKey:
    """Unique conversation scope keyed by Telegram chat + optional forum topic."""

    chat_id: int
    thread_id: int = 0

    @property
    def label(self) -> str:
        if self.thread_id:
            return f"{self.chat_id}:{self.thread_id}"
        return str(self.chat_id)


@dataclass(slots=True)
class SessionState:
    """Persisted per-chat/topic settings and active AGY conversation binding."""

    chat_id: int
    thread_id: int
    workspace: str
    conversation_id: str | None = None
    permission_mode: str = "default"
    sandbox: bool = False
    model: str | None = None
    effort: str | None = None
    agent: str | None = None
    verbose: bool = True
    runner_mode: str = "persistent"
    updated_at: float = 0.0

    @property
    def key(self) -> SessionKey:
        return SessionKey(self.chat_id, self.thread_id)

    def worker_signature(
        self,
        extra_dirs: tuple[str, ...] = (),
        disable_slash_commands: bool = False,
    ) -> tuple[Any, ...]:
        """Signature identifying subprocess flags; changing it requires respawning the worker."""
        return (
            self.workspace,
            self.permission_mode,
            self.sandbox,
            self.model or "",
            self.effort or "",
            self.agent or "",
            extra_dirs,
            disable_slash_commands,
        )


@dataclass(slots=True)
class ConversationRecord:
    """Historical conversation metadata for /history and /resume."""

    conversation_id: str
    chat_id: int
    thread_id: int
    workspace: str
    title: str
    num_turns: int = 0
    total_tokens: int = 0
    created_at: float = 0.0
    updated_at: float = 0.0


@dataclass(slots=True)
class TokenUsage:
    """Token consumption metrics reported by `agy`."""

    input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    cache_read_tokens: int = 0
    total_tokens: int = 0

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> TokenUsage:
        if not isinstance(data, dict):
            return cls()
        return cls(
            input_tokens=int(data.get("input_tokens") or 0),
            output_tokens=int(data.get("output_tokens") or 0),
            thinking_tokens=int(data.get("thinking_tokens") or 0),
            cache_read_tokens=int(data.get("cache_read_tokens") or 0),
            total_tokens=int(data.get("total_tokens") or 0),
        )

    def delta_from(self, previous: TokenUsage) -> TokenUsage:
        """Compute non-negative per-turn delta from a previous cumulative snapshot."""
        return TokenUsage(
            input_tokens=max(0, self.input_tokens - previous.input_tokens),
            output_tokens=max(0, self.output_tokens - previous.output_tokens),
            thinking_tokens=max(0, self.thinking_tokens - previous.thinking_tokens),
            cache_read_tokens=max(0, self.cache_read_tokens - previous.cache_read_tokens),
            total_tokens=max(0, self.total_tokens - previous.total_tokens),
        )


@dataclass(slots=True)
class ToolTrace:
    """Tracked tool or subagent invocation during a turn."""

    step_index: int
    name: str
    state: str = "ACTIVE"
    summary: str = ""
    duration_seconds: float | None = None
    error_message: str | None = None
    is_subagent: bool = False


@dataclass(slots=True)
class TurnProgress:
    """Live snapshot emitted to the Telegram streaming renderer."""

    conversation_id: str | None = None
    text_preview: str = ""
    tools: list[ToolTrace] = field(default_factory=list)
    permission_mode: str | None = None
    stderr_notices: list[str] = field(default_factory=list)


@dataclass(slots=True)
class TurnResult:
    """Final outcome of a single AGY turn."""

    conversation_id: str | None
    status: str  # SUCCESS, ERROR, CANCELED, INTERRUPTED, TIMEOUT, etc.
    response: str = ""
    error: str | None = None
    duration_seconds: float = 0.0
    turn_duration_seconds: float = 0.0
    num_turns: int = 0
    usage: TokenUsage = field(default_factory=TokenUsage)
    turn_usage: TokenUsage = field(default_factory=TokenUsage)
    tools: list[ToolTrace] = field(default_factory=list)
    stderr_lines: list[str] = field(default_factory=list)
    permission_denials: list[str] = field(default_factory=list)
    effective_permission_mode: str | None = None
    exit_code: int | None = None

    @property
    def ok(self) -> bool:
        return self.status == "SUCCESS"
