# AGY Telegram Bot (for Linux VPS)

将部署在 Linux VPS 上的 **Google Antigravity CLI (`agy`)** 无缝接入 Telegram Bot，让你在手机或电脑的 Telegram 中直接远程操控 VPS 上的 AGY 进行代码开发、排障、运维与日常问答。

## 核心特性

- **原生 Headless `stream-json` 协议桥接**：
  - **`persistent` 常驻流式模式（默认）**：基于 `agy --input-format stream-json --output-format stream-json`，每个活跃会话复用预热的后台进程，多轮对话无冷启动延迟；空闲超过阈值自动回收进程释放 VPS 内存，并将 `conversation_id` 持久化到 SQLite，下次发消息自动通过 `--conversation` 续接上下文。
  - **`oneshot` 单次模式（可随时切换）**：每轮按需调用 `agy -p ... --output-format stream-json --conversation <id>`，适合超小内存 VPS。
- **实时流式进度与工具轨迹**：
  - 在 Telegram 消息中实时展示思考状态、正在执行的工具（`run_command`、`edit_file`、Subagent 等）与流式文本预览，自带防限流节流与 `[🛑 中止任务]` 一键取消按钮。
  - 自动将 Markdown 渲染为 Telegram HTML，支持跨消息智能分段（保持代码块围栏完整不截断），超长回复自动附带完整 `.md` 文件。
- **完整复用 VPS 本机 `agy` 生态**：
  - 直接继承本机 Google 账号订阅（SSH OAuth）、`GEMINI_API_KEY` 或 Google Cloud ADC 认证，以及 `~/.gemini/antigravity-cli/settings.json` 中的 Skills、MCP Server、Rules 与插件。
- **严格安全与可切换权限模式**：
  - **Fail-Closed 用户白名单**：未配置 `ALLOWED_USER_IDS` 时拒绝启动。
  - **五档权限策略可随时切换（`/perm`）**：`default` / `auto` / `accept-edits` / `plan` / `danger`（`--dangerously-skip-permissions`，带二次确认且可通过 `ALLOW_DANGEROUS_MODE=false` 彻底锁定）。
  - 支持 `--sandbox` 终端沙箱切换（`/sandbox`）及 `ALLOWED_WORKSPACE_ROOTS` 目录边界限制。
- **双向文件与图片自动直发**：
  - 发送图片、文档、语音/音频给 Bot，自动保存至 `<工作区>/.tg_inbox/` 并通知 `agy` 读取分析。
  - **对话图片自动发送**：`agy` 在回复或工具执行中生成/引用的本地图片（`.png`/`.jpg`/`.webp`/`.gif`/`.bmp`），以及本轮在工作区新生成的图表/截图，都会自动直接发送到 Telegram 对话中（大图或不支持尺寸自动降级为原图文档发送，且不会重复回发 `.tg_inbox/` 中的用户上传图）。
  - 使用 `/get <路径>` 可按需从 VPS 拉取任意日志、构建产物或代码文件到 Telegram。
- **交互式控制面板（`/start` / `/menu`）与群组话题隔离**：
  - 发送 `/start` 或 `/menu` 即可呼出可视化按钮控制面板，一键完成新建对话、模型/推理强度/权限模式切换、沙箱/引擎/详细轨迹开关、目录逐层浏览、历史恢复与配额查看。
  - 支持 Telegram 群组 **Forum Topics**，每个话题独立维护工作区、模型与会话上下文。
- **自带交互式配置命令行（`agy-tg-config`）、VPS 诊断工具（`agy-tg-doctor`）与 `systemd` 一键部署脚本**。

---

## 目录结构

