"""Round-trip .env file manager, setting schema catalog, and AGY settings.json editor."""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

from .models import VALID_EFFORT_LEVELS, VALID_PERMISSION_MODES, VALID_RUNNER_MODES

GROUP_TITLES: dict[str, str] = {
    "telegram": "Telegram 凭证与访问控制",
    "agy": "AGY CLI 与工作区",
    "security": "权限与安全策略",
    "auth": "认证透传（可选，非 SSH OAuth 时使用）",
    "runtime": "运行限制与交互体验",
}

GROUP_ORDER: tuple[str, ...] = ("telegram", "agy", "security", "auth", "runtime")

_ENV_LINE_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")
_COMMENTED_ENV_RE = re.compile(r"^\s*#\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True, slots=True)
class SettingSpec:
    """Metadata and validation rules for a known Bot `.env` setting."""

    key: str
    group: str
    label: str
    kind: str  # str, secret, bool, int, float, choice, int_list, path, path_list, model
    default: str = ""
    choices: tuple[tuple[str, str], ...] = ()
    required: bool = False
    aliases: tuple[str, ...] = ()
    min_val: float | None = None
    hint: str = ""


SETTING_SPECS: tuple[SettingSpec, ...] = (
    # --- Telegram ---
    SettingSpec(
        key="TELEGRAM_BOT_TOKEN",
        group="telegram",
        label="Bot Token",
        kind="secret",
        required=True,
        aliases=("token", "bot"),
        hint="从 @BotFather 获取，例如 123456789:AAH...",
    ),
    SettingSpec(
        key="ALLOWED_USER_IDS",
        group="telegram",
        label="允许使用的用户 ID 列表",
        kind="int_list",
        required=True,
        aliases=("users", "uid", "allow"),
        hint="逗号分隔的数字 Telegram User ID（可从 @userinfobot 获取）",
    ),
    SettingSpec(
        key="ALLOWED_CHAT_IDS",
        group="telegram",
        label="允许的群组 Chat ID 列表",
        kind="int_list",
        aliases=("chats", "groups"),
        hint="留空则默认允许白名单用户私聊及所在群；填写负数群 ID 可限定指定群组",
    ),
    SettingSpec(
        key="TELEGRAM_PROXY_URL",
        group="telegram",
        label="Telegram API 代理地址",
        kind="str",
        aliases=("proxy",),
        hint="可选，如 socks5://127.0.0.1:1080 或 http://127.0.0.1:7890",
    ),
    # --- AGY CLI ---
    SettingSpec(
        key="AGY_BIN",
        group="agy",
        label="agy 可执行文件路径",
        kind="str",
        default="agy",
        aliases=("bin", "agy"),
        hint="默认 agy（会自动探测 ~/.local/bin/agy）",
    ),
    SettingSpec(
        key="DEFAULT_WORKSPACE",
        group="agy",
        label="默认工作区目录",
        kind="path",
        default="~/workspace",
        aliases=("ws", "workspace", "dir"),
        hint="支持 ~ 展开，新会话默认在此目录启动",
    ),
    SettingSpec(
        key="ALLOWED_WORKSPACE_ROOTS",
        group="agy",
        label="允许访问的根目录白名单",
        kind="path_list",
        aliases=("roots",),
        hint="限制 /cd 与 /get 的目录边界；留空则允许当前系统用户可访问的任意路径",
    ),
    SettingSpec(
        key="AGY_EXTRA_DIRS",
        group="agy",
        label="附加工作区目录 (--add-dir)",
        kind="path_list",
        aliases=("extradirs",),
        hint="逗号分隔，启动 agy 时自动附带 --add-dir",
    ),
    SettingSpec(
        key="AGY_RUNNER_MODE",
        group="agy",
        label="AGY 运行引擎模式",
        kind="choice",
        default="persistent",
        choices=(
            ("persistent", "常驻 stream-json 热进程，连续对话低延迟（推荐）"),
            ("oneshot", "每轮按需启动 agy -p，空闲零内存"),
        ),
        aliases=("runner",),
    ),
    SettingSpec(
        key="AGY_DEFAULT_MODEL",
        group="agy",
        label="默认模型 Slug",
        kind="model",
        default="",
        aliases=("model",),
        hint="留空继承 CLI 默认；支持直接从 `agy models` 选取",
    ),
    SettingSpec(
        key="AGY_DEFAULT_EFFORT",
        group="agy",
        label="默认推理强度 (--effort)",
        kind="choice",
        default="",
        choices=(
            ("", "继承 CLI 默认"),
            ("low", "low"),
            ("medium", "medium"),
            ("high", "high"),
            ("xhigh", "xhigh"),
            ("max", "max"),
        ),
        aliases=("effort",),
    ),
    SettingSpec(
        key="AGY_DEFAULT_AGENT",
        group="agy",
        label="默认自定义 Agent",
        kind="str",
        default="",
        aliases=("agent",),
        hint="留空使用默认 Agent（可用 agy agents 查看）",
    ),
    # --- Security & Permissions ---
    SettingSpec(
        key="AGY_DEFAULT_PERMISSION_MODE",
        group="security",
        label="默认工具权限模式",
        kind="choice",
        default="default",
        choices=(
            ("default", "遵循 ~/.gemini/antigravity-cli/settings.json / CLI 默认（推荐）"),
            ("auto", "--mode auto（自动批准工作区编辑与安全/沙箱命令）"),
            ("accept-edits", "--mode accept-edits（自动批准工作区文件编辑）"),
            ("plan", "--mode plan（只读调研与规划）"),
            ("danger", "--dangerously-skip-permissions（全自动跳过确认）"),
        ),
        aliases=("perm", "mode"),
    ),
    SettingSpec(
        key="ALLOW_DANGEROUS_MODE",
        group="security",
        label="允许远程切换 danger 模式",
        kind="bool",
        default="true",
        aliases=("danger",),
        hint="设为 false 可在生产 VPS 上彻底禁用 /perm danger",
    ),
    SettingSpec(
        key="AGY_DEFAULT_SANDBOX",
        group="security",
        label="默认开启终端 OS 沙箱 (--sandbox)",
        kind="bool",
        default="false",
        aliases=("sandbox",),
    ),
    SettingSpec(
        key="AGY_DISABLE_SLASH_COMMANDS",
        group="security",
        label="禁用 headless 斜杠命令展开",
        kind="bool",
        default="false",
    ),
    SettingSpec(
        key="AGY_CLI_DISABLE_AUTO_UPDATE",
        group="security",
        label="禁用 agy 后台自更新（防 update.lock）",
        kind="bool",
        default="true",
        aliases=("noupdate",),
    ),
    # --- Auth passthrough ---
    SettingSpec(
        key="GEMINI_API_KEY",
        group="auth",
        label="Gemini API Key（可选）",
        kind="secret",
        default="",
        aliases=("apikey", "gemini"),
        hint="使用 API Key 模式时需同时在 AGY settings.json 中设 modelProvider=gemini",
    ),
    SettingSpec(
        key="GOOGLE_GEMINI_BASE_URL",
        group="auth",
        label="自定义 Gemini API Base URL",
        kind="str",
        default="",
        aliases=("baseurl",),
    ),
    SettingSpec(
        key="AGY_ADC_AUTH",
        group="auth",
        label="启用 Google Cloud ADC 认证",
        kind="bool",
        default="",
        aliases=("adc",),
        hint="企业版 / Vertex ADC 场景设为 true",
    ),
    SettingSpec(
        key="GOOGLE_CLOUD_QUOTA_PROJECT",
        group="auth",
        label="GCP Quota Project",
        kind="str",
        default="",
    ),
    SettingSpec(
        key="GOOGLE_CLOUD_LOCATION",
        group="auth",
        label="GCP Region / Location",
        kind="str",
        default="",
    ),
    # --- Runtime ---
    SettingSpec(
        key="TURN_TIMEOUT_SECONDS",
        group="runtime",
        label="单轮超时时间（秒，0=不限）",
        kind="float",
        default="1800",
        min_val=0.0,
        aliases=("timeout",),
    ),
    SettingSpec(
        key="SESSION_IDLE_TIMEOUT_SECONDS",
        group="runtime",
        label="热进程空闲回收时间（秒）",
        kind="float",
        default="600",
        min_val=10.0,
        aliases=("idle",),
    ),
    SettingSpec(
        key="MAX_CONCURRENT_RUNS",
        group="runtime",
        label="全局最大并发任务数",
        kind="int",
        default="3",
        min_val=1.0,
        aliases=("concurrency",),
    ),
    SettingSpec(
        key="STREAM_EDIT_INTERVAL_SECONDS",
        group="runtime",
        label="流式编辑刷新间隔（秒）",
        kind="float",
        default="1.2",
        min_val=0.3,
    ),
    SettingSpec(
        key="DEFAULT_VERBOSE",
        group="runtime",
        label="默认显示工具轨迹与 Token 摘要",
        kind="bool",
        default="true",
        aliases=("verbose",),
    ),
    SettingSpec(
        key="AUTO_SEND_IMAGES",
        group="runtime",
        label="自动发送对话中生成/引用的图片",
        kind="bool",
        default="true",
        aliases=("images", "autoimg"),
    ),
    SettingSpec(
        key="MAX_AUTO_IMAGES",
        group="runtime",
        label="单轮最多自动发送图片数量",
        kind="int",
        default="8",
        min_val=1.0,
    ),
    SettingSpec(
        key="LONG_OUTPUT_FILE_THRESHOLD",
        group="runtime",
        label="超长回复附带 .md 阈值（字符，0=禁用）",
        kind="int",
        default="12000",
        min_val=0.0,
    ),
    SettingSpec(
        key="MAX_UPLOAD_MB",
        group="runtime",
        label="Telegram 上传附件大小上限（MB）",
        kind="int",
        default="20",
        min_val=1.0,
    ),
    SettingSpec(
        key="DB_PATH",
        group="runtime",
        label="SQLite 会话数据库路径",
        kind="str",
        default="./data/bot.db",
        aliases=("db",),
    ),
    SettingSpec(
        key="LOG_LEVEL",
        group="runtime",
        label="日志级别",
        kind="choice",
        default="INFO",
        choices=(
            ("DEBUG", "DEBUG（详细排障）"),
            ("INFO", "INFO（推荐）"),
            ("WARNING", "WARNING"),
            ("ERROR", "ERROR"),
        ),
        aliases=("log",),
    ),
)

SPECS_BY_KEY: dict[str, SettingSpec] = {s.key: s for s in SETTING_SPECS}


def mask_secret(value: str) -> str:
    """Safely mask tokens and API keys for terminal display."""
    cleaned = value.strip()
    if not cleaned:
        return "(未设置)"
    if cleaned.startswith("123456789:replace-"):
        return "(示例占位符 — 请修改!)"
    if len(cleaned) <= 10:
        return "*" * len(cleaned)
    return f"{cleaned[:5]}…{cleaned[-4:]} ({len(cleaned)} chars)"


def format_env_value(value: str) -> str:
    """Serialize a `.env` value with safe quoting when special characters are present."""
    if value == "":
        return ""
    needs_quotes = any(ch in value for ch in (" ", "\t", "#", "'", '"', "\n", "\r", "$"))
    if not needs_quotes:
        return value
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
    )
    return f'"{escaped}"'


