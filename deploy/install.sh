#!/usr/bin/env bash
set -euo pipefail

# =============================================================================
# AGY Telegram Bot — Interactive Linux VPS Installer
#
# Usage:
#   bash deploy/install.sh                     # Interactive guided installer (recommended)
#   bash deploy/install.sh --yes --user        # Non-interactive user systemd install
#   bash deploy/install.sh --yes --system      # Non-interactive system systemd install
#   bash deploy/install.sh --yes --no-service  # Non-interactive venv-only install
#   bash deploy/install.sh --with-proxy        # Also install httpx[socks]
#   bash deploy/install.sh --uninstall         # Stop and remove installed systemd units
# =============================================================================

INTERACTIVE=1
SERVICE_MODE=""
WITH_PROXY=0
SKIP_WIZARD=0
SKIP_START=0
UNINSTALL=0

for arg in "$@"; do
  case "$arg" in
    -y|--yes|--non-interactive) INTERACTIVE=0 ;;
    --user) SERVICE_MODE="user" ;;
    --system) SERVICE_MODE="system" ;;
    --no-service) SERVICE_MODE="none" ;;
    --with-proxy) WITH_PROXY=1 ;;
    --skip-wizard) SKIP_WIZARD=1 ;;
    --skip-start) SKIP_START=1 ;;
    --uninstall) UNINSTALL=1 ;;
    -h|--help)
      cat <<'EOF'
AGY Telegram Bot — Interactive Linux VPS Installer

Usage:
  bash deploy/install.sh [options]

Options:
  -y, --yes, --non-interactive  Run without interactive prompts (uses defaults/flags)
  --user                        Install systemd --user service (recommended for OAuth)
  --system                      Install system-wide /etc/systemd/system/ service (sudo)
  --no-service                  Only install venv, CLI links, and .env
  --with-proxy                  Also install SOCKS5 proxy support (httpx[socks])
  --skip-wizard                 Skip launching agy-tg-config during installation
  --skip-start                  Do not automatically start systemd service at the end
  --uninstall                   Stop and disable installed systemd services
  -h, --help                    Show this help message
EOF
      exit 0
      ;;
    *)
      echo "[ERROR] Unknown option: $arg" >&2
      exit 1
      ;;
  esac
done

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CURRENT_USER="$(id -un)"
CURRENT_HOME="$HOME"
LOCAL_BIN="$CURRENT_HOME/.local/bin"
export PATH="$LOCAL_BIN:$PATH"

# Detect TTY (supports `curl ... | bash` by attaching /dev/tty)
TTY_FD="/dev/tty"
if [[ "$INTERACTIVE" -eq 1 ]]; then
  if [[ ! -r "$TTY_FD" || ! -w "$TTY_FD" ]]; then
    INTERACTIVE=0
  fi
fi

# ANSI colors when stdout is a terminal
if [[ -t 1 && -z "${NO_COLOR:-}" ]]; then
  C_BOLD=$'\033[1m'
  C_DIM=$'\033[2m'
  C_CYAN=$'\033[36m'
  C_GREEN=$'\033[32m'
  C_YELLOW=$'\033[33m'
  C_RED=$'\033[31m'
  C_RESET=$'\033[0m'
else
  C_BOLD="" C_DIM="" C_CYAN="" C_GREEN="" C_YELLOW="" C_RED="" C_RESET=""
fi

info()    { echo "${C_CYAN}[INFO]${C_RESET} $*"; }
ok()      { echo "${C_GREEN}[OK]${C_RESET}   $*"; }
warn()    { echo "${C_YELLOW}[WARN]${C_RESET} $*"; }
err()     { echo "${C_RED}[ERROR]${C_RESET} $*" >&2; }
section() { echo; echo "${C_BOLD}${C_CYAN}━━━ $* ━━━${C_RESET}"; }