```text
TG_Bot/
├── src/agy_tg_bot/
│   ├── __main__.py       # Bot 启动入口 (agy-tg-bot)
│   ├── config.py         # 环境变量解析、白名单与路径边界校验
│   ├── env_editor.py     # 保留注释的 .env 与 AGY settings.json 原子读写器
│   ├── cli_config.py     # 交互式设置与环境变量命令行 (agy-tg-config)
│   ├── models.py         # 会话状态、事件与统计模型
│   ├── storage.py        # SQLite 会话与历史记录持久化
│   ├── agy_client.py     # agy CLI 异步子进程桥接（persistent + oneshot）
│   ├── formatting.py     # Markdown -> Telegram HTML 转换与围栏感知分段
│   ├── bot.py            # Telegram 指令、内联按钮、媒体与流式渲染
│   └── doctor.py         # VPS 环境与鉴权自检工具 (agy-tg-doctor)
├── install.sh            # 交互式安装入口（支持本地运行与 curl 一键引导克隆）
├── deploy/
│   ├── install.sh                    # Linux VPS 7步交互式安装/卸载向导
│   ├── agy-telegram-bot-user.service # systemd --user 服务模板（推荐）
│   └── agy-telegram-bot.service      # 系统级 systemd 服务模板
├── tests/                # 完整单元测试与模拟 agy 协议端到端测试
├── .env.example          # 详细配置模板
├── pyproject.toml
└── requirements.txt
```

---

## 快速部署到 Linux VPS

### 1. 在 VPS 上安装并登录 `agy` CLI

```bash
# 安装 Antigravity CLI（默认安装至 ~/.local/bin/agy）
curl -fsSL https://antigravity.google/cli/install.sh | bash
export PATH="$HOME/.local/bin:$PATH"
```

根据你的使用方式选择以下**任意一种**认证方式：

#### 方式 A：个人 Google 账号（SSH OAuth，复用 Pro/Ultra 订阅，推荐）
在 SSH 终端运行一次交互式 `agy`：
```bash
agy
```
CLI 会检测到 SSH 环境并输出授权链接：在本地浏览器打开登录，将页面返回的授权码粘贴回 SSH 终端即可完成认证（完成后按 `Ctrl+D` 退出）。

> **提示**：Linux 无头 VPS 若使用 Keyring 存储 OAuth 凭证，请优先采用下方的 **`systemd --user` 用户服务 + `loginctl enable-linger`**，确保后台服务能访问当前用户的 D-Bus / Keyring 会话。

#### 方式 B：Gemini API Key（纯无头服务器最省心）
1. 在 `~/.gemini/antigravity-cli/settings.json` 中配置：
   ```json
   {
     "modelProvider": "gemini"
   }
   ```
2. 在项目的 `.env` 中填入 `GEMINI_API_KEY=你的密钥`（可选配置 `GOOGLE_GEMINI_BASE_URL`）。

#### 方式 C：Google Cloud ADC（Gemini Enterprise / Vertex）
```bash
gcloud auth application-default login --project YOUR_GCP_PROJECT
```
并在 `.env` 中设置 `AGY_ADC_AUTH=true`。

---

### 2. 获取 Telegram Bot Token 与你的 User ID

