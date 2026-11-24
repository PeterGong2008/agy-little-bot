"""Interactive CLI (`agy-tg-config`) to edit `.env` variables and AGY `settings.json`."""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import subprocess
import sys
import unicodedata
from collections.abc import Callable
from pathlib import Path
from typing import TextIO

from .agy_client import AgyRunner, parse_cli_pairs
from .config import load_config, resolve_agy_binary
from .doctor import run_doctor
from .env_editor import (
    AGY_AGENT_MODES,
    AGY_TOOL_PERMISSIONS,
    GROUP_ORDER,
    GROUP_TITLES,
    SETTING_SPECS,
    SPECS_BY_KEY,
    AgySettingsDocument,
    EnvFileDocument,
    SettingSpec,
    mask_secret,
    validate_setting_value,
)

WIZARD_KEYS: tuple[str, ...] = (
    "TELEGRAM_BOT_TOKEN",
    "ALLOWED_USER_IDS",
    "DEFAULT_WORKSPACE",
    "AGY_DEFAULT_PERMISSION_MODE",
    "AGY_RUNNER_MODE",
    "TELEGRAM_PROXY_URL",
)

PRESET_ALLOW_RULES: tuple[tuple[str, str], ...] = (
    ("command(git)", "放行所有 git 命令"),
    ("command(pytest)", "放行 pytest 测试"),
    ("command(docker ps)", "放行 docker ps 查看容器"),
    ("command(systemctl status)", "放行查看 systemd 状态"),
    ("write_file(src/)", "放行写入 src/ 目录"),
)


def _display_width(text: str) -> int:
    width = 0
    for ch in text:
        if unicodedata.combining(ch):
            continue
        width += 2 if unicodedata.east_asian_width(ch) in {"W", "F"} else 1
    return width


def _pad_right(text: str, target_width: int) -> str:
    cur = _display_width(text)
    if cur >= target_width:
        return text
    return text + (" " * (target_width - cur))


def _truncate_display(text: str, max_width: int) -> str:
    if _display_width(text) <= max_width:
        return text
    out: list[str] = []
    cur = 0
    for ch in text:
        w = 0 if unicodedata.combining(ch) else (2 if unicodedata.east_asian_width(ch) in {"W", "F"} else 1)
        if cur + w > max_width - 1:
            break
        out.append(ch)
        cur += w
    return "".join(out) + "…"


class _Ansi:
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def wrap(self, code: str, text: str) -> str:
        if not self.enabled:
            return text
        return f"\033[{code}m{text}\033[0m"

    def bold(self, text: str) -> str:
        return self.wrap("1", text)

    def dim(self, text: str) -> str:
        return self.wrap("2", text)

    def cyan(self, text: str) -> str:
        return self.wrap("36", text)

    def green(self, text: str) -> str:
        return self.wrap("32", text)

    def yellow(self, text: str) -> str:
        return self.wrap("33", text)

    def red(self, text: str) -> str:
        return self.wrap("31", text)

    def magenta(self, text: str) -> str:
        return self.wrap("35", text)


def _detect_color(stream: TextIO) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    try:
        return bool(stream.isatty())
    except Exception:
        return False


def _enable_windows_ansi() -> None:
    if os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass


def _resolve_default_env_path(env_file: Path | None) -> Path:
    if env_file is not None:
        return env_file.expanduser().resolve()
    cwd_env = Path.cwd() / ".env"
    if cwd_env.is_file() or (Path.cwd() / ".env.example").is_file():
        return cwd_env.resolve()
    # Check repository root relative to installed editable package
    pkg_root = Path(__file__).resolve().parents[2]
    if (pkg_root / ".env").is_file() or (pkg_root / ".env.example").is_file():
        return (pkg_root / ".env").resolve()
    return cwd_env.resolve()