ask_input() {
  local prompt="$1"
  local default_val="${2:-}"
  local ans=""
  if [[ "$INTERACTIVE" -eq 1 ]]; then
    printf "%s" "$prompt" >"$TTY_FD"
    IFS= read -r ans <"$TTY_FD" || true
  fi
  if [[ -z "$ans" ]]; then
    ans="$default_val"
  fi
  printf "%s" "$ans"
}

ask_yes_no() {
  local prompt="$1"
  local default_yn="${2:-y}"  # y or n
  if [[ "$INTERACTIVE" -eq 0 ]]; then
    [[ "$default_yn" == "y" ]]
    return
  fi
  local suffix="[Y/n]"
  [[ "$default_yn" == "n" ]] && suffix="[y/N]"
  local ans
  ans="$(ask_input "${C_BOLD}${prompt} ${suffix} > ${C_RESET}" "$default_yn")"
  case "${ans,,}" in
    y|yes) return 0 ;;
    n|no)  return 1 ;;
    *)     [[ "$default_yn" == "y" ]] ;;
  esac
}

# -----------------------------------------------------------------------------
# Uninstall Flow
# -----------------------------------------------------------------------------
if [[ "$UNINSTALL" -eq 1 ]]; then
  section "卸载 AGY Telegram Bot 系统服务"
  USER_UNIT="$CURRENT_HOME/.config/systemd/user/agy-telegram-bot.service"
  SYS_UNIT="/etc/systemd/system/agy-telegram-bot.service"

  if [[ -f "$USER_UNIT" ]]; then
    systemctl --user disable --now agy-telegram-bot.service 2>/dev/null || true
    rm -f "$USER_UNIT"
    systemctl --user daemon-reload 2>/dev/null || true
    ok "已移除用户级服务：$USER_UNIT"
  fi

  if [[ -f "$SYS_UNIT" ]]; then
    sudo systemctl disable --now agy-telegram-bot.service 2>/dev/null || true
    sudo rm -f "$SYS_UNIT"
    sudo systemctl daemon-reload 2>/dev/null || true
    ok "已移除系统级服务：$SYS_UNIT"
  fi

  for bin_name in agy-tg-bot agy-tg-config agy-tg-doctor agy-tg-imgsearch; do
    if [[ -L "$LOCAL_BIN/$bin_name" ]]; then
      rm -f "$LOCAL_BIN/$bin_name"
      ok "已移除命令软链：$LOCAL_BIN/$bin_name"
    fi
  done

  echo
  info "代码目录、.env 配置与 data/bot.db 均已保留；如需彻底删除可手动执行：rm -rf $APP_DIR"
  exit 0
fi

# -----------------------------------------------------------------------------
# Banner
# -----------------------------------------------------------------------------
echo "${C_BOLD}${C_CYAN}============================================================${C_RESET}"
echo "${C_BOLD}  AGY Telegram Bot — Linux VPS 交互式安装向导${C_RESET}"
echo "${C_BOLD}${C_CYAN}============================================================${C_RESET}"
echo "  安装目录 : $APP_DIR"
echo "  当前用户 : $CURRENT_USER"
echo "  交互模式 : $([[ "$INTERACTIVE" -eq 1 ]] && echo "开启" || echo "静默/非交互")"

# -----------------------------------------------------------------------------
# Step 1: Python 3.10+ & venv check
# -----------------------------------------------------------------------------
section "步骤 1/7：检查 Python 3.10+ 环境"

PYTHON_BIN=""
for cand in python3 python3.14 python3.13 python3.12 python3.11 python3.10; do
  if command -v "$cand" >/dev/null 2>&1; then
    if "$cand" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
      PYTHON_BIN="$cand"
      break
    fi
  fi
done

if [[ -z "$PYTHON_BIN" ]]; then
  err "未找到 Python 3.10+。请先安装 python3 与 python3-venv。"
  exit 1
fi
ok "检测到 Python：$($PYTHON_BIN --version)"