def validate_setting_value(spec: SettingSpec, value: str) -> tuple[bool, str]:
    """Validate a single setting value against its SettingSpec. Returns (ok, normalized_or_error)."""
    raw = value.strip()

    if raw == "":
        if spec.required:
            return False, f"{spec.key} 为必填项，不能为空。"
        return True, ""

    if spec.kind == "secret":
        if spec.key == "TELEGRAM_BOT_TOKEN":
            if raw.startswith("123456789:replace-"):
                return False, "不能使用示例占位符 Token。"
            if ":" not in raw or len(raw) < 20:
                return False, "Bot Token 格式通常为 `<数字>:<密钥>`（来自 @BotFather）。"
        return True, raw

    if spec.kind == "bool":
        low = raw.lower()
        if low in {"1", "true", "yes", "on", "y"}:
            return True, "true"
        if low in {"0", "false", "no", "off", "n"}:
            return True, "false"
        return False, "请输入 true 或 false。"

    if spec.kind == "int":
        try:
            num = int(raw)
        except ValueError:
            return False, "请输入整数。"
        if spec.min_val is not None and num < spec.min_val:
            return False, f"数值必须 >= {int(spec.min_val)}。"
        return True, str(num)

    if spec.kind == "float":
        try:
            num_f = float(raw)
        except ValueError:
            return False, "请输入数字。"
        if spec.min_val is not None and num_f < spec.min_val:
            return False, f"数值必须 >= {spec.min_val}。"
        # Preserve clean integer-like formatting when whole
        if num_f.is_integer() and "." not in raw:
            return True, str(int(num_f))
        return True, str(num_f)

    if spec.kind == "choice":
        allowed = {val for val, _ in spec.choices}
        norm = raw.upper() if spec.key == "LOG_LEVEL" else raw.lower()
        if norm not in allowed:
            return False, f"无效选项，可选：{', '.join(a or '(空)' for a in allowed)}"
        return True, norm

    if spec.kind == "int_list":
        items: list[str] = []
        seen: set[int] = set()
        for part in raw.split(","):
            p = part.strip()
            if not p:
                continue
            try:
                num = int(p)
            except ValueError:
                return False, f"包含非数字 ID：{p!r}"
            if num not in seen:
                seen.add(num)
                items.append(str(num))
        if spec.required and not items:
            return False, f"{spec.key} 至少需要一个有效的数字 ID。"
        return True, ",".join(items)

    if spec.kind == "path_list":
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        return True, ",".join(parts)

    return True, raw


