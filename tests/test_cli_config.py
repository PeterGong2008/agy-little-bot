"""Tests for EnvFileDocument, AgySettingsDocument, and InteractiveConfigCLI."""

from __future__ import annotations

import io
import json
from pathlib import Path

from agy_tg_bot.cli_config import InteractiveConfigCLI, main
from agy_tg_bot.env_editor import AgySettingsDocument, EnvFileDocument


def _seed_env(tmp_path: Path) -> Path:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "# Header comment must be preserved",
                "TELEGRAM_BOT_TOKEN=1234567890:ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                "ALLOWED_USER_IDS=1001",
                "DEFAULT_WORKSPACE=~/workspace",
                "AGY_DEFAULT_PERMISSION_MODE=default",
                "ALLOW_DANGEROUS_MODE=true",
                "AGY_DEFAULT_SANDBOX=false",
                "# GEMINI_API_KEY=",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return env_path


def test_env_roundtrip_preserves_comments_and_uncomments_template(tmp_path: Path) -> None:
    env_path = _seed_env(tmp_path)
    doc = EnvFileDocument.load(env_path)

    doc.set("AGY_DEFAULT_PERMISSION_MODE", "auto")
    doc.set("GEMINI_API_KEY", "AIzaSy-secret-key-123456")
    doc.set("CUSTOM_NOTE", "hello # world with spaces")
    doc.save()

    raw = env_path.read_text(encoding="utf-8")
    assert "# Header comment must be preserved" in raw
    assert "AGY_DEFAULT_PERMISSION_MODE=auto" in raw
    # Uncommented in-place (not left commented)
    assert "# GEMINI_API_KEY=" not in raw
    assert "GEMINI_API_KEY=AIzaSy-secret-key-123456" in raw
    assert 'CUSTOM_NOTE="hello # world with spaces"' in raw

    # Reload via python-dotenv and verify exact values
    reloaded = EnvFileDocument.load(env_path)
    assert reloaded.get("AGY_DEFAULT_PERMISSION_MODE") == "auto"
    assert reloaded.get("GEMINI_API_KEY") == "AIzaSy-secret-key-123456"
    assert reloaded.get("CUSTOM_NOTE") == "hello # world with spaces"
    assert reloaded.extra_keys() == ["CUSTOM_NOTE"]
    assert reloaded.validate_all() == []


def test_agy_settings_document_preserves_unknown_keys(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps({"colorScheme": "tokyo night", "customUnknown": 42}),
        encoding="utf-8",
    )

    doc = AgySettingsDocument.load(settings_path)
    doc.set_field("agentMode", "auto")
    doc.set_field("enableTerminalSandbox", True)
    doc.set_permissions_list("allow", ["command(git)", "write_file(src/)"])
    doc.save()

    saved = json.loads(settings_path.read_text(encoding="utf-8"))
    assert saved["colorScheme"] == "tokyo night"
    assert saved["customUnknown"] == 42
    assert saved["agentMode"] == "auto"
    assert saved["enableTerminalSandbox"] is True
    assert saved["permissions"]["allow"] == ["command(git)", "write_file(src/)"]


def test_interactive_session_editing_and_gemini_provider_sync(tmp_path: Path) -> None:
    env_path = _seed_env(tmp_path)
    agy_settings_path = tmp_path / "agy_settings.json"
    doc = EnvFileDocument.load(env_path)

    # Scripted user responses:
    # 1. `perm` -> pick `2` (auto)
    # 2. `users` -> append `2002`, then enter to finish list
    # 3. `sandbox` -> `t` toggle to true
    # 4. inline custom var `HTTP_PROXY=http://127.0.0.1:7890`, pause enter
    # 5. `apikey` -> set GEMINI_API_KEY, answer `y` to sync modelProvider=gemini
    # 6. `a` -> open AGY settings -> `6` (permissions.allow) -> `p1` (command(git)) -> enter -> `s` save -> pause -> `b` back
    # 7. `q` -> quit -> `s` save & exit
    answers = iter(
        [
            "perm",
            "2",
            "users",
            "2002",
            "",
            "sandbox",
            "t",
            "HTTP_PROXY=http://127.0.0.1:7890",
            "",
            "apikey",
            "AIzaSy-test-key-987654321",
            "y",
            "a",
            "6",
            "p1",
            "",
            "s",
            "",
            "b",
            "q",
            "s",
        ]
    )
    out = io.StringIO()
    cli = InteractiveConfigCLI(
        doc,
        agy_settings_path=agy_settings_path,
        input_fn=lambda prompt="": next(answers),
        out=out,
        use_color=False,
        clear_screen=False,
    )
    rc = cli.run_interactive()
    assert rc == 0

    reloaded_env = EnvFileDocument.load(env_path)
    assert reloaded_env.get("AGY_DEFAULT_PERMISSION_MODE") == "auto"
    assert reloaded_env.get("ALLOWED_USER_IDS") == "1001,2002"
    assert reloaded_env.get("AGY_DEFAULT_SANDBOX") == "true"
    assert reloaded_env.get("HTTP_PROXY") == "http://127.0.0.1:7890"
    assert reloaded_env.get("GEMINI_API_KEY") == "AIzaSy-test-key-987654321"

    agy_saved = json.loads(agy_settings_path.read_text(encoding="utf-8"))
    assert agy_saved["modelProvider"] == "gemini"
    assert "command(git)" in agy_saved["permissions"]["allow"]


def test_noninteractive_subcommands_set_get_unset_list(
    tmp_path: Path, capsys
) -> None:
    env_path = _seed_env(tmp_path)

    # Set using alias
    rc = main(["--env-file", str(env_path), "set", "perm", "accept-edits"])
    assert rc == 0

    # Get using alias
    rc = main(["--env-file", str(env_path), "get", "perm"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "accept-edits" in captured.out

    # Invalid value fails with exit 2
    rc = main(["--env-file", str(env_path), "set", "perm", "not-a-valid-mode"])
    assert rc == 2

    # Set and unset custom var
    assert main(["--env-file", str(env_path), "set", "MY_FLAG", "yes"]) == 0
    assert main(["--env-file", str(env_path), "get", "MY_FLAG"]) == 0
    assert main(["--env-file", str(env_path), "unset", "MY_FLAG"]) == 0
    assert main(["--env-file", str(env_path), "get", "MY_FLAG"]) == 1

    # List prints masked secrets
    capsys.readouterr()
    assert main(["--env-file", str(env_path), "list"]) == 0
    list_out = capsys.readouterr().out
    assert "TELEGRAM_BOT_TOKEN" in list_out
    assert "ABCDEFGHIJKLMNOPQRSTUVWXYZ" not in list_out