# Verify venv / ensurepip availability (Debian/Ubuntu often omits python3-venv)
if ! "$PYTHON_BIN" -c 'import venv, ensurepip' >/dev/null 2>&1; then
  warn "当前 Python 缺少 venv/ensurepip 模块。"
  if command -v apt-get >/dev/null 2>&1 && ask_yes_no "是否使用 apt 自动安装 python3-venv？" "y"; then
    PY_MINOR="$("$PYTHON_BIN" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
    sudo apt-get update
    sudo apt-get install -y "python${PY_MINOR}-venv" || sudo apt-get install -y python3-venv
  else
    err "请先安装 python3-venv 后重新运行本脚本。"
    exit 1
  fi
fi

# -----------------------------------------------------------------------------
# Step 2: Check / Install Antigravity CLI (`agy`)
# -----------------------------------------------------------------------------
section "步骤 2/7：检查 Antigravity CLI (agy)"

if command -v agy >/dev/null 2>&1; then
  ok "已检测到 agy CLI：$(command -v agy)"
else
  warn "未检测到 agy CLI（官方默认安装位置为 ~/.local/bin/agy）。"
  if ask_yes_no "是否现在自动安装 Google Antigravity CLI (agy)？" "y"; then
    if ! command -v curl >/dev/null 2>&1; then
      err "缺少 curl，无法下载官方安装脚本。请先安装 curl。"
      exit 1
    fi
    info "正在执行官方安装脚本..."
    curl -fsSL https://antigravity.google/cli/install.sh | bash
    export PATH="$LOCAL_BIN:$PATH"
    if command -v agy >/dev/null 2>&1; then
      ok "agy CLI 安装成功：$(command -v agy)"
    else
      warn "安装脚本已执行，但当前 PATH 尚未发现 agy，请确认 ~/.local/bin/agy 是否存在。"
    fi
  else
    warn "跳过 agy 安装。后续可运行：curl -fsSL https://antigravity.google/cli/install.sh | bash"
  fi
fi

# -----------------------------------------------------------------------------
# Step 3: Create venv, install package & link CLI commands
# -----------------------------------------------------------------------------
section "步骤 3/7：创建虚拟环境并安装 Bot 依赖"

if [[ "$WITH_PROXY" -eq 0 && "$INTERACTIVE" -eq 1 ]]; then
  if ask_yes_no "VPS 是否需要通过 SOCKS5 代理访问 Telegram API（额外安装 httpx[socks]）？" "n"; then
    WITH_PROXY=1
  fi
fi

info "创建/更新虚拟环境：$APP_DIR/.venv"
"$PYTHON_BIN" -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip

if [[ "$WITH_PROXY" -eq 1 ]]; then
  info "安装 agy-telegram-bot（含 SOCKS5 代理支持）..."
  "$APP_DIR/.venv/bin/pip" install -e "$APP_DIR[proxy]"
else
  info "安装 agy-telegram-bot..."
  "$APP_DIR/.venv/bin/pip" install -e "$APP_DIR"
fi
ok "Python 依赖安装完成。"

mkdir -p "$LOCAL_BIN"
for bin_name in agy-tg-bot agy-tg-config agy-tg-doctor agy-tg-imgsearch; do
  ln -sf "$APP_DIR/.venv/bin/$bin_name" "$LOCAL_BIN/$bin_name"
done
ok "已将 agy-tg-bot / agy-tg-config / agy-tg-doctor / agy-tg-imgsearch 软链到 $LOCAL_BIN"

# Install bundled AGY skills (web-image-search, fast-image-analysis) and safe allowlist rules
if [[ -d "$APP_DIR/skills" ]]; then
  AGY_SKILLS_DIR="$CURRENT_HOME/.gemini/antigravity-cli/skills"
  mkdir -p "$AGY_SKILLS_DIR"
  cp -r "$APP_DIR/skills/"* "$AGY_SKILLS_DIR/"
  ok "已安装内置 AGY Skills 到 $AGY_SKILLS_DIR（联网搜图 + 快速图像分析）"
fi