class InteractiveConfigCLI:
    """Interactive terminal configurator for `.env` and AGY `settings.json`."""

    def __init__(
        self,
        env_doc: EnvFileDocument,
        *,
        agy_settings_path: Path | None = None,
        input_fn: Callable[[str], str] = input,
        out: TextIO | None = None,
        use_color: bool | None = None,
        clear_screen: bool | None = None,
    ) -> None:
        self.env_doc = env_doc
        self.agy_settings_path = agy_settings_path or AgySettingsDocument.default_path()
        self._input = input_fn
        self._out = out or sys.stdout
        color = _detect_color(self._out) if use_color is None else use_color
        self._c = _Ansi(color)
        self._clear_enabled = (
            (clear_screen if clear_screen is not None else color)
            and os.environ.get("TERM", "") != "dumb"
        )
        self._show_secrets = False
        self._alias_map = self._build_alias_map()

    @staticmethod
    def _build_alias_map() -> dict[str, SettingSpec]:
        mapping: dict[str, SettingSpec] = {}
        for spec in SETTING_SPECS:
            mapping[spec.key.lower()] = spec
            for alias in spec.aliases:
                mapping[alias.lower()] = spec
        return mapping

    def _print(self, text: str = "") -> None:
        self._out.write(text + "\n")
        self._out.flush()

    def _clear(self) -> None:
        if self._clear_enabled:
            self._out.write("\033[2J\033[H")
            self._out.flush()

    def _prompt(self, prompt_text: str) -> str | None:
        try:
            return self._input(prompt_text)
        except (EOFError, KeyboardInterrupt):
            self._print("")
            return None

    def _pause(self, msg: str = "按回车键继续...") -> None:
        self._prompt(self._c.dim(msg))

    # ------------------------------------------------------------------
    # Dashboard & Main Loop
    # ------------------------------------------------------------------

    def render_dashboard(self) -> tuple[list[tuple[str, SettingSpec | None]], list[str]]:
        """Print the full categorized configuration table and return indexed lookup list."""
        indexed: list[tuple[str, SettingSpec | None]] = []
        issues = self.env_doc.validate_all()

        dirty_badge = (
            self._c.yellow(" [未保存变更 *]") if self.env_doc.dirty else self._c.green(" [已保存]")
        )
        self._print(self._c.bold(self._c.cyan("============================================================")))
        self._print(self._c.bold("  AGY Telegram Bot — 交互式设置与环境变量管理器"))
        self._print(self._c.bold(self._c.cyan("============================================================")))
        self._print(f"  配置文件: {self._c.bold(str(self.env_doc.path))}{dirty_badge}")
        self._print(f"  AGY 设置: {self._c.dim(str(self.agy_settings_path))}")

        if issues:
            self._print(self._c.red(f"  配置校验: ❌ 发现 {len(issues)} 项待完善："))
            for err in issues[:4]:
                self._print(self._c.red(f"    • {err}"))
            if len(issues) > 4:
                self._print(self._c.red(f"    … 另有 {len(issues) - 4} 项"))
        else:
            self._print(self._c.green("  配置校验: ✅ 所有必填项与规则校验通过"))

        for group in GROUP_ORDER:
            specs = [s for s in SETTING_SPECS if s.group == group]
            self._print("")
            self._print(self._c.bold(self._c.cyan(f"── {GROUP_TITLES[group]} ──")))
            for spec in specs:
                idx = len(indexed) + 1
                indexed.append((spec.key, spec))
                val = self.env_doc.get(spec.key, "")
                ok, _ = validate_setting_value(spec, val)
                val_display = self._format_display_value(spec, val)
                if not ok:
                    val_colored = self._c.red(val_display)
                elif val == "":
                    val_colored = self._c.dim(val_display)
                else:
                    val_colored = self._c.green(val_display)

                num_str = self._c.yellow(f"[{idx:>2}]")
                key_col = _pad_right(spec.key, 30)
                val_plain_len = _display_width(val_display)
                val_pad = " " * max(1, 28 - val_plain_len)
                req_tag = self._c.red("*") if spec.required else " "
                self._print(
                    f" {num_str}{req_tag} {key_col} {val_colored}{val_pad} {self._c.dim(spec.label)}"
                )

        extras = self.env_doc.extra_keys()
        if extras:
            self._print("")
            self._print(self._c.bold(self._c.magenta("── 自定义环境变量 ──")))
            for key in extras:
                idx = len(indexed) + 1
                indexed.append((key, None))
                val = self.env_doc.get(key, "")
                shown = _truncate_display(val or "(空)", 28)
                self._print(
                    f" {self._c.yellow(f'[{idx:>2}]')}  {_pad_right(key, 30)} {self._c.green(shown)}"
                )

        self._print("")
        self._print(self._c.dim("────────────────────────────────────────────────────────────"))
        self._print(
            f"  输入 {self._c.yellow('编号')} 或 {self._c.yellow('变量名/别名')} 直接编辑"
            f"  (如 {self._c.cyan('1')} / {self._c.cyan('token')} / {self._c.cyan('perm')} / {self._c.cyan('ws')})"
        )
        self._print(
            f"  {self._c.yellow('[w]')} 快速向导  "
            f"{self._c.yellow('[a]')} AGY设置(settings.json)  "
            f"{self._c.yellow('[c]')} 自定义变量  "
            f"{self._c.yellow('[v]')} {'隐藏' if self._show_secrets else '显示'}密钥"
        )
        self._print(
            f"  {self._c.yellow('[d]')} 环境自检  "
            f"{self._c.yellow('[r]')} 保存并重启服务  "
            f"{self._c.yellow('[s]')} 保存  "
            f"{self._c.yellow('[h]')} 别名帮助  "
            f"{self._c.yellow('[q]')} 退出"
        )
        return indexed, issues

    def _format_display_value(self, spec: SettingSpec, val: str) -> str:
        if spec.kind == "secret":
            if self._show_secrets and val:
                return _truncate_display(val, 27)
            return _truncate_display(mask_secret(val), 27)
        if val == "":
            if spec.default:
                return _truncate_display(f"(默认: {spec.default})", 27)
            return "(未设置)"
        return _truncate_display(val, 27)

    def run_interactive(self) -> int:
        """Start the interactive dashboard loop."""
        while True:
            self._clear()
            indexed, _ = self.render_dashboard()
            raw = self._prompt(self._c.bold("\n请选择操作或输入编号/变量名 > "))
            if raw is None:
                if self._confirm_exit():
                    return 0
                continue

            cmd = raw.strip()
            if not cmd:
                continue
            low = cmd.lower()

            if low in {"q", "quit", "exit"}:
                if self._confirm_exit():
                    return 0
                continue

            if low in {"s", "save"}:
                self._save_env_with_feedback()
                self._pause()
                continue

            if low in {"w", "wizard"}:
                self.run_wizard()
                continue

            if low in {"a", "agy-settings", "settings"}:
                self.run_agy_settings_editor()
                continue

            if low in {"c", "custom"}:
                self._manage_custom_vars()
                continue

            if low in {"v", "reveal"}:
                self._show_secrets = not self._show_secrets
                continue

            if low in {"d", "doctor"}:
                self._run_doctor_interactive()
                continue

            if low in {"r", "restart"}:
                self._save_and_restart_service()
                continue

            if low in {"h", "help", "?"}:
                self._show_alias_help()
                continue

            # Inline quick assignment syntax: `KEY=VALUE`
            if "=" in cmd:
                k_part, v_part = cmd.split("=", 1)
                self._apply_inline_set(k_part.strip(), v_part.strip())
                self._pause()
                continue

            # Numeric selection
            if cmd.isdigit():
                idx = int(cmd) - 1
                if 0 <= idx < len(indexed):
                    key, spec = indexed[idx]
                    if spec is not None:
                        self._edit_spec(spec)
                    else:
                        self._edit_custom_var(key)
                else:
                    self._print(self._c.red(f"无效编号：{cmd}"))
                    self._pause()
                continue

            # Key or alias match
            spec = self._alias_map.get(low)
            if spec is not None:
                self._edit_spec(spec)
                continue

            # Custom key match (case-insensitive)
            matched_custom = next(
                (k for k in self.env_doc.extra_keys() if k.lower() == low), None
            )
            if matched_custom is not None:
                self._edit_custom_var(matched_custom)
                continue

            self._print(self._c.red(f"未识别的指令或变量名：{cmd}（输入 h 查看快捷别名）"))
            self._pause()

    def _confirm_exit(self) -> bool:
        if not self.env_doc.dirty:
            return True
        ans = self._prompt(
            self._c.yellow("检测到未保存的修改：[s]保存并退出 / [d]放弃修改退出 / [c]返回 > ")
        )
        if ans is None:
            return True
        low = ans.strip().lower()
        if low in {"s", "y", "yes", ""}:
            self._save_env_with_feedback()
            return True
        if low in {"d", "n", "no"}:
            self._print(self._c.dim("已放弃未保存的修改。"))
            return True
        return False

    def _save_env_with_feedback(self) -> bool:
        issues = self.env_doc.validate_all()
        if issues:
            self._print(self._c.yellow("\n⚠️ 当前配置仍有校验提醒："))
            for item in issues:
                self._print(self._c.yellow(f"  • {item}"))
        try:
            saved = self.env_doc.save(chmod_600=True)
            self._print(self._c.green(f"\n✅ 已保存至 {saved}（权限 600）"))
            return True
        except OSError as exc:
            self._print(self._c.red(f"\n❌ 保存失败：{exc}"))
            return False

    def _apply_inline_set(self, key_or_alias: str, value: str) -> None:
        spec = self._alias_map.get(key_or_alias.lower())
        if spec is not None:
            ok, norm = validate_setting_value(spec, value)
            if not ok:
                self._print(self._c.red(f"❌ {norm}"))
                return
            self.env_doc.set(spec.key, norm)
            shown = mask_secret(norm) if spec.kind == "secret" else (norm or "(空)")
            self._print(self._c.green(f"✅ 已更新 {spec.key} = {shown}"))
            if spec.key == "GEMINI_API_KEY" and norm:
                self._offer_sync_gemini_provider()
            return

        try:
            self.env_doc.set(key_or_alias, value)
            self._print(self._c.green(f"✅ 已更新自定义变量 {key_or_alias} = {value}"))
        except ValueError as exc:
            self._print(self._c.red(f"❌ {exc}"))

    # ------------------------------------------------------------------
    # Guided Setup Wizard
    # ------------------------------------------------------------------

    def run_wizard(self) -> int:
        """Step-by-step setup wizard for the core VPS Bot parameters."""
        self._clear()
        self._print(self._c.bold(self._c.cyan("=== AGY Telegram Bot 快速初始化向导 ===")))
        self._print(self._c.dim("依次配置最核心的 6 个参数；直接回车保留当前值，Ctrl+C 可随时中断。\n"))

        for step_idx, key in enumerate(WIZARD_KEYS, start=1):
            spec = SPECS_BY_KEY[key]
            self._print(self._c.bold(f"\n[{step_idx}/{len(WIZARD_KEYS)}] {spec.label} ({spec.key})"))
            if not self._edit_spec(spec, pause_on_error=False):
                break

        self._print("")
        save_ans = self._prompt(self._c.bold("向导完成，是否立即保存 .env？[Y/n] > "))
        if save_ans is None or save_ans.strip().lower() in {"", "y", "yes"}:
            self._save_env_with_feedback()
        self._pause()
        return 0

    # ------------------------------------------------------------------
    # Type-Aware Field Editors
    # ------------------------------------------------------------------

    def _edit_spec(self, spec: SettingSpec, *, pause_on_error: bool = True) -> bool:
        """Edit a known SettingSpec interactively. Returns False if user aborted with EOF/Ctrl+C."""
        cur = self.env_doc.get(spec.key, "")
        self._print("")
        self._print(self._c.bold(f"编辑 {spec.key}") + f" — {spec.label}")
        if spec.hint:
            self._print(self._c.dim(f"说明：{spec.hint}"))

        if spec.kind == "choice":
            return self._edit_choice(spec, cur)
        if spec.kind == "bool":
            return self._edit_bool(spec, cur)
        if spec.kind in {"int_list", "path_list"}:
            return self._edit_list(spec, cur)
        if spec.kind == "model":
            return self._edit_model(spec, cur)
        if spec.kind == "path":
            return self._edit_path(spec, cur)

        # Standard scalar / secret editor
        shown_cur = mask_secret(cur) if spec.kind == "secret" else (cur or f"(默认: {spec.default})" if spec.default else "(未设置)")
        self._print(f"当前值：{self._c.cyan(shown_cur)}")
        clear_hint = "" if spec.required else "，输入 - 清空"
        while True:
            raw = self._prompt(f"请输入新值（回车保留原值{clear_hint}）> ")
            if raw is None:
                return False
            trimmed = raw.strip()
            if trimmed == "":
                return True
            if trimmed == "-" and not spec.required:
                self.env_doc.set(spec.key, "")
                self._print(self._c.green(f"✅ 已清空 {spec.key}"))
                return True

            ok, norm = validate_setting_value(spec, trimmed)
            if not ok:
                self._print(self._c.red(f"❌ {norm}"))
                continue

            self.env_doc.set(spec.key, norm)
            shown_new = mask_secret(norm) if spec.kind == "secret" else norm
            self._print(self._c.green(f"✅ 已更新 {spec.key} = {shown_new}"))

            if spec.key == "GEMINI_API_KEY" and norm:
                self._offer_sync_gemini_provider()
            return True

    def _edit_choice(self, spec: SettingSpec, cur: str) -> bool:
        effective = cur if cur != "" else spec.default
        for idx, (val, desc) in enumerate(spec.choices, start=1):
            marker = self._c.green("●") if val == effective else " "
            label = val if val != "" else "(默认/留空)"
            self._print(f"  {marker} [{idx}] {_pad_right(label, 14)} {self._c.dim(desc)}")

        while True:
            raw = self._prompt("请选择编号或直接输入选项值（回车保持不变）> ")
            if raw is None:
                return False
            trimmed = raw.strip()
            if trimmed == "":
                return True

            chosen: str | None = None
            if trimmed.isdigit():
                i = int(trimmed) - 1
                if 0 <= i < len(spec.choices):
                    chosen = spec.choices[i][0]
            else:
                ok, norm = validate_setting_value(spec, trimmed)
                if ok:
                    chosen = norm

            if chosen is None:
                self._print(self._c.red("无效选择，请重新输入。"))
                continue

            # Guard against danger conflict
            if spec.key == "AGY_DEFAULT_PERMISSION_MODE" and chosen == "danger":
                allow_d = (self.env_doc.get("ALLOW_DANGEROUS_MODE") or "true").strip().lower()
                if allow_d in {"0", "false", "no", "off"}:
                    self._print(
                        self._c.yellow(
                            "⚠️ ALLOW_DANGEROUS_MODE 当前为 false，已同时提示：需先将其设为 true 才可启用 danger。"
                        )
                    )
                    ans = self._prompt("是否同步将 ALLOW_DANGEROUS_MODE 设为 true？[Y/n] > ")
                    if ans is not None and ans.strip().lower() in {"", "y", "yes"}:
                        self.env_doc.set("ALLOW_DANGEROUS_MODE", "true")
                    else:
                        continue

            self.env_doc.set(spec.key, chosen)
            self._print(self._c.green(f"✅ 已更新 {spec.key} = {chosen or '(默认)'}"))
            return True

    def _edit_bool(self, spec: SettingSpec, cur: str) -> bool:
        effective = (cur or spec.default or "false").strip().lower() in {"1", "true", "yes", "on"}
        self._print(
            f"当前值：{self._c.cyan('true' if effective else 'false')}"
            + (self._c.dim("（未显式设置，使用默认值）") if cur == "" else "")
        )
        raw = self._prompt("输入 [t]切换 / [1]true / [2]false / [-]清空（回车保留）> ")
        if raw is None:
            return False
        low = raw.strip().lower()
        if low == "":
            return True
        if low in {"t", "toggle"}:
            new_val = "false" if effective else "true"
        elif low in {"1", "true", "y", "yes", "on"}:
            new_val = "true"
        elif low in {"2", "false", "n", "no", "off"}:
            new_val = "false"
        elif low == "-" and not spec.required:
            new_val = ""
        else:
            self._print(self._c.red("无效输入，未修改。"))
            return True

        self.env_doc.set(spec.key, new_val)
        self._print(self._c.green(f"✅ 已更新 {spec.key} = {new_val or '(未设置)'}"))
        return True

    def _edit_list(self, spec: SettingSpec, cur: str) -> bool:
        items = [p.strip() for p in cur.split(",") if p.strip()]
        while True:
            self._print("")
            if items:
                self._print("当前列表：")
                for i, item in enumerate(items, start=1):
                    self._print(f"  [{i}] {self._c.cyan(item)}")
            else:
                self._print(self._c.dim("当前列表为空。"))

            self._print(
                self._c.dim(
                    "操作：直接输入一项或逗号分隔多项进行追加 | `-编号` 删除 | `=值1,值2` 覆盖 | `clear` 清空 | 回车完成"
                )
            )
            raw = self._prompt(f"{spec.key} > ")
            if raw is None:
                return False
            cmd = raw.strip()
            if cmd == "":
                joined = ",".join(items)
                ok, norm = validate_setting_value(spec, joined)
                if not ok:
                    self._print(self._c.red(f"❌ {norm}"))
                    continue
                self.env_doc.set(spec.key, norm)
                self._print(self._c.green(f"✅ 已更新 {spec.key} = {norm or '(空)'}"))
                return True

            if cmd.lower() == "clear":
                items.clear()
                continue

            if cmd.startswith("-"):
                target = cmd[1:].strip()
                if target.isdigit():
                    idx = int(target) - 1
                    if 0 <= idx < len(items):
                        removed = items.pop(idx)
                        self._print(self._c.yellow(f"已移除：{removed}"))
                    else:
                        self._print(self._c.red("编号超出范围。"))
                elif target in items:
                    items.remove(target)
                    self._print(self._c.yellow(f"已移除：{target}"))
                else:
                    self._print(self._c.red(f"未找到：{target}"))
                continue

            if cmd.startswith("="):
                new_raw = cmd[1:].strip()
                ok, norm = validate_setting_value(spec, new_raw)
                if not ok:
                    self._print(self._c.red(f"❌ {norm}"))
                    continue
                items = [p.strip() for p in norm.split(",") if p.strip()]
                continue

            # Append one or more comma-separated items
            candidate = ",".join(items + [cmd]) if items else cmd
            ok, norm = validate_setting_value(spec, candidate)
            if not ok:
                self._print(self._c.red(f"❌ {norm}"))
                continue
            items = [p.strip() for p in norm.split(",") if p.strip()]

    def _edit_path(self, spec: SettingSpec, cur: str) -> bool:
        effective = cur or spec.default
        resolved = Path(effective).expanduser().resolve() if effective else None
        exists_note = ""
        if resolved is not None:
            exists_note = " ✅ 目录存在" if resolved.is_dir() else " ⚠️ 目录尚不存在"
        self._print(f"当前值：{self._c.cyan(effective or '(未设置)')}{exists_note}")

        raw = self._prompt("请输入新目录路径（支持 ~，回车保持不变）> ")
        if raw is None:
            return False
        trimmed = raw.strip()
        if trimmed == "":
            return True

        target = Path(trimmed).expanduser()
        if not target.exists():
            mk = self._prompt(f"目录 {target} 尚不存在，是否立即创建？[Y/n] > ")
            if mk is not None and mk.strip().lower() in {"", "y", "yes"}:
                try:
                    target.mkdir(parents=True, exist_ok=True)
                    self._print(self._c.green(f"✅ 已创建目录：{target.resolve()}"))
                except OSError as exc:
                    self._print(self._c.red(f"❌ 创建目录失败：{exc}"))

        self.env_doc.set(spec.key, trimmed)
        self._print(self._c.green(f"✅ 已更新 {spec.key} = {trimmed}"))
        return True

    def _edit_model(self, spec: SettingSpec, cur: str) -> bool:
        self._print(f"当前模型：{self._c.cyan(cur or '(继承 CLI 默认)')}")
        self._print(
            self._c.dim("输入 `m` 调用 `agy models` 交互选择 | 输入 `0` 恢复 CLI 默认 | 直接输入模型 slug | 回车保留")
        )
        raw = self._prompt("AGY_DEFAULT_MODEL > ")
        if raw is None:
            return False
        trimmed = raw.strip()
        if trimmed == "":
            return True
        if trimmed in {"0", "-", "default", "reset"}:
            self.env_doc.set(spec.key, "")
            self._print(self._c.green("✅ 已恢复为继承 CLI 默认模型。"))
            return True

        if trimmed.lower() == "m":
            self._print(self._c.dim("正在查询 `agy models`..."))
            models, err = self._query_agy_models()
            if err:
                self._print(self._c.red(f"❌ 查询失败：{err}"))
                self._pause()
                return True
            if not models:
                self._print(self._c.yellow("未解析到可用模型列表。"))
                self._pause()
                return True
            self._print("  [0] (继承 CLI 默认)")
            for i, (slug, label) in enumerate(models, start=1):
                mark = self._c.green("●") if slug == cur else " "
                self._print(f" {mark}[{i:>2}] {_pad_right(slug, 30)} {self._c.dim(label)}")
            pick = self._prompt("请选择模型编号（回车取消）> ")
            if pick is None or not pick.strip():
                return True
            if pick.strip() == "0":
                self.env_doc.set(spec.key, "")
                self._print(self._c.green("✅ 已恢复为 CLI 默认模型。"))
                return True
            if pick.strip().isdigit():
                idx = int(pick.strip()) - 1
                if 0 <= idx < len(models):
                    slug = models[idx][0]
                    self.env_doc.set(spec.key, slug)
                    self._print(self._c.green(f"✅ 已设置默认模型为：{slug}"))
                    return True
            self._print(self._c.red("无效编号，未修改。"))
            return True

        self.env_doc.set(spec.key, trimmed)
        self._print(self._c.green(f"✅ 已设置默认模型为：{trimmed}"))
        return True

    def _query_agy_models(self) -> tuple[list[tuple[str, str]], str | None]:
        from dotenv import load_dotenv

        if self.env_doc.path.is_file():
            load_dotenv(dotenv_path=self.env_doc.path, override=True)
        try:
            cfg = load_config(
                env_file=self.env_doc.path if self.env_doc.path.is_file() else None,
                require_token=False,
                require_allowlist=False,
            )
        except Exception:
            agy_bin = resolve_agy_binary(self.env_doc.get("AGY_BIN") or "agy")
            try:
                proc = subprocess.run(
                    [agy_bin, "models"],
                    capture_output=True,
                    text=True,
                    timeout=20,
                    check=False,
                )
            except Exception as exc:
                return [], str(exc)
            if proc.returncode != 0:
                return [], (proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.returncode}")
            return parse_cli_pairs(proc.stdout), None

        runner = AgyRunner(cfg)
        return asyncio.run(runner.list_models())

    # ------------------------------------------------------------------
    # Custom Env Vars Manager
    # ------------------------------------------------------------------

    def _manage_custom_vars(self) -> None:
        while True:
            self._clear()
            self._print(self._c.bold(self._c.magenta("=== 管理自定义环境变量 (.env) ===")))
            extras = self.env_doc.extra_keys()
            if extras:
                for i, k in enumerate(extras, start=1):
                    self._print(f"  [{i}] {_pad_right(k, 28)} = {self._c.cyan(self.env_doc.get(k))}")
            else:
                self._print(self._c.dim("  （当前暂无自定义变量）"))

            self._print("")
            self._print(
                f"  {self._c.yellow('[n]')} 新增变量（如 HTTP_PROXY）  "
                f"{self._c.yellow('[编号]')} 编辑  "
                f"{self._c.yellow('[-编号]')} 删除  "
                f"{self._c.yellow('[b]')} 返回"
            )
            raw = self._prompt("请选择 > ")
            if raw is None:
                return
            cmd = raw.strip()
            if not cmd or cmd.lower() in {"b", "back", "q"}:
                return
            if cmd.lower() in {"n", "add", "new"}:
                key = self._prompt("变量名（如 HTTP_PROXY）> ")
                if key is None or not key.strip():
                    continue
                val = self._prompt(f"{key.strip()} 的值 > ")
                if val is None:
                    continue
                try:
                    self.env_doc.set(key.strip(), val.strip())
                    self._print(self._c.green(f"✅ 已添加 {key.strip()}"))
                except ValueError as exc:
                    self._print(self._c.red(f"❌ {exc}"))
                self._pause()
                continue
            if cmd.startswith("-") and cmd[1:].isdigit():
                idx = int(cmd[1:]) - 1
                if 0 <= idx < len(extras):
                    k = extras[idx]
                    self.env_doc.unset(k)
                    self._print(self._c.yellow(f"已删除变量 {k}"))
                    self._pause()
                continue
            if cmd.isdigit():
                idx = int(cmd) - 1
                if 0 <= idx < len(extras):
                    self._edit_custom_var(extras[idx])

    def _edit_custom_var(self, key: str) -> None:
        cur = self.env_doc.get(key, "")
        self._print(f"\n自定义变量 {self._c.bold(key)} 当前值：{self._c.cyan(cur)}")
        raw = self._prompt("请输入新值（回车保留，输入 DELETE 删除该变量）> ")
        if raw is None or raw.strip() == "":
            return
        if raw.strip() == "DELETE":
            self.env_doc.unset(key)
            self._print(self._c.yellow(f"已删除 {key}"))
            return
        self.env_doc.set(key, raw.strip())
        self._print(self._c.green(f"✅ 已更新 {key} = {raw.strip()}"))

    # ------------------------------------------------------------------
    # AGY CLI settings.json Editor
    # ------------------------------------------------------------------

    def _offer_sync_gemini_provider(self) -> None:
        try:
            agy_doc = AgySettingsDocument.load(self.agy_settings_path)
        except Exception:
            return
        if agy_doc.data.get("modelProvider") == "gemini":
            return
        ans = self._prompt(
            self._c.yellow(
                f"提示：agy 使用 GEMINI_API_KEY 需要将 {agy_doc.path} 中的 modelProvider 设为 \"gemini\"，是否同步写入？[Y/n] > "
            )
        )
        if ans is not None and ans.strip().lower() in {"", "y", "yes"}:
            agy_doc.set_field("modelProvider", "gemini")
            agy_doc.save()
            self._print(self._c.green(f"✅ 已同步更新 {agy_doc.path} (modelProvider=gemini)"))

    def run_agy_settings_editor(self) -> int:
        """Interactive editor for ~/.gemini/antigravity-cli/settings.json."""
        try:
            doc = AgySettingsDocument.load(self.agy_settings_path)
        except Exception as exc:
            self._print(self._c.red(f"❌ 读取 {self.agy_settings_path} 失败：{exc}"))
            self._pause()
            return 1

        while True:
            self._clear()
            dirty = self._c.yellow(" [未保存 *]") if doc.dirty else self._c.green(" [已保存]")
            self._print(self._c.bold(self._c.cyan("=== AGY CLI 设置编辑器 (settings.json) ===")))
            self._print(f"文件路径：{doc.path}{dirty}\n")

            agent_mode = str(doc.data.get("agentMode") or "(默认)")
            tool_perm = str(doc.data.get("toolPermission") or "request-review (默认)")
            sandbox = bool(doc.data.get("enableTerminalSandbox", False))
            non_ws = bool(doc.data.get("allowNonWorkspaceAccess", False))
            provider = str(doc.data.get("modelProvider") or "(默认: Google 账号 OAuth)")
            allow_rules = doc.get_permissions_list("allow")
            deny_rules = doc.get_permissions_list("deny")

            self._print(f"  [1] {_pad_right('agentMode', 25)} {self._c.cyan(agent_mode)}")
            self._print(f"  [2] {_pad_right('toolPermission', 25)} {self._c.cyan(tool_perm)}")
            self._print(f"  [3] {_pad_right('enableTerminalSandbox', 25)} {self._c.cyan(str(sandbox).lower())}")
            self._print(f"  [4] {_pad_right('allowNonWorkspaceAccess', 25)} {self._c.cyan(str(non_ws).lower())}")
            self._print(f"  [5] {_pad_right('modelProvider', 25)} {self._c.cyan(provider)}")
            self._print(
                f"  [6] {_pad_right('permissions.allow', 25)} {self._c.cyan(f'{len(allow_rules)} 条规则')} "
                f"{self._c.dim(', '.join(allow_rules[:3]))}"
            )
            self._print(
                f"  [7] {_pad_right('permissions.deny', 25)} {self._c.cyan(f'{len(deny_rules)} 条规则')} "
                f"{self._c.dim(', '.join(deny_rules[:3]))}"
            )
            self._print("")
            self._print(
                f"  {self._c.yellow('[1-7]')} 编辑对应项  "
                f"{self._c.yellow('[s]')} 保存 settings.json  "
                f"{self._c.yellow('[b]')} 返回主菜单"
            )

            raw = self._prompt("\nAGY 设置 > ")
            if raw is None:
                return 0
            cmd = raw.strip().lower()
            if cmd in {"b", "q", "back"}:
                if doc.dirty:
                    ans = self._prompt(self._c.yellow("settings.json 有未保存更改，是否保存？[Y/n] > "))
                    if ans is None or ans.strip().lower() in {"", "y", "yes"}:
                        doc.save()
                        self._print(self._c.green(f"✅ 已保存 {doc.path}"))
                return 0

            if cmd == "s":
                doc.save()
                self._print(self._c.green(f"✅ 已保存 {doc.path}"))
                self._pause()
            elif cmd == "1":
                self._pick_agy_json_choice(doc, "agentMode", AGY_AGENT_MODES)
            elif cmd == "2":
                self._pick_agy_json_choice(doc, "toolPermission", AGY_TOOL_PERMISSIONS)
            elif cmd == "3":
                doc.set_field("enableTerminalSandbox", not sandbox)
            elif cmd == "4":
                doc.set_field("allowNonWorkspaceAccess", not non_ws)
            elif cmd == "5":
                self._pick_agy_json_choice(
                    doc,
                    "modelProvider",
                    (
                        ("", "默认（Google 账号 OAuth / ADC）"),
                        ("gemini", "gemini（使用环境变量 GEMINI_API_KEY 直连 Gemini API）"),
                    ),
                )
            elif cmd == "6":
                self._edit_permission_rules(doc, "allow")
            elif cmd == "7":
                self._edit_permission_rules(doc, "deny")

    def _pick_agy_json_choice(
        self,
        doc: AgySettingsDocument,
        key: str,
        choices: tuple[tuple[str, str], ...],
    ) -> None:
        self._print(f"\n设置 {self._c.bold(key)}：")
        cur = str(doc.data.get(key) or "")
        for idx, (val, desc) in enumerate(choices, start=1):
            mark = self._c.green("●") if val == cur else " "
            self._print(f"  {mark} [{idx}] {_pad_right(val or '(默认)', 20)} {self._c.dim(desc)}")
        ans = self._prompt("选择编号（回车取消）> ")
        if ans and ans.strip().isdigit():
            i = int(ans.strip()) - 1
            if 0 <= i < len(choices):
                doc.set_field(key, choices[i][0])

    def _edit_permission_rules(self, doc: AgySettingsDocument, bucket: str) -> None:
        rules = list(doc.get_permissions_list(bucket))
        while True:
            self._print(f"\npermissions.{bucket} 当前规则：")
            if rules:
                for idx, rule in enumerate(rules, start=1):
                    self._print(f"  [{idx}] {self._c.cyan(rule)}")
            else:
                self._print(self._c.dim("  （空）"))

            if bucket == "allow":
                self._print(self._c.dim("常用模板（输入 p1~p5 快速添加）："))
                for pidx, (rule, desc) in enumerate(PRESET_ALLOW_RULES, start=1):
                    self._print(self._c.dim(f"  p{pidx}: {rule} — {desc}"))

            self._print(self._c.dim("输入规则字符串新增（如 command(git)）| `-编号` 删除 | 回车完成"))
            raw = self._prompt(f"permissions.{bucket} > ")
            if raw is None or raw.strip() == "":
                doc.set_permissions_list(bucket, rules)
                return
            cmd = raw.strip()
            if cmd.startswith("-") and cmd[1:].isdigit():
                i = int(cmd[1:]) - 1
                if 0 <= i < len(rules):
                    rules.pop(i)
                continue
            if bucket == "allow" and cmd.lower().startswith("p") and cmd[1:].isdigit():
                pi = int(cmd[1:]) - 1
                if 0 <= pi < len(PRESET_ALLOW_RULES):
                    preset = PRESET_ALLOW_RULES[pi][0]
                    if preset not in rules:
                        rules.append(preset)
                    continue
            if cmd not in rules:
                rules.append(cmd)

    # ------------------------------------------------------------------
    # Doctor, Service Restart & Help
    # ------------------------------------------------------------------

    def _run_doctor_interactive(self) -> None:
        from dotenv import load_dotenv

        if self.env_doc.dirty:
            ans = self._prompt(self._c.yellow("自检前是否先保存当前 .env 修改？[Y/n] > "))
            if ans is None or ans.strip().lower() in {"", "y", "yes"}:
                self._save_env_with_feedback()
        if self.env_doc.path.is_file():
            load_dotenv(dotenv_path=self.env_doc.path, override=True)
        live = self._prompt("是否同时执行联网测试（验证 agy 与 Telegram API）？[y/N] > ")
        do_live = bool(live and live.strip().lower() in {"y", "yes"})
        self._print("")
        asyncio.run(
            run_doctor(
                env_file=self.env_doc.path if self.env_doc.path.is_file() else None,
                live_test=do_live,
                check_telegram=do_live,
            )
        )
        self._pause()

    def _save_and_restart_service(self) -> None:
        if not self._save_env_with_feedback():
            self._pause()
            return

        if os.name == "nt" or shutil.which("systemctl") is None:
            self._print(
                self._c.yellow(
                    "当前环境未检测到 systemd（systemctl）。已保存 .env，请手动重启 Bot 进程使其生效。"
                )
            )
            self._pause()
            return

        user_unit = Path.home() / ".config" / "systemd" / "user" / "agy-telegram-bot.service"
        sys_unit = Path("/etc/systemd/system/agy-telegram-bot.service")

        if user_unit.is_file():
            cmd = ["systemctl", "--user", "restart", "agy-telegram-bot"]
            status_cmd = ["systemctl", "--user", "--no-pager", "status", "agy-telegram-bot"]
        elif sys_unit.is_file():
            cmd = ["sudo", "systemctl", "restart", "agy-telegram-bot"]
            status_cmd = ["sudo", "systemctl", "--no-pager", "status", "agy-telegram-bot"]
        else:
            self._print(
                self._c.yellow(
                    "未检测到已安装的 agy-telegram-bot.service。可先执行 `bash deploy/install.sh` 安装服务。"
                )
            )
            self._pause()
            return

        self._print(self._c.dim(f"正在执行：{' '.join(cmd)}"))
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if proc.returncode == 0:
            self._print(self._c.green("✅ 服务已重启！"))
            st = subprocess.run(status_cmd, capture_output=True, text=True, check=False)
            for ln in (st.stdout or st.stderr).splitlines()[:10]:
                self._print(self._c.dim("  " + ln))
        else:
            self._print(self._c.red(f"❌ 重启失败：\n{proc.stderr or proc.stdout}"))
        self._pause()

    def _show_alias_help(self) -> None:
        self._clear()
        self._print(self._c.bold(self._c.cyan("=== 快捷别名与行内赋值说明 ===\n")))
        self._print("1. 在主界面可直接输入 `KEY=VALUE` 或 `别名=VALUE` 一步修改，例如：")
        self._print(self._c.cyan("   perm=auto"))
        self._print(self._c.cyan("   proxy=socks5://127.0.0.1:1080"))
        self._print(self._c.cyan("   HTTP_PROXY=http://127.0.0.1:7890"))
        self._print("\n2. 内置快捷别名：")
        for spec in SETTING_SPECS:
            if spec.aliases:
                self._print(
                    f"   {_pad_right(', '.join(spec.aliases), 18)} -> {self._c.bold(spec.key)} "
                    f"{self._c.dim(spec.label)}"
                )
        self._print("")
        self._pause()