1. 在 Telegram 找 [@BotFather](https://t.me/BotFather) 发送 `/newbot`，获取 `TELEGRAM_BOT_TOKEN`。
2. 找 [@userinfobot](https://t.me/userinfobot) 获取你的数字 `User ID`，填入 `ALLOWED_USER_IDS`。

---

### 3. 交互式一键安装与配置

在 Linux VPS 上直接执行一行命令，自动克隆仓库并进入交互式安装向导：

```bash
curl -fsSL https://raw.githubusercontent.com/PeterGong2008/agy-little-bot/main/install.sh | bash
```

或者手动克隆后运行：

```bash
git clone https://github.com/PeterGong2008/agy-little-bot.git ~/agy-little-bot
cd ~/agy-little-bot
bash install.sh
```

交互式安装向导将分 7 步自动引导完成：
1. 检查 Python 3.10+ 与 `python3-venv`（Debian/Ubuntu 缺失时可一键 `apt` 安装）；
2. 检查并可选自动安装官方 `agy` CLI；
3. 创建 `.venv`、按需安装 SOCKS5 代理依赖（`httpx[socks]`），并将 `agy-tg-bot` / `agy-tg-config` / `agy-tg-doctor` 软链到 `~/.local/bin/`；
4. 引导选择 AGY 认证方式（SSH OAuth 直接登录 / Gemini API Key 自动同步 `settings.json` / Google Cloud ADC）；
5. 启动内置 `agy-tg-config` 交互向导填写 Token、白名单、工作区、权限模式与代理；
6. 选择安装 `systemd --user`（自动开启 `enable-linger`）或系统级 `systemd` 服务；
7. 一键执行 `agy-tg-doctor --live-test --check-telegram` 并直接启动守护进程。

如需非交互式自动化安装或卸载服务：
```bash
bash deploy/install.sh --yes --user --with-proxy   # 静默安装用户级服务（含代理支持）
bash deploy/install.sh --yes --system              # 静默安装系统级服务
bash deploy/install.sh --uninstall                 # 停止并卸载 systemd 服务（保留 .env 与数据库）
```

后续随时使用内置交互式配置命令行修改设置与环境变量（自动保留 `.env` 注释并维持 `chmod 600`）：

```bash
# 打开交互式配置面板（安装脚本已软链到 ~/.local/bin，任意目录可执行）
agy-tg-config

# 或直接运行首次部署 6 步向导
agy-tg-config wizard
```

也支持直接命令行快捷读写：

```bash
agy-tg-config list                              # 查看当前全部配置（敏感项自动脱敏）
agy-tg-config set perm auto                     # 支持别名或完整变量名，带合法性校验
agy-tg-config set proxy socks5://127.0.0.1:1080
agy-tg-config get ALLOWED_USER_IDS
agy-tg-config unset TELEGRAM_PROXY_URL
agy-tg-config agy-settings                      # 交互式编辑 ~/.gemini/antigravity-cli/settings.json
```

在 `agy-tg-config` 交互界面中可直接：
- 输入 **编号** 或 **别名**（如 `token` / `users` / `ws` / `perm` / `model` / `proxy`），或直接输入 **`KEY=VALUE`** 一行修改；
- 按 **`[w]`** 进入首次部署向导，**`[a]`** 编辑 AGY CLI 的 `settings.json`（含 `modelProvider` 与 `permissions.allow` 规则模板），**`[c]`** 管理自定义环境变量（如 `HTTP_PROXY`），**`[v]`** 切换密钥明文预览，**`[d]`** 运行环境自检，**`[r]`** 保存并自动重启 `systemd` 服务。

> 若 VPS 所在网络需要代理访问 Telegram API，请设置 `TELEGRAM_PROXY_URL=socks5://127.0.0.1:1080` 并安装 SOCKS 支持：
> `.venv/bin/pip install "httpx[socks]"`

---

### 4. 运行环境自检并启动服务

```bash
# 运行内置诊断工具（检查 Python、.env、agy 路径、鉴权、模型列表与 Telegram 连通性）
.venv/bin/agy-tg-doctor --live-test --check-telegram
```

确认全部通过后启动后台守护进程：

```bash
# 用户级 systemd（默认）
systemctl --user restart agy-telegram-bot
systemctl --user status agy-telegram-bot
journalctl --user -u agy-telegram-bot -f

# 若选择了 --system 安装方式：
# sudo systemctl restart agy-telegram-bot
# sudo journalctl -u agy-telegram-bot -f
```

---

## Telegram 指令一览

| 指令 | 说明 |
| --- | --- |
| 直接发文本 / 图片 / 文件 | 发送提示词给 `agy`；附件自动存入 `<workspace>/.tg_inbox/`；AGY 生成/引用的图片自动回发 |
| `/start`（或 `/menu`） | 打开交互式控制面板（内联按钮一站式控制所有设置） |
| `/help` | 查看指令菜单 |
| `/new`（或 `/clear`） | 开启全新对话（清空当前 `conversation_id`） |
| `/cancel`（或 `/stop`） | 立即中止正在执行的任务（也可直接点消息底部的 `🛑 中止任务` 按钮） |
| `/status` | 查看当前工作区、对话 ID、权限模式、沙箱、模型与后台进程状态 |
| `/cd [路径]` | 切换 VPS 工作目录；不带参数时弹出可点击的目录浏览器 |
| `/ls [路径]` | 列出目录中的文件与子目录 |
| `/get <路径>` | 将 VPS 上的文件直接发送到 Telegram |
| `/img <关键词>` | 秒级联网搜图并直接发送高清实图到对话（同时 `agy` 自身也已内置 `web-image-search` Skill，可直接对话要求联网搜图） |
| `/perm [模式]` | 切换工具权限模式：`default` / `auto` / `accept-edits` / `plan` / `danger` |
| `/sandbox [on\|off]` | 开关 `--sandbox` OS 级终端沙箱 |
| `/model [slug\|default]` | 调用 `agy models` 展示可点击的模型列表，或直接切换模型 |
| `/effort [level\|default]` | 设置推理强度（`low` / `medium` / `high` / `xhigh` / `max`） |
| `/agent [name\|default]` | 查看/切换自定义 Agent |
| `/usage` | 独立运行 `agy -p /usage` 查看配额与用量（不干扰活跃流式会话） |
| `/history` | 列出最近的历史会话并支持点击按钮一键恢复 |
| `/resume <id>` | 按完整 ID 或前缀恢复历史会话 |
| `/runner [persistent\|oneshot]` | 切换常驻流式模式或单次按需模式 |
| `/verbose [on\|off]` | 开关工具执行轨迹与耗时/Token 底部摘要 |
| `/restart` | 重启后台 `agy` 进程（保留当前对话 ID） |
| `/pass <内容>` | 透传任意指令或自定义 Skill（例如 `/pass /my-skill 参数`） |
| `/goal` `/plan` `/compact` `/boost` `/grill_me` `/learn` `/btw` `/swarm` `/teamwork` | 直接透传对应的 AGY Agent 斜杠命令 |

---

## 权限与安全建议

1. **严格白名单**：仅将你本人可信的 Telegram 数字 ID 填入 `ALLOWED_USER_IDS`。
2. **目录边界控制**：若不希望 Bot 越界浏览整个磁盘，在 `.env` 设置 `ALLOWED_WORKSPACE_ROOTS=/home/ubuntu/projects,/srv/apps`，即可限制 `/cd` 和 `/get` 的可访问范围。
3. **精细化工具放行（推荐替代全局 danger）**：
   在 `~/.gemini/antigravity-cli/settings.json` 中按需放行常用命令，既安全又无需每次被 headless 软拒绝：
   ```json
   {
     "agentMode": "auto",
     "enableTerminalSandbox": true,
     "permissions": {
       "allow": [
         "command(git)",
         "command(pytest)",
         "command(docker ps)",
         "write_file(src/)"
       ]
     }
   }
   ```
4. **生产机锁定危险模式**：在生产 VPS 上可将 `.env` 中的 `ALLOW_DANGEROUS_MODE=false`，彻底禁用 `/perm danger`。

---

## 常见问题排查

- **`agy: command not found`**：确认 `~/.local/bin/agy` 存在，或在 `.env` 中显式设置 `AGY_BIN=/home/你的用户名/.local/bin/agy`。
- **`secret keyring is locked` / `authentication required`**：
  - 使用 OAuth 登录时，请改用 `systemd --user` + `sudo loginctl enable-linger $USER` 运行服务，确保 `DBUS_SESSION_BUS_ADDRESS` 可用；
  - 或切换为 `GEMINI_API_KEY`（同时在 `~/.gemini/antigravity-cli/settings.json` 设置 `"modelProvider": "gemini"`）。
- **后台卡在 `update.lock`**：本项目的 `systemd` 模板与 `.env.example` 已默认启用 `AGY_CLI_DISABLE_AUTO_UPDATE=true`。若此前残留锁文件，可执行 `rm -f ~/.gemini/antigravity-cli/updater/update.lock`。
- **工具调用在 Telegram 提示权限未自动执行**：headless 模式下没有交互式确认提示，需审批的操作会被软拒绝。可通过 `/perm auto`、`/perm danger`，或在 `settings.json` 添加 `permissions.allow` 规则解决。

---

## 本地开发与测试

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest -v
```