"$APP_DIR/.venv/bin/python" -c '
from agy_tg_bot.env_editor import AgySettingsDocument
doc = AgySettingsDocument.load()
allow = doc.get_permissions_list("allow")
changed = False
for rule in ("command(agy-tg-imgsearch)", "command(exiftool)"):
    if rule not in allow:
        allow.append(rule)
        changed = True
if changed:
    doc.set_permissions_list("allow", allow)
    doc.save()
' && ok "已放行 agy-tg-imgsearch 与 exiftool 安全命令规则"

# Offer to persist ~/.local/bin in shell profile if missing
if [[ ":$PATH:" != *":$LOCAL_BIN:"* ]] || ! grep -qs '\.local/bin' "$CURRENT_HOME/.bashrc" "$CURRENT_HOME/.zshrc" 2>/dev/null; then
  SHELL_RC="$CURRENT_HOME/.bashrc"
  [[ "${SHELL:-}" == *zsh* ]] && SHELL_RC="$CURRENT_HOME/.zshrc"
  if [[ -f "$SHELL_RC" ]] && ! grep -qs '\.local/bin' "$SHELL_RC" 2>/dev/null; then
    if ask_yes_no "是否将 ~/.local/bin 写入 $SHELL_RC 以便任意目录直接运行 agy 与 agy-tg-config？" "y"; then
      printf '\nexport PATH="$HOME/.local/bin:$PATH"\n' >>"$SHELL_RC"
      ok "已写入 $SHELL_RC（重新打开终端或执行 source $SHELL_RC 生效）"
    fi
  fi
fi

# Prepare .env with strict permissions
if [[ ! -f "$APP_DIR/.env" ]]; then
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  chmod 600 "$APP_DIR/.env"
  ok "已初始化 $APP_DIR/.env（权限 600）"
else
  chmod 600 "$APP_DIR/.env" || true
  ok "保留现有 $APP_DIR/.env（权限 600）"
fi

# -----------------------------------------------------------------------------
# Step 4: AGY Authentication Setup
# -----------------------------------------------------------------------------
section "步骤 4/7：配置 AGY 认证方式"

if [[ "$INTERACTIVE" -eq 1 ]]; then
  echo "  请选择你的 VPS 上 agy CLI 的认证方式："
  echo "    [1] 个人 Google 账号（SSH OAuth，复用 Pro/Ultra 订阅，推荐）"
  echo "    [2] Gemini API Key（无头服务器最省心，自动同步 settings.json）"
  echo "    [3] Google Cloud ADC（Gemini Enterprise / Vertex）"
  echo "    [4] 已完成认证 / 稍后自行配置"
  AUTH_CHOICE="$(ask_input "${C_BOLD}请选择 [1-4，默认 4] > ${C_RESET}" "4")"

  case "$AUTH_CHOICE" in
    1)
      if command -v agy >/dev/null 2>&1; then
        if ask_yes_no "是否现在启动一次交互式 agy 完成 SSH OAuth 登录？（登录成功后按 Ctrl+D 返回安装）" "y"; then
          agy <"$TTY_FD" >"$TTY_FD" 2>&1 || true
          ok "已完成 agy 登录步骤。"
        fi
      else
        warn "尚未安装 agy，请安装后手动运行 agy 完成 OAuth 登录。"
      fi
      ;;
    2)
      API_KEY="$(ask_input "${C_BOLD}请输入 GEMINI_API_KEY（回车跳过，稍后在配置向导中填）> ${C_RESET}" "")"
      if [[ -n "$API_KEY" ]]; then
        "$APP_DIR/.venv/bin/agy-tg-config" --env-file "$APP_DIR/.env" set GEMINI_API_KEY "$API_KEY"
      fi
      "$APP_DIR/.venv/bin/python" -c '
