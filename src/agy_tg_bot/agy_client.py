"""Async subprocess bridge to Google Antigravity CLI (`agy`) in headless stream-json mode."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import signal
import time
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import BotConfig
from .models import SessionKey, SessionState, TokenUsage, ToolTrace, TurnProgress, TurnResult

logger = logging.getLogger(__name__)

# Large per-line buffer (16 MiB) because tool outputs in stream-json NDJSON can easily
# exceed Python asyncio's default 64 KiB StreamReader limit.
STREAM_LINE_LIMIT = 16 * 1024 * 1024

ProgressCallback = Callable[[TurnProgress], Awaitable[None]]

# Known CLI-handled slash commands that cannot be sent into `--input-format stream-json`
# (see https://antigravity.google/docs/cli/headless#unsupported-messages).
CLI_ONLY_SLASH_COMMANDS: frozenset[str] = frozenset(
    {
        "/model",
        "/usage",
        "/quota",
        "/changelog",
        "/clear",
        "/new",
        "/exit",
        "/quit",
        "/logout",
        "/help",
        "/config",
        "/settings",
        "/keybindings",
        "/copy",
        "/diff",
        "/artifact",
        "/context",
        "/credits",
        "/feedback",
        "/hooks",
        "/mcp",
        "/agents",
        "/codesearch",
        "/cs",
        "/search",
        "/voice",
        "/record",
        "/title",
    }
)

_PERMISSION_NOTICE_RE = re.compile(
    r"(soft[- ]?denied|permission|requires approval|permissions\.allow|--dangerously-skip-permissions)",
    re.IGNORECASE,
)


async def _read_line_unbounded(stream: asyncio.StreamReader) -> bytes:
    """Read a full newline-terminated line without ever crashing on LimitOverrunError."""
    chunks = bytearray()
    while True:
        try:
            line = await stream.readline()
            if chunks:
                chunks.extend(line)
                return bytes(chunks)
            return line
        except ValueError:
            # Raised when a single line exceeds STREAM_LINE_LIMIT; drain until newline.
            try:
                part = await stream.readuntil(b"\n")
                chunks.extend(part)
                return bytes(chunks)
            except asyncio.LimitOverrunError as exc:
                part = await stream.readexactly(exc.consumed)
                chunks.extend(part)
            except asyncio.IncompleteReadError as exc:
                chunks.extend(exc.partial)
                return bytes(chunks)


def _summarize_tool_params(tool_name: str, tool_info: dict[str, Any] | None) -> str:
    """Extract a concise one-line summary of tool invocation arguments for Telegram UI."""
    if not isinstance(tool_info, dict):
        return ""
    params = tool_info.get("parameters")
    if not isinstance(params, dict) or not params:
        return ""

    priority_keys = (
        "CommandLine",
        "command",
        "cmd",
        "Path",
        "path",
        "TargetFile",
        "file_path",
        "AbsolutePath",
        "DirectoryPath",
        "SearchPath",
        "Query",
        "query",
        "Pattern",
        "pattern",
        "Url",
        "url",
        "Role",
        "role",
        "Prompt",
    )
    for key in priority_keys:
        val = params.get(key)
        if isinstance(val, str) and val.strip():
            text = " ".join(val.strip().split())
            return text[:110] + ("…" if len(text) > 110 else "")

    # Fallback to first scalar param
    for key, val in params.items():
        if isinstance(val, (str, int, float, bool)) and str(val).strip():
            text = f"{key}={str(val).strip()}"
            text = " ".join(text.split())
            return text[:110] + ("…" if len(text) > 110 else "")
    return ""


def _summarize_subagent(subagent_info: dict[str, Any] | None) -> tuple[str, str]:
    """Extract display name and summary from a subagent_info payload."""
    if not isinstance(subagent_info, dict):
        return "subagent", ""
    subagents = subagent_info.get("subagents")
    if isinstance(subagents, list) and subagents:
        first = subagents[0] if isinstance(subagents[0], dict) else {}
        type_name = str(first.get("type_name") or "subagent")
        role = str(first.get("role") or "")
        extra = f" (+{len(subagents) - 1})" if len(subagents) > 1 else ""
        return f"subagent:{type_name}", f"{role}{extra}".strip()
    return "subagent", ""


def _extract_tool_error(tool_info: dict[str, Any] | None) -> str | None:
    if not isinstance(tool_info, dict):
        return None
    err = tool_info.get("error")
    if isinstance(err, dict):
        msg = err.get("message") or err.get("type")
        return str(msg) if msg else None
    if isinstance(err, str) and err.strip():
        return err.strip()
    return None


def _is_permission_notice(line: str) -> bool:
    return bool(_PERMISSION_NOTICE_RE.search(line))


def build_agy_command(
    config: BotConfig,
    session: SessionState,
    *,
    persistent: bool,
    prompt: str | None = None,
    cmd_prefix: list[str] | None = None,
) -> list[str]:
    """Construct the exact `agy` CLI argv for persistent or oneshot headless mode."""
    argv: list[str] = list(cmd_prefix) if cmd_prefix else [config.agy_bin]

    if persistent:
        argv.extend(["--input-format", "stream-json", "--output-format", "stream-json"])
    else:
        if prompt is None:
            raise ValueError("prompt is required for oneshot mode")
        argv.extend(["-p", prompt, "--output-format", "stream-json"])
        if config.turn_timeout_seconds > 0:
            argv.extend(["--print-timeout", f"{max(1, int(config.turn_timeout_seconds))}s"])

    if session.conversation_id:
        argv.extend(["--conversation", session.conversation_id])

    # Permission mode mapping
    perm = session.permission_mode
    if perm in {"auto", "accept-edits", "plan"}:
        argv.extend(["--mode", perm])
    elif perm == "danger":
        if config.allow_dangerous_mode:
            argv.append("--dangerously-skip-permissions")
        else:
            logger.warning(
                "Session %s requested danger mode but ALLOW_DANGEROUS_MODE=false; "
                "falling back to default mode.",
                session.key.label,
            )

    if session.sandbox:
        argv.append("--sandbox")
    if session.model:
        argv.extend(["--model", session.model])
    if session.effort:
        argv.extend(["--effort", session.effort])
    if session.agent:
        argv.extend(["--agent", session.agent])

    for extra_dir in config.extra_dirs:
        argv.extend(["--add-dir", extra_dir])

    if config.disable_slash_commands:
        argv.append("--disable-slash-commands")

    return argv


@dataclass
class _PersistentWorker:
    """Holds a warm `agy --input-format stream-json --output-format stream-json` process."""

    key: SessionKey
    process: asyncio.subprocess.Process
    signature: tuple[Any, ...]
    conversation_id: str | None
    effective_permission_mode: str | None = None
    last_used_at: float = field(default_factory=time.monotonic)
    busy: bool = False
    stderr_history: deque[str] = field(default_factory=lambda: deque(maxlen=200))
    turn_stderr: list[str] = field(default_factory=list)
    stderr_task: asyncio.Task[None] | None = None
    last_usage: TokenUsage = field(default_factory=TokenUsage)
    last_duration_seconds: float = 0.0
    turns_completed: int = 0

    def is_alive(self) -> bool:
        return self.process.returncode is None and self.process.stdin is not None

    def start_stderr_drainer(self) -> None:
        if self.process.stderr is None:
            return
        self.stderr_task = asyncio.create_task(
            self._drain_stderr(), name=f"agy-stderr-{self.key.label}"
        )

    async def _drain_stderr(self) -> None:
        assert self.process.stderr is not None
        try:
            while True:
                raw = await _read_line_unbounded(self.process.stderr)
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if line:
                    self.stderr_history.append(line)
                    self.turn_stderr.append(line)
                    logger.debug("[agy stderr %s] %s", self.key.label, line)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("Stderr drainer stopped for %s", self.key.label, exc_info=True)

    async def close(self, *, graceful: bool = True) -> None:
        """Close stdin gracefully and ensure the subprocess exits cleanly."""
        proc = self.process
        if proc.returncode is None:
            if graceful and proc.stdin is not None and not proc.stdin.is_closing():
                try:
                    proc.stdin.close()
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(proc.stdin.wait_closed(), timeout=1.0)
                except Exception:
                    pass
                try:
                    await asyncio.wait_for(proc.wait(), timeout=3.0)
                except asyncio.TimeoutError:
                    pass

            if proc.returncode is None:
                await _terminate_process(proc)

        if self.stderr_task is not None and not self.stderr_task.done():
            self.stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.stderr_task


async def _terminate_process(proc: asyncio.subprocess.Process, *, interrupt_first: bool = False) -> None:
    """Terminate an asyncio subprocess safely on POSIX and Windows."""
    if proc.returncode is not None:
        return

    if interrupt_first and os.name != "nt":
        try:
            proc.send_signal(signal.SIGINT)
            await asyncio.wait_for(proc.wait(), timeout=2.5)
            return
        except (ProcessLookupError, OSError):
            return
        except asyncio.TimeoutError:
            pass

    if proc.returncode is None:
        try:
            proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=2.5)
            return
        except (ProcessLookupError, OSError):
            return
        except asyncio.TimeoutError:
            pass

    if proc.returncode is None:
        try:
            proc.kill()
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except (ProcessLookupError, OSError, asyncio.TimeoutError):
            pass


class _ActiveRun:
    """Tracks an in-flight turn so /cancel can abort it immediately."""

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self.process = process
        self.canceled = False


class AgyRunner:
    """Manages persistent and oneshot `agy` headless sessions across Telegram chats."""

    def __init__(
        self,
        config: BotConfig,
        *,
        cmd_prefix: list[str] | None = None,
    ) -> None:
        self._config = config
        self._cmd_prefix = list(cmd_prefix) if cmd_prefix else None
        self._workers: dict[SessionKey, _PersistentWorker] = {}
        self._active_runs: dict[SessionKey, _ActiveRun] = {}
        self._semaphore = asyncio.Semaphore(config.max_concurrent_runs)
        self._reaper_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Start the background idle-worker reaper."""
        if self._reaper_task is None or self._reaper_task.done():
            self._reaper_task = asyncio.create_task(
                self._idle_reaper_loop(), name="agy-idle-reaper"
            )

    async def stop(self) -> None:
        """Stop the reaper and cleanly terminate all running `agy` subprocesses."""
        if self._reaper_task is not None and not self._reaper_task.done():
            self._reaper_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reaper_task
            self._reaper_task = None

        # Abort any active oneshot runs
        for key, active in list(self._active_runs.items()):
            active.canceled = True
            await _terminate_process(active.process, interrupt_first=True)
            self._active_runs.pop(key, None)

        # Gracefully close all warm persistent workers
        workers = list(self._workers.values())
        self._workers.clear()
        await asyncio.gather(*(w.close(graceful=True) for w in workers), return_exceptions=True)

    def has_live_worker(self, key: SessionKey) -> bool:
        worker = self._workers.get(key)
        return worker is not None and worker.is_alive()

    def is_running(self, key: SessionKey) -> bool:
        return key in self._active_runs

    def active_worker_count(self) -> int:
        return sum(1 for w in self._workers.values() if w.is_alive())

    async def close_worker(self, key: SessionKey, *, graceful: bool = True) -> None:
        """Close and remove any cached persistent worker for `key`."""
        worker = self._workers.pop(key, None)
        if worker is not None:
            await worker.close(graceful=graceful)

    async def cancel_turn(self, key: SessionKey) -> bool:
        """Abort the currently executing turn for `key`. Returns True if a run was active."""
        active = self._active_runs.get(key)
        if active is None:
            return False
        active.canceled = True
        logger.info("Canceling active AGY turn for session %s", key.label)
        await _terminate_process(active.process, interrupt_first=True)
        # Drop worker so the next turn cleanly respawns (resuming conversation_id if known)
        worker = self._workers.pop(key, None)
        if worker is not None:
            await worker.close(graceful=False)
        return True

    async def run_turn(
        self,
        session: SessionState,
        prompt: str,
        *,
        on_progress: ProgressCallback | None = None,
    ) -> TurnResult:
        """Execute one user prompt against `agy` and stream progress events."""
        # Guard against CLI-only slash commands in persistent stream-json mode
        first_token = prompt.strip().split(maxsplit=1)[0].lower() if prompt.strip() else ""
        if session.runner_mode == "persistent" and first_token in CLI_ONLY_SLASH_COMMANDS:
            return TurnResult(
                conversation_id=session.conversation_id,
                status="ERROR",
                error=(
                    f"命令 `{first_token}` 由 agy CLI 本地拦截，无法在持久流式会话中直接发送。"
                    "请使用对应的 Telegram Bot 命令（如 /model、/usage、/new、/help）。"
                ),
            )

        workspace = self._resolve_valid_workspace(session)
        if workspace is None:
            return TurnResult(
                conversation_id=session.conversation_id,
                status="ERROR",
                error=f"工作目录不存在或无法访问：{session.workspace}",
            )

        async with self._semaphore:
            if session.runner_mode == "persistent":
                return await self._run_persistent_turn(
                    session, workspace, prompt, on_progress=on_progress
                )
            return await self._run_oneshot_turn(
                session, workspace, prompt, on_progress=on_progress
            )

    def _resolve_valid_workspace(self, session: SessionState) -> str | None:
        ws = Path(session.workspace).expanduser()
        if ws.is_dir():
            return str(ws.resolve())
        fallback = Path(self._config.default_workspace).expanduser()
        if fallback.is_dir():
            session.workspace = str(fallback.resolve())
            return session.workspace
        try:
            fallback.mkdir(parents=True, exist_ok=True)
            session.workspace = str(fallback.resolve())
            return session.workspace
        except OSError:
            return None

    async def _spawn_persistent_worker(
        self, session: SessionState, workspace: str
    ) -> _PersistentWorker:
        argv = build_agy_command(
            self._config,
            session,
            persistent=True,
            cmd_prefix=self._cmd_prefix,
        )
        logger.info("Spawning persistent agy worker for %s in %s", session.key.label, workspace)
        logger.debug("argv=%s", argv)
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=workspace,
            env=self._config.build_subprocess_env(),
            limit=STREAM_LINE_LIMIT,
        )
        sig = session.worker_signature(
            extra_dirs=self._config.extra_dirs,
            disable_slash_commands=self._config.disable_slash_commands,
        )
        worker = _PersistentWorker(
            key=session.key,
            process=proc,
            signature=sig,
            conversation_id=session.conversation_id,
        )
        worker.start_stderr_drainer()
        return worker

    async def _run_persistent_turn(
        self,
        session: SessionState,
        workspace: str,
        prompt: str,
        *,
        on_progress: ProgressCallback | None = None,
    ) -> TurnResult:
        sig = session.worker_signature(
            extra_dirs=self._config.extra_dirs,
            disable_slash_commands=self._config.disable_slash_commands,
        )

        # Allow one automatic retry if a reused warm worker died before producing output
        for attempt in range(2):
            reused = False
            worker = self._workers.get(session.key)
            if (
                worker is not None
                and worker.is_alive()
                and worker.signature == sig
                and worker.conversation_id == session.conversation_id
            ):
                reused = True
            else:
                if worker is not None:
                    await self.close_worker(session.key, graceful=True)
                try:
                    worker = await self._spawn_persistent_worker(session, workspace)
                except FileNotFoundError:
                    return TurnResult(
                        conversation_id=session.conversation_id,
                        status="ERROR",
                        error=(
                            f"找不到 agy 可执行文件：`{self._config.agy_bin}`。\n"
                            "请确认已在 VPS 安装 Antigravity CLI 并在 .env 中正确设置 AGY_BIN。"
                        ),
                    )
                except OSError as exc:
                    return TurnResult(
                        conversation_id=session.conversation_id,
                        status="ERROR",
                        error=f"启动 agy 进程失败：{exc}",
                    )
                self._workers[session.key] = worker

            worker.busy = True
            worker.turn_stderr.clear()
            active = _ActiveRun(worker.process)
            self._active_runs[session.key] = active
            started_at = time.monotonic()

            try:
                assert worker.process.stdin is not None
                assert worker.process.stdout is not None

                payload = json.dumps(
                    {"event": "user", "message": {"content": prompt}},
                    ensure_ascii=False,
                )
                try:
                    worker.process.stdin.write((payload + "\n").encode("utf-8"))
                    await worker.process.stdin.drain()
                except (BrokenPipeError, ConnectionResetError, OSError) as exc:
                    await self.close_worker(session.key, graceful=False)
                    if reused and attempt == 0:
                        logger.info(
                            "Reused worker pipe closed for %s; respawning and retrying once.",
                            session.key.label,
                        )
                        continue
                    return TurnResult(
                        conversation_id=session.conversation_id,
                        status="ERROR",
                        error=f"向 agy 进程写入提示词失败：{exc}",
                    )

                result, got_any_event = await self._consume_stream_turn(
                    stdout=worker.process.stdout,
                    process=worker.process,
                    active=active,
                    stderr_lines=worker.turn_stderr,
                    initial_conversation_id=worker.conversation_id or session.conversation_id,
                    initial_perm_mode=worker.effective_permission_mode,
                    previous_usage=worker.last_usage,
                    previous_duration=worker.last_duration_seconds,
                    cumulative_metrics=True,
                    started_at=started_at,
                    on_progress=on_progress,
                )

                # If a reused warm worker immediately EOF'd before any event, respawn once
                if (
                    reused
                    and attempt == 0
                    and not got_any_event
                    and not active.canceled
                    and not result.ok
                ):
                    logger.info(
                        "Reused worker exited before emitting events for %s; respawning once.",
                        session.key.label,
                    )
                    await self.close_worker(session.key, graceful=False)
                    continue

                # Update worker tracking state
                if result.conversation_id:
                    worker.conversation_id = result.conversation_id
                if result.effective_permission_mode:
                    worker.effective_permission_mode = result.effective_permission_mode
                worker.last_used_at = time.monotonic()

                if result.ok and worker.is_alive():
                    worker.last_usage = result.usage
                    worker.last_duration_seconds = result.duration_seconds
                    worker.turns_completed += 1
                else:
                    # Drop unhealthy or exited worker so next turn starts fresh
                    await self.close_worker(session.key, graceful=False)

                return result
            finally:
                worker.busy = False
                if self._active_runs.get(session.key) is active:
                    self._active_runs.pop(session.key, None)

        return TurnResult(
            conversation_id=session.conversation_id,
            status="ERROR",
            error="agy 持久会话重试失败，请重试或执行 /restart。",
        )

    async def _run_oneshot_turn(
        self,
        session: SessionState,
        workspace: str,
        prompt: str,
        *,
        on_progress: ProgressCallback | None = None,
    ) -> TurnResult:
        argv = build_agy_command(
            self._config,
            session,
            persistent=False,
            prompt=prompt,
            cmd_prefix=self._cmd_prefix,
        )
        logger.info("Running oneshot agy turn for %s in %s", session.key.label, workspace)
        logger.debug("argv=%s", argv)
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=workspace,
                env=self._config.build_subprocess_env(),
                limit=STREAM_LINE_LIMIT,
            )
        except FileNotFoundError:
            return TurnResult(
                conversation_id=session.conversation_id,
                status="ERROR",
                error=(
                    f"找不到 agy 可执行文件：`{self._config.agy_bin}`。\n"
                    "请确认已在 VPS 安装 Antigravity CLI 并在 .env 中正确设置 AGY_BIN。"
                ),
            )
        except OSError as exc:
            return TurnResult(
                conversation_id=session.conversation_id,
                status="ERROR",
                error=f"启动 agy 进程失败：{exc}",
            )

        stderr_lines: list[str] = []

        async def _drain() -> None:
            assert proc.stderr is not None
            try:
                while True:
                    raw = await _read_line_unbounded(proc.stderr)
                    if not raw:
                        break
                    line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                    if line:
                        stderr_lines.append(line)
                        logger.debug("[agy oneshot stderr %s] %s", session.key.label, line)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass

        stderr_task = asyncio.create_task(_drain(), name=f"agy-oneshot-stderr-{session.key.label}")
        active = _ActiveRun(proc)
        self._active_runs[session.key] = active
        started_at = time.monotonic()

        try:
            assert proc.stdout is not None
            result, _ = await self._consume_stream_turn(
                stdout=proc.stdout,
                process=proc,
                active=active,
                stderr_lines=stderr_lines,
                initial_conversation_id=session.conversation_id,
                initial_perm_mode=None,
                previous_usage=TokenUsage(),
                previous_duration=0.0,
                cumulative_metrics=False,
                started_at=started_at,
                on_progress=on_progress,
            )
            if proc.returncode is None:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(proc.wait(), timeout=3.0)
            if proc.returncode is None:
                await _terminate_process(proc)
            result.exit_code = proc.returncode
            return result
        finally:
            if not stderr_task.done():
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(stderr_task, timeout=1.0)
            if not stderr_task.done():
                stderr_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await stderr_task
            if self._active_runs.get(session.key) is active:
                self._active_runs.pop(session.key, None)

    async def _consume_stream_turn(
        self,
        *,
        stdout: asyncio.StreamReader,
        process: asyncio.subprocess.Process,
        active: _ActiveRun,
        stderr_lines: list[str],
        initial_conversation_id: str | None,
        initial_perm_mode: str | None,
        previous_usage: TokenUsage,
        previous_duration: float,
        cumulative_metrics: bool,
        started_at: float,
        on_progress: ProgressCallback | None,
    ) -> tuple[TurnResult, bool]:
        """Read NDJSON events from `stdout` until a `result` event, EOF, cancel, or timeout."""
        conversation_id = initial_conversation_id
        effective_perm = initial_perm_mode
        step_texts: OrderedDict[int, list[str]] = OrderedDict()
        tools_by_step: OrderedDict[int, ToolTrace] = OrderedDict()
        non_json_lines: list[str] = []
        got_any_event = False

        async def _emit_progress() -> None:
            if on_progress is None:
                return
            preview = _join_step_texts(step_texts)
            notices = [ln for ln in stderr_lines if _is_permission_notice(ln)]
            try:
                await on_progress(
                    TurnProgress(
                        conversation_id=conversation_id,
                        text_preview=preview,
                        tools=list(tools_by_step.values()),
                        permission_mode=effective_perm,
                        stderr_notices=notices[-5:],
                    )
                )
            except Exception:
                logger.debug("Progress callback failed", exc_info=True)

        async def _read_until_result() -> TurnResult | None:
            nonlocal conversation_id, effective_perm, got_any_event
            while True:
                raw = await _read_line_unbounded(stdout)
                if not raw:
                    return None
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue

                try:
                    event_obj = json.loads(line)
                except json.JSONDecodeError:
                    non_json_lines.append(line)
                    logger.debug("Ignoring non-JSON stdout line from agy: %s", line[:200])
                    continue

                if not isinstance(event_obj, dict):
                    continue

                event_type = event_obj.get("event")
                got_any_event = True

                if event_type == "init":
                    cid = event_obj.get("conversation_id")
                    if cid:
                        conversation_id = str(cid)
                    init_payload = event_obj.get("init")
                    if isinstance(init_payload, dict):
                        pm = init_payload.get("permission_mode")
                        if pm:
                            effective_perm = str(pm)
                    await _emit_progress()
                    continue

                if event_type == "step_update":
                    su = event_obj.get("step_update")
                    if not isinstance(su, dict):
                        continue
                    cid = su.get("conversation_id")
                    if cid:
                        conversation_id = str(cid)

                    step_index = int(su.get("step_index") or 0)
                    step_type = str(su.get("step_type") or "")
                    state = str(su.get("state") or "")
                    text_delta = su.get("text_delta")

                    changed = False
                    if isinstance(text_delta, str) and text_delta:
                        step_texts.setdefault(step_index, []).append(text_delta)
                        changed = True

                    tool_info = su.get("tool_info")
                    subagent_info = su.get("subagent_info")
                    tool_name = su.get("tool_name")

                    if step_type == "tool" or tool_name or tool_info or subagent_info:
                        if subagent_info:
                            display_name, summary = _summarize_subagent(subagent_info)
                            is_sub = True
                        else:
                            display_name = str(
                                tool_name
                                or (tool_info.get("name") if isinstance(tool_info, dict) else None)
                                or "tool"
                            )
                            summary = _summarize_tool_params(display_name, tool_info)
                            is_sub = False

                        err_msg = _extract_tool_error(tool_info)
                        dur = su.get("duration_seconds")
                        duration_val = float(dur) if isinstance(dur, (int, float)) else None

                        existing = tools_by_step.get(step_index)
                        if existing is None:
                            tools_by_step[step_index] = ToolTrace(
                                step_index=step_index,
                                name=display_name,
                                state=state or "ACTIVE",
                                summary=summary,
                                duration_seconds=duration_val,
                                error_message=err_msg,
                                is_subagent=is_sub,
                            )
                        else:
                            if display_name and display_name != "tool":
                                existing.name = display_name
                            if state:
                                existing.state = state
                            if summary:
                                existing.summary = summary
                            if duration_val is not None:
                                existing.duration_seconds = duration_val
                            if err_msg:
                                existing.error_message = err_msg
                        changed = True

                    if changed:
                        await _emit_progress()
                    continue

                if event_type == "result":
                    res = event_obj.get("result")
                    if not isinstance(res, dict):
                        res = {}
                    cid = res.get("conversation_id") or conversation_id
                    if cid:
                        conversation_id = str(cid)
                    status = str(res.get("status") or "ERROR").upper()
                    response_text = res.get("response")
                    if not isinstance(response_text, str) or not response_text.strip():
                        fallback_preview = _join_step_texts(step_texts)
                        if fallback_preview:
                            response_text = fallback_preview
                        else:
                            response_text = response_text or ""

                    error_text = res.get("error")
                    if error_text is not None:
                        error_text = str(error_text).strip() or None

                    raw_duration = float(res.get("duration_seconds") or 0.0)
                    wall_duration = max(0.0, time.monotonic() - started_at)
                    usage = TokenUsage.from_dict(res.get("usage"))

                    if cumulative_metrics:
                        turn_usage = usage.delta_from(previous_usage)
                        turn_duration = (
                            max(0.0, raw_duration - previous_duration)
                            if raw_duration > previous_duration
                            else wall_duration
                        )
                    else:
                        turn_usage = usage
                        turn_duration = raw_duration if raw_duration > 0 else wall_duration

                    denials = _collect_permission_denials(stderr_lines, list(tools_by_step.values()))
                    return TurnResult(
                        conversation_id=conversation_id,
                        status=status,
                        response=response_text,
                        error=error_text,
                        duration_seconds=raw_duration or wall_duration,
                        turn_duration_seconds=turn_duration,
                        num_turns=int(res.get("num_turns") or 0),
                        usage=usage,
                        turn_usage=turn_usage,
                        tools=list(tools_by_step.values()),
                        stderr_lines=list(stderr_lines),
                        permission_denials=denials,
                        effective_permission_mode=effective_perm,
                    )

        timeout = self._config.turn_timeout_seconds
        try:
            if timeout > 0:
                res_obj = await asyncio.wait_for(_read_until_result(), timeout=timeout)
            else:
                res_obj = await _read_until_result()
        except asyncio.TimeoutError:
            await _terminate_process(process, interrupt_first=True)
            wall = max(0.0, time.monotonic() - started_at)
            return (
                TurnResult(
                    conversation_id=conversation_id,
                    status="TIMEOUT",
                    response=_join_step_texts(step_texts),
                    error=f"执行超时（超过 {int(timeout)} 秒），已中止本次任务。",
                    duration_seconds=wall,
                    turn_duration_seconds=wall,
                    tools=list(tools_by_step.values()),
                    stderr_lines=list(stderr_lines),
                    permission_denials=_collect_permission_denials(
                        stderr_lines, list(tools_by_step.values())
                    ),
                    effective_permission_mode=effective_perm,
                ),
                got_any_event,
            )

        if res_obj is not None:
            return res_obj, got_any_event

        # Stream ended (EOF) without a terminal `result` event
        wall = max(0.0, time.monotonic() - started_at)
        if process.returncode is None:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(process.wait(), timeout=2.0)

        if active.canceled:
            return (
                TurnResult(
                    conversation_id=conversation_id,
                    status="CANCELED",
                    response=_join_step_texts(step_texts),
                    error="已取消当前任务。",
                    duration_seconds=wall,
                    turn_duration_seconds=wall,
                    tools=list(tools_by_step.values()),
                    stderr_lines=list(stderr_lines),
                    effective_permission_mode=effective_perm,
                    exit_code=process.returncode,
                ),
                got_any_event,
            )

        # Build helpful diagnostic error from stderr / non-JSON stdout
        detail_parts: list[str] = []
        if stderr_lines:
            detail_parts.append("\n".join(stderr_lines[-10:]))
        if non_json_lines:
            detail_parts.append("\n".join(non_json_lines[-5:]))
        detail = "\n".join(p for p in detail_parts if p).strip()
        exit_info = f"exit={process.returncode}" if process.returncode is not None else "EOF"
        err_msg = f"agy 进程意外结束（{exit_info}）。"
        if detail:
            err_msg = f"{err_msg}\n{detail}"

        return (
            TurnResult(
                conversation_id=conversation_id,
                status="ERROR",
                response=_join_step_texts(step_texts),
                error=err_msg,
                duration_seconds=wall,
                turn_duration_seconds=wall,
                tools=list(tools_by_step.values()),
                stderr_lines=list(stderr_lines),
                permission_denials=_collect_permission_denials(
                    stderr_lines, list(tools_by_step.values())
                ),
                effective_permission_mode=effective_perm,
                exit_code=process.returncode,
            ),
            got_any_event,
        )

    async def _idle_reaper_loop(self) -> None:
        """Periodically close idle persistent `agy` processes to save VPS RAM."""
        try:
            while True:
                await asyncio.sleep(30.0)
                now = time.monotonic()
                timeout = self._config.session_idle_timeout_seconds
                to_close: list[SessionKey] = []
                for key, worker in list(self._workers.items()):
                    if not worker.is_alive():
                        to_close.append(key)
                    elif not worker.busy and (now - worker.last_used_at) >= timeout:
                        to_close.append(key)
                for key in to_close:
                    logger.info("Reaping idle/dead agy worker for session %s", key.label)
                    await self.close_worker(key, graceful=True)
        except asyncio.CancelledError:
            raise

    # ------------------------------------------------------------------
    # Standalone CLI queries (never injected into a stream-json session)
    # ------------------------------------------------------------------

    async def _run_simple_cli(
        self,
        args: list[str],
        *,
        cwd: str | None = None,
        timeout: float = 30.0,
    ) -> tuple[int, str, str]:
        argv = (list(self._cmd_prefix) if self._cmd_prefix else [self._config.agy_bin]) + args
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd or self._config.default_workspace,
                env=self._config.build_subprocess_env(),
                limit=STREAM_LINE_LIMIT,
            )
        except FileNotFoundError:
            return 127, "", f"Binary not found: {self._config.agy_bin}"
        except OSError as exc:
            return 1, "", str(exc)

        try:
            out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            await _terminate_process(proc)
            return 124, "", f"Command timed out after {int(timeout)}s"

        return (
            int(proc.returncode or 0),
            out_b.decode("utf-8", errors="replace"),
            err_b.decode("utf-8", errors="replace"),
        )

    async def list_models(self, *, cwd: str | None = None) -> tuple[list[tuple[str, str]], str | None]:
        """Run `agy models` and return [(slug, label), ...]."""
        code, stdout, stderr = await self._run_simple_cli(["models"], cwd=cwd, timeout=25.0)
        if code != 0:
            return [], (stderr.strip() or stdout.strip() or f"agy models exited with {code}")

        models: list[tuple[str, str]] = []
        for raw_line in stdout.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("Available") or line.startswith("---"):
                continue
            # Expected format from docs: "gemini-4-neon-xhigh            Gemini 4 Neon (XHigh)"
            parts = re.split(r"\s{2,}", line, maxsplit=1)
            slug = parts[0].strip()
            if not slug or " " in slug:
                continue
            label = parts[1].strip() if len(parts) > 1 else slug
            models.append((slug, label))
        return models, None

    async def list_agents(self, *, cwd: str | None = None) -> tuple[str, str | None]:
        """Run `agy agents` and return raw output."""
        code, stdout, stderr = await self._run_simple_cli(["agents"], cwd=cwd, timeout=25.0)
        if code != 0:
            return "", (stderr.strip() or stdout.strip() or f"agy agents exited with {code}")
        return stdout.strip(), None

    async def get_usage_report(self, *, cwd: str | None = None) -> tuple[str, str | None]:
        """Run standalone `agy -p /usage` without affecting any active stream session."""
        code, stdout, stderr = await self._run_simple_cli(
            ["-p", "/usage", "--print-timeout", "30s"],
            cwd=cwd,
            timeout=35.0,
        )
        if code != 0:
            return "", (stderr.strip() or stdout.strip() or f"agy -p /usage exited with {code}")
        return (stdout.strip() or stderr.strip()), None


def _join_step_texts(step_texts: OrderedDict[int, list[str]]) -> str:
    blocks = ["".join(parts) for parts in step_texts.values()]
    non_empty = [b for b in blocks if b]
    if not non_empty:
        return ""
    if len(non_empty) == 1:
        return non_empty[0]
    out = non_empty[0]
    for nxt in non_empty[1:]:
        if out.endswith("\n\n") or nxt.startswith("\n"):
            out += nxt
        elif out.endswith("\n"):
            out += "\n" + nxt
        else:
            out += "\n\n" + nxt
    return out


def _collect_permission_denials(
    stderr_lines: list[str], tools: list[ToolTrace]
) -> list[str]:
    denials: list[str] = []
    seen: set[str] = set()
    for line in stderr_lines:
        if _is_permission_notice(line) and line not in seen:
            seen.add(line)
            denials.append(line)
    for t in tools:
        if t.error_message and _is_permission_notice(t.error_message):
            entry = f"{t.name}: {t.error_message}"
            if entry not in seen:
                seen.add(entry)
                denials.append(entry)
    return denials
