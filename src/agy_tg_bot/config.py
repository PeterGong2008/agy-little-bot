"""Configuration loading, validation, and path security policies."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from .models import VALID_EFFORT_LEVELS, VALID_PERMISSION_MODES, VALID_RUNNER_MODES


class ConfigError(ValueError):
    """Raised when required configuration is missing or invalid."""


def _parse_bool(raw: str | None, default: bool) -> bool:
    if raw is None or raw.strip() == "":
        return default
    val = raw.strip().lower()
    if val in {"1", "true", "yes", "on", "y"}:
        return True
    if val in {"0", "false", "no", "off", "n"}:
        return False
    raise ConfigError(f"Invalid boolean value: {raw!r}")


def _parse_int(raw: str | None, default: int, *, min_val: int | None = None) -> int:
    if raw is None or raw.strip() == "":
        return default
    try:
        val = int(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"Invalid integer value: {raw!r}") from exc
    if min_val is not None and val < min_val:
        raise ConfigError(f"Integer value {val} must be >= {min_val}")
    return val


def _parse_float(raw: str | None, default: float, *, min_val: float | None = None) -> float:
    if raw is None or raw.strip() == "":
        return default
    try:
        val = float(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"Invalid float value: {raw!r}") from exc
    if min_val is not None and val < min_val:
        raise ConfigError(f"Float value {val} must be >= {min_val}")
    return val


def _parse_int_set(raw: str | None, field_name: str) -> frozenset[int]:
    if not raw or not raw.strip():
        return frozenset()
    result: set[int] = set()
    for part in raw.split(","):
        item = part.strip()
        if not item:
            continue
        try:
            result.add(int(item))
        except ValueError as exc:
            raise ConfigError(f"{field_name} contains non-integer ID: {item!r}") from exc
    return frozenset(result)


def _parse_path_tuple(raw: str | None) -> tuple[str, ...]:
    if not raw or not raw.strip():
        return ()
    paths: list[str] = []
    for part in raw.split(","):
        item = part.strip()
        if not item:
            continue
        paths.append(str(Path(item).expanduser().resolve()))
    return tuple(paths)


def resolve_agy_binary(configured: str) -> str:
    """Resolve `agy` executable path, checking standard installer locations if needed."""
    cleaned = configured.strip() or "agy"
    # Direct path provided
    expanded = Path(cleaned).expanduser()
    if expanded.is_absolute() or os.sep in cleaned or (os.altsep and os.altsep in cleaned):
        return str(expanded)

    found = shutil.which(cleaned)
    if found:
        return found

    # Fallback: official Antigravity CLI installer default directories
    candidates = [
        Path.home() / ".local" / "bin" / cleaned,
        Path.home() / ".local" / "bin" / f"{cleaned}.exe",
    ]
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        candidates.append(Path(local_app_data) / "agy" / "bin" / f"{cleaned}.exe")
        candidates.append(Path(local_app_data) / "agy" / "bin" / cleaned)

    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)

    return cleaned


@dataclass(frozen=True, slots=True)
class BotConfig:
    """Validated runtime configuration for AGY Telegram Bot."""

    telegram_bot_token: str
    allowed_user_ids: frozenset[int]
    allowed_chat_ids: frozenset[int]
    telegram_proxy_url: str | None

    agy_bin: str
    default_workspace: str
    allowed_workspace_roots: tuple[str, ...]
    extra_dirs: tuple[str, ...]
    runner_mode: str
    default_permission_mode: str
    allow_dangerous_mode: bool
    default_sandbox: bool
    default_model: str | None
    default_effort: str | None
    default_agent: str | None
    disable_slash_commands: bool
    disable_auto_update: bool

    turn_timeout_seconds: float
    session_idle_timeout_seconds: float
    max_concurrent_runs: int
    stream_edit_interval_seconds: float
    default_verbose: bool
    long_output_file_threshold: int
    max_upload_mb: int
    db_path: str
    log_level: str

    def is_user_authorized(self, user_id: int | None, chat_id: int | None) -> bool:
        """Strict fail-closed authorization check."""
        if user_id is None or user_id not in self.allowed_user_ids:
            return False
        if self.allowed_chat_ids:
            if chat_id is None:
                return False
            # Always allow private DM with an allowed user (chat_id == user_id)
            if chat_id != user_id and chat_id not in self.allowed_chat_ids:
                return False
        return True

    def is_path_allowed(self, target: Path) -> bool:
        """Check whether `target` lies within configured ALLOWED_WORKSPACE_ROOTS."""
        if not self.allowed_workspace_roots:
            return True
        try:
            resolved = target.expanduser().resolve()
        except OSError:
            return False
        for root_str in self.allowed_workspace_roots:
            root = Path(root_str).resolve()
            if resolved == root or root in resolved.parents:
                return True
        return False

    def build_subprocess_env(self) -> dict[str, str]:
        """Construct subprocess environment for `agy`, ensuring ~/.local/bin is in PATH."""
        env = os.environ.copy()
        local_bin = str(Path.home() / ".local" / "bin")
        current_path = env.get("PATH", "")
        path_parts = current_path.split(os.pathsep) if current_path else []
        if local_bin not in path_parts:
            env["PATH"] = f"{local_bin}{os.pathsep}{current_path}" if current_path else local_bin
        if self.disable_auto_update:
            env.setdefault("AGY_CLI_DISABLE_AUTO_UPDATE", "true")
        return env


def load_config(
    env_file: str | Path | None = None,
    *,
    require_token: bool = True,
    require_allowlist: bool = True,
) -> BotConfig:
    """Load and validate BotConfig from environment (and optional .env file)."""
    if env_file is not None:
        load_dotenv(dotenv_path=env_file, override=False)
    else:
        load_dotenv(override=False)

    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    if require_token and (not token or token.startswith("123456789:replace-")):
        raise ConfigError(
            "TELEGRAM_BOT_TOKEN is missing or still set to the placeholder value. "
            "Configure it in .env."
        )

    allowed_users = _parse_int_set(os.environ.get("ALLOWED_USER_IDS"), "ALLOWED_USER_IDS")
    if require_allowlist and not allowed_users:
        raise ConfigError(
            "ALLOWED_USER_IDS must contain at least one numeric Telegram user ID "
            "(fail-closed security policy)."
        )

    allowed_chats = _parse_int_set(os.environ.get("ALLOWED_CHAT_IDS"), "ALLOWED_CHAT_IDS")
    proxy_url = (os.environ.get("TELEGRAM_PROXY_URL") or "").strip() or None

    agy_bin = resolve_agy_binary(os.environ.get("AGY_BIN") or "agy")

    raw_workspace = (os.environ.get("DEFAULT_WORKSPACE") or "").strip()
    if raw_workspace:
        default_ws_path = Path(raw_workspace).expanduser().resolve()
    else:
        default_ws_path = Path.cwd().resolve()

    allowed_roots = _parse_path_tuple(os.environ.get("ALLOWED_WORKSPACE_ROOTS"))
    extra_dirs = _parse_path_tuple(os.environ.get("AGY_EXTRA_DIRS"))

    runner_mode = (os.environ.get("AGY_RUNNER_MODE") or "persistent").strip().lower()
    if runner_mode not in VALID_RUNNER_MODES:
        raise ConfigError(
            f"AGY_RUNNER_MODE must be one of {VALID_RUNNER_MODES}, got {runner_mode!r}"
        )

    allow_dangerous = _parse_bool(os.environ.get("ALLOW_DANGEROUS_MODE"), True)
    default_perm = (os.environ.get("AGY_DEFAULT_PERMISSION_MODE") or "default").strip().lower()
    if default_perm not in VALID_PERMISSION_MODES:
        raise ConfigError(
            f"AGY_DEFAULT_PERMISSION_MODE must be one of {VALID_PERMISSION_MODES}, "
            f"got {default_perm!r}"
        )
    if default_perm == "danger" and not allow_dangerous:
        raise ConfigError(
            "AGY_DEFAULT_PERMISSION_MODE=danger conflicts with ALLOW_DANGEROUS_MODE=false."
        )

    default_sandbox = _parse_bool(os.environ.get("AGY_DEFAULT_SANDBOX"), False)
    default_model = (os.environ.get("AGY_DEFAULT_MODEL") or "").strip() or None

    default_effort = (os.environ.get("AGY_DEFAULT_EFFORT") or "").strip().lower() or None
    if default_effort is not None and default_effort not in VALID_EFFORT_LEVELS:
        raise ConfigError(
            f"AGY_DEFAULT_EFFORT must be one of {VALID_EFFORT_LEVELS}, got {default_effort!r}"
        )

    default_agent = (os.environ.get("AGY_DEFAULT_AGENT") or "").strip() or None
    disable_slash = _parse_bool(os.environ.get("AGY_DISABLE_SLASH_COMMANDS"), False)
    disable_auto_update = _parse_bool(os.environ.get("AGY_CLI_DISABLE_AUTO_UPDATE"), True)

    turn_timeout = _parse_float(os.environ.get("TURN_TIMEOUT_SECONDS"), 1800.0, min_val=0.0)
    idle_timeout = _parse_float(
        os.environ.get("SESSION_IDLE_TIMEOUT_SECONDS"), 600.0, min_val=10.0
    )
    max_concurrent = _parse_int(os.environ.get("MAX_CONCURRENT_RUNS"), 3, min_val=1)
    edit_interval = _parse_float(
        os.environ.get("STREAM_EDIT_INTERVAL_SECONDS"), 1.2, min_val=0.3
    )
    default_verbose = _parse_bool(os.environ.get("DEFAULT_VERBOSE"), True)
    long_output_threshold = _parse_int(
        os.environ.get("LONG_OUTPUT_FILE_THRESHOLD"), 12000, min_val=0
    )
    max_upload_mb = _parse_int(os.environ.get("MAX_UPLOAD_MB"), 20, min_val=1)

    raw_db = (os.environ.get("DB_PATH") or "./data/bot.db").strip()
    db_path = str(Path(raw_db).expanduser().resolve())

    log_level = (os.environ.get("LOG_LEVEL") or "INFO").strip().upper()
    if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ConfigError(f"Invalid LOG_LEVEL: {log_level!r}")

    cfg = BotConfig(
        telegram_bot_token=token,
        allowed_user_ids=allowed_users,
        allowed_chat_ids=allowed_chats,
        telegram_proxy_url=proxy_url,
        agy_bin=agy_bin,
        default_workspace=str(default_ws_path),
        allowed_workspace_roots=allowed_roots,
        extra_dirs=extra_dirs,
        runner_mode=runner_mode,
        default_permission_mode=default_perm,
        allow_dangerous_mode=allow_dangerous,
        default_sandbox=default_sandbox,
        default_model=default_model,
        default_effort=default_effort,
        default_agent=default_agent,
        disable_slash_commands=disable_slash,
        disable_auto_update=disable_auto_update,
        turn_timeout_seconds=turn_timeout,
        session_idle_timeout_seconds=idle_timeout,
        max_concurrent_runs=max_concurrent,
        stream_edit_interval_seconds=edit_interval,
        default_verbose=default_verbose,
        long_output_file_threshold=long_output_threshold,
        max_upload_mb=max_upload_mb,
        db_path=db_path,
        log_level=log_level,
    )

    if allowed_roots and not cfg.is_path_allowed(default_ws_path):
        raise ConfigError(
            f"DEFAULT_WORKSPACE ({default_ws_path}) is outside ALLOWED_WORKSPACE_ROOTS "
            f"({allowed_roots})."
        )

    return cfg