from agy_tg_bot.env_editor import AgySettingsDocument
doc = AgySettingsDocument.load()
doc.set_field("modelProvider", "gemini")
doc.save()
print(f"[OK]   已写入 {doc.path} (modelProvider=gemini)")
'
      ;;
    3)
      "$APP_DIR/.venv/bin/agy-tg-config" --env-file "$APP_DIR/.env" set AGY_ADC_AUTH true
      info "请确认已执行：gcloud auth application-default login --project <你的GCP项目ID>"
      ;;
    *)
      info "跳过认证配置（随时可通过 agy-tg-config 或运行 agy 完成）。"
      ;;
  esac
else
  info "非交互模式：跳过认证引导。"
fi

# -----------------------------------------------------------------------------
# Step 5: Interactive Bot Configuration (agy-tg-config)
# -----------------------------------------------------------------------------
section "步骤 5/7：配置 Telegram Bot 与环境变量"

if [[ "$INTERACTIVE" -eq 1 && "$SKIP_WIZARD" -eq 0 ]]; then
  echo "  请选择配置方式："
  echo "    [1] 6 步快速配置向导（Token / 用户白名单 / 工作区 / 权限模式 / 代理，推荐）"
  echo "    [2] 完整交互式设置与变量面板（可编辑全部 31 项变量 + AGY settings.json）"
  echo "    [3] 跳过（保留当前 .env，稍后手动运行 agy-tg-config）"
  CFG_CHOICE="$(ask_input "${C_BOLD}请选择 [1-3，默认 1] > ${C_RESET}" "1")"
  case "$CFG_CHOICE" in
    1) "$APP_DIR/.venv/bin/agy-tg-config" --env-file "$APP_DIR/.env" wizard <"$TTY_FD" >"$TTY_FD" 2>&1 || true ;;
    2) "$APP_DIR/.venv/bin/agy-tg-config" --env-file "$APP_DIR/.env" <"$TTY_FD" >"$TTY_FD" 2>&1 || true ;;
    *) info "跳过配置向导。" ;;
  esac
else
  info "跳过交互式配置向导（可随时运行 agy-tg-config 进行编辑）。"
fi

# -----------------------------------------------------------------------------
# Step 6: Install systemd service
# -----------------------------------------------------------------------------
section "步骤 6/7：安装 systemd 后台守护进程"

if [[ -z "$SERVICE_MODE" ]]; then
  if [[ "$INTERACTIVE" -eq 1 ]]; then
    echo "  请选择守护进程安装模式："
    echo "    [1] systemd --user 用户级服务 + enable-linger（推荐，兼容 SSH OAuth Keyring）"
    echo "    [2] 系统级 /etc/systemd/system/ 服务（需要 sudo，适合 API Key / ADC）"
    echo "    [3] 不安装服务（仅前台运行）"
    SVC_CHOICE="$(ask_input "${C_BOLD}请选择 [1-3，默认 1] > ${C_RESET}" "1")"
    case "$SVC_CHOICE" in
      1) SERVICE_MODE="user" ;;
      2) SERVICE_MODE="system" ;;
      3) SERVICE_MODE="none" ;;
      *) SERVICE_MODE="user" ;;
    esac
  else
    SERVICE_MODE="user"
  fi
fi

INSTALLED_SERVICE=""
if [[ "$SERVICE_MODE" == "user" ]]; then
  USER_UNIT_DIR="$CURRENT_HOME/.config/systemd/user"
  mkdir -p "$USER_UNIT_DIR"
  UNIT_DST="$USER_UNIT_DIR/agy-telegram-bot.service"
  sed -e "s|__APP_DIR__|$APP_DIR|g" "$APP_DIR/deploy/agy-telegram-bot-user.service" > "$UNIT_DST"
  ok "已安装用户级 systemd 服务：$UNIT_DST"

  if command -v loginctl >/dev/null 2>&1; then
    info "启用 loginctl enable-linger，确保 SSH 断开后服务持续后台运行..."
    loginctl enable-linger "$CURRENT_USER" 2>/dev/null \
      || sudo loginctl enable-linger "$CURRENT_USER" 2>/dev/null \
      || warn "无法自动启用 linger，请手动执行：sudo loginctl enable-linger $CURRENT_USER"
  fi

  if systemctl --user daemon-reload 2>/dev/null; then
    systemctl --user enable agy-telegram-bot.service 2>/dev/null || true
    INSTALLED_SERVICE="user"
    ok "用户级服务已注册并设为开机自启。"
  else
    warn "当前 shell 未连接到 systemd --user 总线（常见于刚开启 linger 或 su 切换用户）。"
    warn "请重新 SSH 登录后运行：systemctl --user enable --now agy-telegram-bot"
    warn "或改用系统级服务：bash $APP_DIR/deploy/install.sh --system"
  fi