# ----------------------------------------------------------------------
# CLI Entrypoint (Interactive + Non-Interactive Subcommands)
# ----------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    _enable_windows_ansi()
    parser = argparse.ArgumentParser(
        prog="agy-tg-config",
        description="Interactive & scriptable configurator for AGY Telegram Bot (.env & AGY settings.json)",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help="Path to .env file (defaults to ./.env)",
    )
    parser.add_argument(
        "--agy-settings",
        type=Path,
        default=None,
        help="Path to AGY CLI settings.json (defaults to ~/.gemini/antigravity-cli/settings.json)",
    )

    sub = parser.add_subparsers(dest="command")
    sub.add_parser("wizard", help="Run step-by-step first-time setup wizard")
    sub.add_parser("agy-settings", help="Open interactive editor for AGY CLI settings.json")

    p_list = sub.add_parser("list", help="List all configured settings")
    p_list.add_argument("--show-secrets", action="store_true", help="Print secret values unmasked")

    p_get = sub.add_parser("get", help="Print a single environment variable value")
    p_get.add_argument("key", help="Environment variable name or alias")

    p_set = sub.add_parser("set", help="Validate and set an environment variable in .env")
    p_set.add_argument("key", help="Environment variable name or alias")
    p_set.add_argument("value", help="Value to assign")

    p_unset = sub.add_parser("unset", help="Remove an environment variable from .env")
    p_unset.add_argument("key", help="Environment variable name or alias")

    args = parser.parse_args(argv)
    env_path = _resolve_default_env_path(args.env_file)
    doc = EnvFileDocument.load(env_path)
    cli = InteractiveConfigCLI(doc, agy_settings_path=args.agy_settings)

    if args.command is None:
        return cli.run_interactive()

    if args.command == "wizard":
        return cli.run_wizard()

    if args.command == "agy-settings":
        return cli.run_agy_settings_editor()

    if args.command == "list":
        cli._show_secrets = bool(args.show_secrets)
        cli.render_dashboard()
        return 0

    if args.command == "get":
        spec = cli._alias_map.get(args.key.lower())
        real_key = spec.key if spec is not None else args.key
        if not doc.has_key(real_key):
            print(f"{real_key} is not set in {doc.path}", file=sys.stderr)
            return 1
        print(doc.get(real_key))
        return 0

    if args.command == "set":
        spec = cli._alias_map.get(args.key.lower())
        if spec is not None:
            ok, norm = validate_setting_value(spec, args.value)
            if not ok:
                print(f"[ERROR] {spec.key}: {norm}", file=sys.stderr)
                return 2
            doc.set(spec.key, norm)
            saved_key, saved_val = spec.key, (mask_secret(norm) if spec.kind == "secret" else norm)
        else:
            try:
                doc.set(args.key, args.value)
            except ValueError as exc:
                print(f"[ERROR] {exc}", file=sys.stderr)
                return 2
            saved_key, saved_val = args.key, args.value
        doc.save(chmod_600=True)
        print(f"Updated {saved_key}={saved_val} in {doc.path}")
        return 0

    if args.command == "unset":
        spec = cli._alias_map.get(args.key.lower())
        real_key = spec.key if spec is not None else args.key
        if doc.unset(real_key):
            doc.save(chmod_600=True)
            print(f"Removed {real_key} from {doc.path}")
            return 0
        print(f"{real_key} was not present in {doc.path}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
