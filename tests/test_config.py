"""Tests for configuration loading, fail-closed auth, and workspace boundaries."""

from __future__ import annotations

from pathlib import Path

import pytest

from agy_tg_bot.config import ConfigError, load_config


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in [
        "TELEGRAM_BOT_TOKEN",
        "ALLOWED_USER_IDS",
        "ALLOWED_CHAT_IDS",
        "TELEGRAM_PROXY_URL",
        "AGY_BIN",
        "DEFAULT_WORKSPACE",
        "ALLOWED_WORKSPACE_ROOTS",
        "AGY_EXTRA_DIRS",
        "AGY_RUNNER_MODE",
        "AGY_DEFAULT_PERMISSION_MODE",
        "ALLOW_DANGEROUS_MODE",
        "AGY_DEFAULT_SANDBOX",
        "AGY_DEFAULT_MODEL",
        "AGY_DEFAULT_EFFORT",
        "AGY_DEFAULT_AGENT",
        "DB_PATH",
    ]:
        monkeypatch.delenv(key, raising=False)


def test_fail_closed_when_allowlist_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _clear_env(monkeypatch)
    env_file = tmp_path / ".env"
    env_file.write_text("TELEGRAM_BOT_TOKEN=999:real-token-value\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="ALLOWED_USER_IDS"):
        load_config(env_file=env_file)


def test_fail_closed_placeholder_token(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _clear_env(monkeypatch)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "TELEGRAM_BOT_TOKEN=123456789:replace-with-your-bot-token\nALLOWED_USER_IDS=42\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="TELEGRAM_BOT_TOKEN"):
        load_config(env_file=env_file)


def test_danger_mode_conflict_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _clear_env(monkeypatch)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "TELEGRAM_BOT_TOKEN=999:valid-token",
                "ALLOWED_USER_IDS=42",
                "ALLOW_DANGEROUS_MODE=false",
                "AGY_DEFAULT_PERMISSION_MODE=danger",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="ALLOW_DANGEROUS_MODE=false"):
        load_config(env_file=env_file)


def test_authorization_and_path_boundaries(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _clear_env(monkeypatch)
    allowed_root = tmp_path / "projects"
    sub = allowed_root / "app"
    outside = tmp_path / "secrets"
    sub.mkdir(parents=True)
    outside.mkdir(parents=True)

    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "TELEGRAM_BOT_TOKEN=999:valid-token",
                "ALLOWED_USER_IDS=100,200",
                "ALLOWED_CHAT_IDS=-10012345",
                f"DEFAULT_WORKSPACE={sub}",
                f"ALLOWED_WORKSPACE_ROOTS={allowed_root}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    cfg = load_config(env_file=env_file)

    # Authorization checks
    assert cfg.is_user_authorized(100, 100) is True  # private DM
    assert cfg.is_user_authorized(100, -10012345) is True  # allowed group
    assert cfg.is_user_authorized(100, -10099999) is False  # unapproved group
    assert cfg.is_user_authorized(999, 999) is False  # unauthorized user
    assert cfg.is_user_authorized(None, 100) is False

    # Workspace boundary checks (including traversal)
    assert cfg.is_path_allowed(allowed_root) is True
    assert cfg.is_path_allowed(sub) is True
    assert cfg.is_path_allowed(sub / ".." / "app") is True
    assert cfg.is_path_allowed(outside) is False
    assert cfg.is_path_allowed(sub / ".." / ".." / "secrets") is False