elif [[ "$SERVICE_MODE" == "system" ]]; then
  TMP_UNIT="$(mktemp)"
  sed \
    -e "s|__APP_DIR__|$APP_DIR|g" \
    -e "s|__USER__|$CURRENT_USER|g" \
    -e "s|__HOME__|$CURRENT_HOME|g" \
    "$APP_DIR/deploy/agy-telegram-bot.service" > "$TMP_UNIT"
  sudo mv "$TMP_UNIT" /etc/systemd/system/agy-telegram-bot.service
  sudo chmod 644 /etc/systemd/system/agy-telegram-bot.service
  sudo systemctl daemon-reload
  sudo systemctl enable agy-telegram-bot.service
  INSTALLED_SERVICE="system"
  ok "已安装系统级服务：/etc/systemd/system/agy-telegram-bot.service"
else
  info "未安装 systemd 服务。"
fi

# -----------------------------------------------------------------------------
# Step 7: Doctor Check & Start Service
# -----------------------------------------------------------------------------
section "步骤 7/7：环境自检与启动服务"

if [[ "$INTERACTIVE" -eq 1 ]]; then
  if ask_yes_no "是否立即运行环境自检（agy-tg-doctor --live-test --check-telegram）？" "y"; then
    "$APP_DIR/.venv/bin/agy-tg-doctor" --env-file "$APP_DIR/.env" --live-test --check-telegram || \
      warn "自检存在未通过项，可随时执行 agy-tg-config 修正后重测。"
  fi
fi

if [[ -n "$INSTALLED_SERVICE" && "$SKIP_START" -eq 0 ]]; then
  if [[ "$INTERACTIVE" -eq 0 ]] || ask_yes_no "是否立即启动/重启 agy-telegram-bot 后台服务？" "y"; then
    if [[ "$INSTALLED_SERVICE" == "user" ]]; then
      systemctl --user restart agy-telegram-bot.service || warn "启动失败，请查看日志。"
      systemctl --user --no-pager status agy-telegram-bot.service | head -n 12 || true
    else
      sudo systemctl restart agy-telegram-bot.service || warn "启动失败，请查看日志。"
      sudo systemctl --no-pager status agy-telegram-bot.service | head -n 12 || true
    fi
  fi
fi

echo
echo "${C_BOLD}${C_GREEN}🎉 安装流程完成！常用运维命令：${C_RESET}"
echo "  • 交互式修改配置与变量 : agy-tg-config"
echo "  • 运行 VPS 环境自检    : agy-tg-doctor --live-test --check-telegram"
if [[ "$SERVICE_MODE" == "user" ]]; then
  echo "  • 查看实时日志         : journalctl --user -u agy-telegram-bot -f"
  echo "  • 重启服务             : systemctl --user restart agy-telegram-bot"
elif [[ "$SERVICE_MODE" == "system" ]]; then
  echo "  • 查看实时日志         : sudo journalctl -u agy-telegram-bot -f"
  echo "  • 重启服务             : sudo systemctl restart agy-telegram-bot"
else
  echo "  • 前台启动             : agy-tg-bot --env-file $APP_DIR/.env"
fi
echo "  • 卸载后台服务         : bash $APP_DIR/deploy/install.sh --uninstall"
