#!/usr/bin/env bash
set -euo pipefail

# =============================================================================
# AGY Telegram Bot — Bootstrap & Interactive VPS Installer Entry
#
# Supports BOTH:
#   1) Local checkout:  bash install.sh [options]
#   2) One-line curl:   curl -fsSL https://raw.githubusercontent.com/PeterGong2008/agy-little-bot/main/install.sh | bash
#                       (override repo via AGY_TG_BOT_REPO="https://github.com/<USER>/<REPO>.git")
# =============================================================================

DEFAULT_REPO_URL="${AGY_TG_BOT_REPO:-https://github.com/PeterGong2008/agy-little-bot.git}"
DEFAULT_INSTALL_DIR="${AGY_TG_BOT_DIR:-$HOME/agy-little-bot}"

# Detect if we are already inside a repository checkout
SCRIPT_DIR=""
if [[ -n "${BASH_SOURCE[0]:-}" && -f "${BASH_SOURCE[0]}" ]]; then
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi

if [[ -n "$SCRIPT_DIR" && -f "$SCRIPT_DIR/deploy/install.sh" && -f "$SCRIPT_DIR/pyproject.toml" ]]; then
  exec bash "$SCRIPT_DIR/deploy/install.sh" "$@"
fi

# Standalone bootstrap mode (e.g., `curl -fsSL ... | bash`)
TTY_IN="/dev/tty"
HAS_TTY=0
if [[ -r "$TTY_IN" && -w "$TTY_IN" ]]; then
  HAS_TTY=1
fi

prompt_tty() {
  local message="$1"
  local default_val="${2:-}"
  local reply=""
  if [[ "$HAS_TTY" -eq 1 ]]; then
    printf "%s" "$message" >"$TTY_IN"
    IFS= read -r reply <"$TTY_IN" || true
  fi
  if [[ -z "$reply" ]]; then
    reply="$default_val"
  fi
  printf "%s" "$reply"
}

echo "============================================================"
echo "  AGY Telegram Bot — Linux VPS 引导安装程序"
echo "============================================================"
echo

if ! command -v git >/dev/null 2>&1; then
  echo "[ERROR] 未检测到 git，请先安装 git（例如：sudo apt-get install -y git）。" >&2
  exit 1
fi

REPO_URL="$DEFAULT_REPO_URL"
if [[ -z "$REPO_URL" ]]; then
  REPO_URL="$(prompt_tty "请输入 GitHub 仓库地址 (例如 https://github.com/user/agy-telegram-bot.git): " "")"
fi
if [[ -z "$REPO_URL" ]]; then
  echo "[ERROR] 未提供仓库地址，请设置 AGY_TG_BOT_REPO 或手动克隆仓库后执行 bash install.sh。" >&2
  exit 1
fi

INSTALL_DIR="$(prompt_tty "安装目录 [默认: $DEFAULT_INSTALL_DIR]: " "$DEFAULT_INSTALL_DIR")"
INSTALL_DIR="${INSTALL_DIR/#\~/$HOME}"

if [[ -d "$INSTALL_DIR/.git" ]]; then
  echo "[INFO] 检测到已有仓库目录 $INSTALL_DIR，正在拉取最新代码..."
  git -C "$INSTALL_DIR" pull --ff-only
elif [[ -e "$INSTALL_DIR" ]]; then
  echo "[ERROR] 目录 $INSTALL_DIR 已存在且不是 git 仓库，请指定其他目录。" >&2
  exit 1
else
  echo "[INFO] 正在克隆仓库到 $INSTALL_DIR ..."
  git clone "$REPO_URL" "$INSTALL_DIR"
fi

echo
exec bash "$INSTALL_DIR/deploy/install.sh" "$@"
