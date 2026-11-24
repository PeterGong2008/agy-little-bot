"""VPS environment diagnostics CLI (`agy-tg-doctor` / `python -m agy_tg_bot.doctor`)."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

from .agy_client import AgyRunner
from .config import ConfigError, load_config


def _status_line(ok: bool | None, label: str, detail: str) -> str:
    icon = "✅" if ok is True else ("⚠️" if ok is None else "❌")
    return f"{icon} {label}: {detail}"


async def run_doctor(
    *,
    env_file: Path | None = None,
    live_test: bool = False,
    check_telegram: bool = False,
) -> int:
    failures = 0

    print("=== AGY Telegram Bot — VPS Doctor ===\n")

    # 1. Python version
    py_ok = sys.version_info >= (3, 10)
    print(
        _status_line(
            py_ok,
            "Python",
            f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
            + ("" if py_ok else " (requires >= 3.10)"),
        )
    )
    if not py_ok:
        failures += 1

    # 2. Config (.env)
    cfg = None
    try:
        cfg = load_config(env_file=env_file, require_token=True, require_allowlist=True)
        print(
            _status_line(
                True,
                "Config (.env)",
                f"allowed_users={len(cfg.allowed_user_ids)}, mode={cfg.runner_mode}, "
                f"perm={cfg.default_permission_mode}, workspace={cfg.default_workspace}",
            )
        )
    except ConfigError as exc:
        print(_status_line(False, "Config (.env)", str(exc)))
        failures += 1
        # Try loading relaxed config for further diagnostics
        try:
            cfg = load_config(env_file=env_file, require_token=False, require_allowlist=False)
        except ConfigError:
            cfg = None

    if cfg is None:
        print("\nFix configuration errors above and re-run doctor.")
        return 1

    # 3. Default workspace
    ws = Path(cfg.default_workspace)
    ws_ok = ws.is_dir() and os.access(ws, os.R_OK | os.W_OK)
    if not ws.exists():
        print(
            _status_line(
                None,
                "Workspace",
                f"{ws} does not exist yet (will be auto-created on bot start)",
            )
        )
    else:
        print(
            _status_line(
                ws_ok,
                "Workspace",
                f"{ws} ({'readable & writable' if ws_ok else 'missing r/w permissions'})",
            )
        )
        if not ws_ok:
            failures += 1

    # 4. `agy` binary discovery
    agy_path = Path(cfg.agy_bin)
    resolved_bin = str(agy_path) if agy_path.is_file() else shutil.which(cfg.agy_bin)
    if resolved_bin:
        print(_status_line(True, "AGY binary", resolved_bin))
    else:
        print(
            _status_line(
                False,
                "AGY binary",
                f"'{cfg.agy_bin}' not found. Install via: "
                "curl -fsSL https://antigravity.google/cli/install.sh | bash",
            )
        )
        failures += 1

    # 5. AGY settings & auth hints
    settings_path = Path.home() / ".gemini" / "antigravity-cli" / "settings.json"
    auth_hints: list[str] = []
    if settings_path.is_file():
        try:
            settings_data = json.loads(settings_path.read_text(encoding="utf-8"))
            provider = settings_data.get("modelProvider", "")
            if provider == "gemini":
                has_key = bool(os.environ.get("GEMINI_API_KEY"))
                print(
                    _status_line(
                        has_key,
                        "Auth (Gemini API Key)",
                        "modelProvider=gemini and GEMINI_API_KEY is set"
                        if has_key
                        else "modelProvider=gemini in settings.json, but GEMINI_API_KEY is NOT set!",
                    )
                )
                if not has_key:
                    failures += 1
            else:
                auth_hints.append(f"settings.json found ({settings_path})")
        except Exception as exc:
            print(_status_line(None, "AGY settings.json", f"Could not parse {settings_path}: {exc}"))
    else:
        auth_hints.append(f"no {settings_path} yet (CLI defaults apply)")

    if os.environ.get("AGY_ADC_AUTH", "").lower() in {"1", "true", "yes"}:
        print(_status_line(True, "Auth (Google Cloud ADC)", "AGY_ADC_AUTH=true"))
    elif os.name != "nt":
        dbus_addr = os.environ.get("DBUS_SESSION_BUS_ADDRESS")
        if dbus_addr:
            print(_status_line(True, "Linux D-Bus / Keyring", f"DBUS_SESSION_BUS_ADDRESS={dbus_addr}"))
        else:
            print(
                _status_line(
                    None,
                    "Linux D-Bus / Keyring",
                    "DBUS_SESSION_BUS_ADDRESS is unset. If using SSH OAuth keyring auth under "
                    "systemd, use the user service (`systemd --user` + `loginctl enable-linger`) "
                    "or configure GEMINI_API_KEY / AGY_ADC_AUTH.",
                )
            )

    if auth_hints:
        print(_status_line(None, "AGY Settings", "; ".join(auth_hints)))

    # 6. Query `agy models`
    runner = AgyRunner(cfg)
    if resolved_bin:
        models, err = await runner.list_models(cwd=str(ws) if ws.is_dir() else None)
        if err:
            print(_status_line(False, "agy models", err.splitlines()[0]))
            failures += 1
        else:
            print(_status_line(True, "agy models", f"{len(models)} model(s) available"))

    # 7. Optional live headless prompt test
    if live_test and resolved_bin:
        print("\nRunning live headless test (`agy -p` stream-json)...")
        from .models import SessionState

        test_session = SessionState(
            chat_id=0,
            thread_id=0,
            workspace=str(ws if ws.is_dir() else Path.cwd()),
            permission_mode="plan",
            runner_mode="oneshot",
        )
        res = await runner.run_turn(test_session, "Reply with exactly: OK")
        if res.ok:
            print(
                _status_line(
                    True,
                    "Live headless test",
                    f"status={res.status}, reply={res.response.strip()[:60]!r}, "
                    f"duration={res.turn_duration_seconds:.1f}s",
                )
            )
        else:
            print(_status_line(False, "Live headless test", f"status={res.status}, error={res.error}"))
            failures += 1

    # 8. Optional Telegram Bot API getMe check
    if check_telegram and cfg.telegram_bot_token:
        try:
            from telegram import Bot
            from telegram.request import HTTPXRequest

            req = HTTPXRequest(proxy=cfg.telegram_proxy_url) if cfg.telegram_proxy_url else None
            bot = Bot(token=cfg.telegram_bot_token, request=req)
            async with bot:
                me = await bot.get_me()
            print(_status_line(True, "Telegram Bot API", f"connected as @{me.username} (id={me.id})"))
        except Exception as exc:
            print(_status_line(False, "Telegram Bot API", str(exc)))
            failures += 1

    print("\n" + ("All checks passed!" if failures == 0 else f"Completed with {failures} failure(s)."))
    return 0 if failures == 0 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agy-tg-doctor",
        description="Diagnose VPS environment, AGY CLI auth, and Telegram connectivity",
    )
    parser.add_argument("--env-file", type=Path, default=None, help="Path to .env file")
    parser.add_argument(
        "--live-test",
        action="store_true",
        help="Send a tiny headless test prompt to `agy` to verify end-to-end authentication",
    )
    parser.add_argument(
        "--check-telegram",
        action="store_true",
        help="Verify Telegram Bot token and network connectivity via getMe",
    )
    args = parser.parse_args(argv)
    return asyncio.run(
        run_doctor(
            env_file=args.env_file,
            live_test=args.live_test,
            check_telegram=args.check_telegram,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