class EnvFileDocument:
    """Comment-preserving `.env` parser and atomic writer."""

    def __init__(self, path: Path, lines: list[str], values: dict[str, str]) -> None:
        self.path = path
        self._lines = lines
        self._values = values
        self.dirty = False

    @classmethod
    def load(cls, path: Path, *, template_path: Path | None = None) -> EnvFileDocument:
        """Load `.env` from `path`, or initialize from `.env.example` template if missing."""
        if path.is_file():
            raw_text = path.read_text(encoding="utf-8")
            lines = raw_text.splitlines()
            parsed_raw = dotenv_values(path)
            values = {k: (v if v is not None else "") for k, v in parsed_raw.items()}
            return cls(path, lines, values)

        if template_path is None:
            candidate = path.parent / ".env.example"
            if candidate.is_file():
                template_path = candidate

        if template_path is not None and template_path.is_file():
            raw_text = template_path.read_text(encoding="utf-8")
            lines = raw_text.splitlines()
            parsed_raw = dotenv_values(template_path)
            values = {k: (v if v is not None else "") for k, v in parsed_raw.items()}
            doc = cls(path, lines, values)
            doc.dirty = True
            return doc

        doc = cls(path, ["# AGY Telegram Bot Configuration", ""], {})
        doc.dirty = True
        return doc

    def get(self, key: str, default: str = "") -> str:
        return self._values.get(key, default)

    def has_key(self, key: str) -> bool:
        return key in self._values

    def keys(self) -> list[str]:
        return list(self._values.keys())

    def extra_keys(self) -> list[str]:
        """Return keys present in `.env` that are not part of the built-in SETTING_SPECS."""
        return [k for k in self._values.keys() if k not in SPECS_BY_KEY]

    def set(self, key: str, value: str) -> None:
        """Set or uncomment `key=value` in-place, preserving comments and file layout."""
        if not _ENV_KEY_RE.match(key):
            raise ValueError(f"无效环境变量名：{key!r}（仅允许字母、数字与下划线，且不能以数字开头）")

        serialized = f"{key}={format_env_value(value)}"
        self._values[key] = value
        self.dirty = True

        # 1. Replace existing active line
        for idx, line in enumerate(self._lines):
            m = _ENV_LINE_RE.match(line)
            if m and m.group(1) == key:
                self._lines[idx] = serialized
                return

        # 2. Uncomment existing commented template line (e.g. `# GEMINI_API_KEY=`)
        for idx, line in enumerate(self._lines):
            m = _COMMENTED_ENV_RE.match(line)
            if m and m.group(1) == key:
                self._lines[idx] = serialized
                return

        # 3. Append new line
        if self._lines and self._lines[-1].strip() != "":
            self._lines.append("")
        self._lines.append(serialized)

    def unset(self, key: str) -> bool:
        """Remove `key` from `.env`. Returns True if removed."""
        existed = key in self._values
        self._values.pop(key, None)
        new_lines: list[str] = []
        removed_line = False
        for line in self._lines:
            m = _ENV_LINE_RE.match(line)
            if m and m.group(1) == key:
                removed_line = True
                continue
            new_lines.append(line)
        if removed_line or existed:
            self._lines = new_lines
            self.dirty = True
            return True
        return False

    def validate_all(self) -> list[str]:
        """Run cross-field validation mirroring config.py rules."""
        issues: list[str] = []
        for spec in SETTING_SPECS:
            val = self.get(spec.key, "")
            ok, msg = validate_setting_value(spec, val)
            if not ok:
                issues.append(f"{spec.key}: {msg}")

        allow_danger = (self.get("ALLOW_DANGEROUS_MODE") or "true").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        perm = (self.get("AGY_DEFAULT_PERMISSION_MODE") or "default").strip().lower()
        if perm == "danger" and not allow_danger:
            issues.append(
                "AGY_DEFAULT_PERMISSION_MODE=danger 与 ALLOW_DANGEROUS_MODE=false 冲突。"
            )

        roots_raw = self.get("ALLOWED_WORKSPACE_ROOTS", "").strip()
        ws_raw = self.get("DEFAULT_WORKSPACE", "").strip()
        if roots_raw and ws_raw:
            try:
                ws_path = Path(ws_raw).expanduser().resolve()
                roots = [Path(r.strip()).expanduser().resolve() for r in roots_raw.split(",") if r.strip()]
                if roots and not any(ws_path == r or r in ws_path.parents for r in roots):
                    issues.append(
                        f"DEFAULT_WORKSPACE ({ws_path}) 不在 ALLOWED_WORKSPACE_ROOTS 范围内。"
                    )
            except OSError:
                pass

        return issues

    def render(self) -> str:
        text = "\n".join(self._lines)
        return text + ("\n" if not text.endswith("\n") else "")

    def save(self, *, chmod_600: bool = True) -> Path:
        """Atomically write `.env` with restrictive POSIX permissions (0600)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        content = self.render()
        fd, tmp_name = tempfile.mkstemp(
            prefix=".env.tmp.", dir=str(self.path.parent), text=True
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(content)
            if chmod_600 and os.name != "nt":
                os.chmod(tmp_name, 0o600)
            os.replace(tmp_name, self.path)
            if chmod_600 and os.name != "nt":
                os.chmod(self.path, 0o600)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        self.dirty = False
        return self.path


AGY_AGENT_MODES: tuple[tuple[str, str], ...] = (
    ("", "默认（CLI 自动选择 auto / default）"),
    ("auto", "auto（自动批准工作区编辑与安全/沙箱命令）"),
    ("accept-edits", "accept-edits（自动批准文件编辑）"),
    ("plan", "plan（只读调研规划）"),
)

AGY_TOOL_PERMISSIONS: tuple[tuple[str, str], ...] = (
    ("request-review", "request-review（写文件/命令默认请求审批）"),
    ("proceed-in-sandbox", "proceed-in-sandbox（沙箱内命令免审批）"),
    ("always-proceed", "always-proceed（除 ask/deny 规则外免审批）"),
    ("strict", "strict（所有工具含读取均请求审批）"),
)


class AgySettingsDocument:
    """Safe reader & atomic writer for `~/.gemini/antigravity-cli/settings.json`."""

    def __init__(self, path: Path, data: dict[str, Any]) -> None:
        self.path = path
        self.data = data
        self.dirty = False

    @classmethod
    def default_path(cls) -> Path:
        return Path.home() / ".gemini" / "antigravity-cli" / "settings.json"

    @classmethod
    def load(cls, path: Path | None = None) -> AgySettingsDocument:
        target = path or cls.default_path()
        if target.is_file():
            raw = json.loads(target.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError(f"{target} 顶层必须是 JSON 对象。")
            return cls(target, raw)
        return cls(target, {})

    def get_permissions_list(self, bucket: str) -> list[str]:
        perms = self.data.get("permissions")
        if not isinstance(perms, dict):
            return []
        raw = perms.get(bucket)
        if not isinstance(raw, list):
            return []
        return [str(x) for x in raw]

    def set_permissions_list(self, bucket: str, rules: list[str]) -> None:
        perms = self.data.get("permissions")
        if not isinstance(perms, dict):
            perms = {}
            self.data["permissions"] = perms
        if rules:
            perms[bucket] = rules
        else:
            perms.pop(bucket, None)
            if not perms:
                self.data.pop("permissions", None)
        self.dirty = True

    def set_field(self, key: str, value: Any) -> None:
        if value is None or value == "":
            if key in self.data:
                self.data.pop(key, None)
                self.dirty = True
        else:
            self.data[key] = value
            self.dirty = True

    def save(self) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(self.data, indent=2, ensure_ascii=False) + "\n"
        fd, tmp_name = tempfile.mkstemp(
            prefix="settings.json.tmp.", dir=str(self.path.parent), text=True
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(content)
            os.replace(tmp_name, self.path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        self.dirty = False
        return self.path
