from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import errno
import fcntl
import os
import pty
import shlex
import signal
import struct
import subprocess
import termios
from dataclasses import dataclass
from pathlib import Path

from .models import GpuProcess, GpuState, Task, WorkerConfig


@dataclass(frozen=True)
class RemoteResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class SshClient:
    def __init__(self, worker: WorkerConfig, timeout: int = 30):
        self.worker = worker
        self.timeout = timeout

    def command(self, command: str, *, force_tty: bool = False) -> list[str]:
        cmd = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            "ServerAliveInterval=15",
            "-o",
            "ServerAliveCountMax=4",
            "-o",
            "TCPKeepAlive=yes",
            "-p",
            str(self.worker.port),
        ]
        if self.worker.ssh_key:
            cmd.extend(["-i", str(Path(self.worker.ssh_key).expanduser())])
        if force_tty:
            cmd.append("-tt")
        cmd.extend([self.worker.ssh_target, command])
        return cmd

    def run(self, command: str, *, input_text: str | None = None, timeout: int | None = None) -> RemoteResult:
        completed = subprocess.run(
            self.command(command),
            input=input_text,
            text=True,
            capture_output=True,
            timeout=timeout or self.timeout,
            check=False,
        )
        return RemoteResult(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )


class RemotePtySession:
    def __init__(self, process: asyncio.subprocess.Process, master_fd: int):
        self.process = process
        self.master_fd = master_fd

    async def read(self, size: int = 32768) -> bytes:
        if self.master_fd < 0:
            return b""
        try:
            return await asyncio.to_thread(os.read, self.master_fd, size)
        except OSError as exc:
            if exc.errno in {errno.EIO, errno.EBADF}:
                return b""
            raise

    async def write(self, data: bytes) -> None:
        if not data or self.master_fd < 0:
            return
        await asyncio.to_thread(_write_fd, self.master_fd, data)

    def resize(self, cols: int, rows: int) -> None:
        if self.master_fd < 0:
            return
        _set_pty_size(self.master_fd, cols, rows)
        if self.process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self.process.send_signal(signal.SIGWINCH)

    async def terminate(self) -> None:
        if self.master_fd >= 0:
            with contextlib.suppress(OSError):
                os.close(self.master_fd)
            self.master_fd = -1
        if self.process.returncode is not None:
            return
        self.process.terminate()
        try:
            await asyncio.wait_for(self.process.wait(), timeout=3)
        except asyncio.TimeoutError:
            self.process.kill()
            await self.process.wait()


class RemoteExecutor:
    def __init__(self, remote_base_dir: str):
        self.remote_base_dir = remote_base_dir

    def probe_gpus(self, worker: WorkerConfig) -> list[GpuState]:
        client = SshClient(worker)
        result = client.run(
            "gpu_output=$(nvidia-smi --query-gpu=index,uuid,pci.bus_id,memory.used,memory.total,utilization.gpu "
            "--format=csv,noheader,nounits) || exit $?; "
            "printf '%s\\n' \"$gpu_output\"; "
            "printf '%s\\n' \"$gpu_output\" | while IFS=, read -r index uuid bus rest; do "
            "index=$(printf '%s' \"$index\" | tr -d '[:space:]'); "
            "bus=$(printf '%s' \"$bus\" | sed 's/^ *//;s/ *$//;s/^00000000:/0000:/' | tr '[:upper:]' '[:lower:]'); "
            "numa=$(cat \"/sys/bus/pci/devices/$bus/numa_node\" 2>/dev/null) || continue; "
            "case \"$numa\" in ''|-1|*[!0-9]*) continue ;; esac; "
            "printf 'GPU-WATCHER-NUMA\\t%s\\t%s\\n' \"$index\" \"$numa\"; "
            "done; "
            "nvidia-smi topo -m 2>/dev/null | awk "
            "'$1 ~ /^GPU[0-9]+$/ { numa=$(NF-1); "
            "if (numa ~ /^[0-9]+$/) { gpu_index=$1; sub(/^GPU/, \"\", gpu_index); "
            "printf \"GPU-WATCHER-NUMA\\t%s\\t%s\\n\", gpu_index, numa } }'; "
            "nvidia-smi --query-compute-apps=gpu_uuid,pid,used_gpu_memory --format=csv,noheader,nounits "
            "2>/dev/null | while IFS=, read -r uuid pid memory; do "
            "pid=$(printf '%s' \"$pid\" | tr -d '[:space:]'); "
            "case \"$pid\" in ''|*[!0-9]*) continue ;; esac; "
            "memory=$(printf '%s' \"$memory\" | tr -cd '0-9'); "
            "user=$(ps -o user:64= -p \"$pid\" 2>/dev/null | awk 'NR==1 {print $1}'); "
            "cmdline=$(ps -ww -o args= -p \"$pid\" 2>/dev/null | base64 | tr -d '\\n'); "
            "printf 'GPU-WATCHER-PROCESS\\t%s\\t%s\\t%s\\t%s\\t%s\\n' "
            "\"$uuid\" \"$pid\" \"${memory:-0}\" \"$user\" \"$cmdline\"; "
            "done",
            timeout=15,
        )
        if not result.ok:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "nvidia-smi failed")

        gpu_rows: list[list[str]] = []
        numa_by_index: dict[int, int] = {}
        processes_by_uuid: dict[str, list[GpuProcess]] = {}
        for line in result.stdout.splitlines():
            if line.startswith("GPU-WATCHER-NUMA\t"):
                parts = line.split("\t", 2)
                try:
                    numa_by_index.setdefault(int(parts[1]), int(parts[2]))
                except (IndexError, ValueError):
                    pass
                continue
            if line.startswith("GPU-WATCHER-PROCESS\t"):
                parts = line.split("\t", 5)
                if len(parts) != 6:
                    continue
                gpu_uuid = parts[1].strip()
                try:
                    pid = int(parts[2])
                    memory_used_mb = int(parts[3])
                    cmdline = base64.b64decode(parts[5], validate=True).decode(
                        "utf-8", errors="replace"
                    )
                except (binascii.Error, ValueError, UnicodeError):
                    continue
                processes_by_uuid.setdefault(gpu_uuid, []).append(
                    GpuProcess(
                        pid=pid,
                        memory_used_mb=memory_used_mb,
                        username=parts[4].strip(),
                        cmdline=cmdline.strip(),
                    )
                )
                continue
            parts = [part.strip() for part in line.split(",")]
            if len(parts) == 6:
                gpu_rows.append(parts)

        states: list[GpuState] = []
        allowed = set(worker.gpus) if worker.gpus else None
        for parts in gpu_rows:
            index = int(parts[0])
            if allowed is not None and index not in allowed:
                continue
            processes = tuple(
                sorted(
                    processes_by_uuid.get(parts[1], []),
                    key=lambda process: (-process.memory_used_mb, process.pid),
                )
            )
            states.append(
                GpuState(
                    index=index,
                    memory_used_mb=int(parts[3]),
                    memory_total_mb=int(parts[4]),
                    utilization_percent=int(parts[5]),
                    numa_node=numa_by_index.get(index),
                    process_users=tuple(
                        sorted({process.username for process in processes if process.username})
                    ),
                    processes=processes,
                )
            )
        return states

    def start_task(self, worker: WorkerConfig, task: Task, gpu_indexes: list[int]) -> str:
        session = self.session_name(worker, task.id)
        task_dir = self.task_dir(task.id)
        script_path = f"{task_dir}/run.sh"
        script = self._render_task_script(task, gpu_indexes, task_dir)

        client = SshClient(worker)
        setup_cmd = (
            f"mkdir -p {_quote_remote_path(task_dir)} && "
            f"cat > {_quote_remote_path(script_path)} && "
            f"chmod 700 {_quote_remote_path(script_path)} && "
            f"rm -f {_quote_remote_path(f'{task_dir}/exit_code')} "
            f"{_quote_remote_path(f'{task_dir}/finished_at')}"
        )
        setup = client.run(setup_cmd, input_text=script, timeout=30)
        if not setup.ok:
            raise RuntimeError(setup.stderr.strip() or setup.stdout.strip() or "failed to upload task script")

        tmux_cmd = (
            "tmux new-session -d "
            f"-s {shlex.quote(session)} "
            f"{shlex.quote(f'bash {script_path}')}"
        )
        started = client.run(tmux_cmd, timeout=15)
        if not started.ok:
            raise RuntimeError(started.stderr.strip() or started.stdout.strip() or "failed to start tmux session")
        return session

    def read_exit_code(self, worker: WorkerConfig, task_id: str) -> int | None:
        task_dir = self.task_dir(task_id)
        client = SshClient(worker)
        result = client.run(
            f"test -f {_quote_remote_path(f'{task_dir}/exit_code')} && "
            f"cat {_quote_remote_path(f'{task_dir}/exit_code')}",
            timeout=10,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            if result.returncode == 255 or _looks_like_ssh_transport_error(detail):
                raise RuntimeError(detail or "SSH status check failed")
            return None
        text = result.stdout.strip()
        if not text:
            return None
        return int(text)

    def session_exists(self, worker: WorkerConfig, session: str) -> bool:
        """Return whether the remote tmux session still exists.

        The command deliberately converts tmux's missing-session exit status into
        a marker so an absent session can be distinguished from an SSH failure.
        """
        result = SshClient(worker).run(
            f"if tmux has-session -t {shlex.quote(session)} 2>/dev/null; then "
            "printf 'present'; else printf 'missing'; fi",
            timeout=10,
        )
        if not result.ok:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "tmux session check failed")
        marker = result.stdout.strip()
        if marker == "present":
            return True
        if marker == "missing":
            return False
        raise RuntimeError(f"unexpected tmux session check result: {marker!r}")

    def tail_logs(self, worker: WorkerConfig, task_id: str, lines: int = 20) -> str:
        task_dir = self.task_dir(task_id)
        client = SshClient(worker)
        result = client.run(
            f"if test -f {_quote_remote_path(f'{task_dir}/terminal.raw.log')}; then "
            f"tail -n {int(lines)} {_quote_remote_path(f'{task_dir}/terminal.raw.log')}; "
            f"else "
            f"tail -n {int(lines)} {_quote_remote_path(f'{task_dir}/stdout.log')} "
            f"{_quote_remote_path(f'{task_dir}/stderr.log')}; "
            f"fi 2>/dev/null || true",
            timeout=10,
        )
        return result.stdout.rstrip("\n")

    async def open_nvtop(
        self,
        worker: WorkerConfig,
        *,
        cols: int = 120,
        rows: int = 36,
    ) -> RemotePtySession:
        master_fd, slave_fd = pty.openpty()
        try:
            _set_pty_size(slave_fd, cols, rows)
            command = (
                "if ! command -v nvtop >/dev/null 2>&1; then "
                "printf 'gpu-watcher: nvtop is not installed on this worker\r\n'; exit 127; "
                "fi; exec env TERM=xterm-256color nvtop"
            )
            process = await asyncio.create_subprocess_exec(
                *SshClient(worker).command(command, force_tty=True),
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                close_fds=True,
            )
        except Exception:
            os.close(master_fd)
            raise
        finally:
            os.close(slave_fd)
        return RemotePtySession(process, master_fd)

    async def open_log_stream(
        self,
        worker: WorkerConfig,
        task_id: str,
        *,
        tail_bytes: int = 262144,
    ) -> asyncio.subprocess.Process:
        client = SshClient(worker)
        command = self._log_stream_command(task_id, max(0, int(tail_bytes)))
        return await asyncio.create_subprocess_exec(
            *client.command(command),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

    def kill_session(self, worker: WorkerConfig, session: str) -> None:
        result = SshClient(worker).run(_kill_session_command(session), timeout=15)
        if not result.ok:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "failed to kill tmux session")

    def kill_task(self, worker: WorkerConfig, task: Task) -> None:
        session = task.tmux_session or self.session_name(worker, task.id)
        self.kill_session(worker, session)

    def session_name(self, worker: WorkerConfig, task_id: str) -> str:
        safe_id = task_id.replace("-", "")[:12]
        return f"{worker.tmux_prefix}-{safe_id}"

    def task_dir(self, task_id: str) -> str:
        return f"{self.remote_base_dir.rstrip('/')}/tasks/{task_id}"

    def _render_task_script(self, task: Task, gpu_indexes: list[int], task_dir: str) -> str:
        command_b64 = base64.b64encode(task.command.encode("utf-8")).decode("ascii")
        env_exports = "\n".join(
            f"export {key}={shlex.quote(value)}"
            for key, value in sorted(task.env.items())
            if key.isidentifier()
        )
        cwd_line = _render_cwd_line(task.cwd) if task.cwd else ""
        cuda_devices = ",".join(map(str, gpu_indexes))
        log_mode = task.log_mode if task.log_mode in {"pty", "pipe"} else "pty"
        return f"""#!/usr/bin/env bash
set +e
REMOTE_DIR={_quote_remote_path(task_dir)}
mkdir -p "$REMOTE_DIR"
COMMAND_FILE="$REMOTE_DIR/command.sh"
RAW_LOG="$REMOTE_DIR/terminal.raw.log"
STDOUT_LOG="$REMOTE_DIR/stdout.log"
STDERR_LOG="$REMOTE_DIR/stderr.log"
COMMAND_EXIT="$REMOTE_DIR/command_exit_code"
rm -f "$REMOTE_DIR/exit_code" "$REMOTE_DIR/finished_at" "$COMMAND_EXIT"
: > "$RAW_LOG"
: > "$STDOUT_LOG"
: > "$STDERR_LOG"
printf '%s' {shlex.quote(command_b64)} | base64 -d > "$COMMAND_FILE"
chmod 700 "$COMMAND_FILE"
export GPU_WATCHER_TASK_ID={shlex.quote(task.id)}
export CUDA_VISIBLE_DEVICES={shlex.quote(cuda_devices)}
export GPU_WATCHER_LOG_MODE={shlex.quote(log_mode)}
{env_exports}
{cwd_line}
if [ "$GPU_WATCHER_LOG_MODE" = "pty" ] && command -v script >/dev/null 2>&1; then
  export GPU_WATCHER_COMMAND_FILE="$COMMAND_FILE"
  export GPU_WATCHER_COMMAND_EXIT="$COMMAND_EXIT"
  script -q -f -c 'bash "$GPU_WATCHER_COMMAND_FILE"; code=$?; printf "%s" "$code" > "$GPU_WATCHER_COMMAND_EXIT"; exit "$code"' "$RAW_LOG"
  script_status=$?
  if [ -s "$COMMAND_EXIT" ]; then
    code="$(cat "$COMMAND_EXIT")"
  else
    code="$script_status"
  fi
else
  if [ "$GPU_WATCHER_LOG_MODE" = "pty" ]; then
    printf 'gpu-watcher: remote script(1) not found; falling back to pipe log mode\\n' | tee -a "$STDERR_LOG" >> "$RAW_LOG"
  fi
  bash "$COMMAND_FILE" > >(tee -a "$STDOUT_LOG" >> "$RAW_LOG") 2> >(tee -a "$STDERR_LOG" >> "$RAW_LOG")
  code=$?
fi
printf "%s" "$code" > "$REMOTE_DIR/exit_code"
date -Is > "$REMOTE_DIR/finished_at"
exit "$code"
"""

    def _log_stream_command(self, task_id: str, tail_bytes: int) -> str:
        task_dir = self.task_dir(task_id)
        raw = _quote_remote_path(f"{task_dir}/terminal.raw.log")
        stdout = _quote_remote_path(f"{task_dir}/stdout.log")
        stderr = _quote_remote_path(f"{task_dir}/stderr.log")
        return (
            "i=0; "
            f"while [ $i -lt 50 ] && [ ! -f {raw} ] && [ ! -f {stdout} ]; do "
            "i=$((i+1)); sleep 0.2; "
            "done; "
            f"if [ -f {raw} ]; then "
            f"tail -c {int(tail_bytes)} -F {raw} 2>/dev/null; "
            f"elif [ -f {stdout} ] || [ -f {stderr} ]; then "
            f"tail -c {int(tail_bytes)} -F {stdout} {stderr} 2>/dev/null; "
            "else "
            "printf 'gpu-watcher: log file not found\\r\\n'; "
            "fi"
        )


def _quote_remote_path(path: str) -> str:
    if path == "~":
        return '"$HOME"'
    if path.startswith("~/"):
        return f'"$HOME/{_escape_double_quoted(path[2:])}"'
    return shlex.quote(path)


def _looks_like_ssh_transport_error(detail: str) -> bool:
    text = detail.lower()
    return any(
        marker in text
        for marker in (
            "connection closed",
            "connection reset",
            "connection refused",
            "connection timed out",
            "connection timeout",
            "no route to host",
            "network is unreachable",
            "could not resolve hostname",
            "kex_exchange_identification",
            "broken pipe",
            "ssh_exchange_identification",
        )
    )


def _set_pty_size(fd: int, cols: int, rows: int) -> None:
    size = struct.pack("HHHH", max(1, int(rows)), max(1, int(cols)), 0, 0)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, size)


def _write_fd(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def _kill_session_command(session: str) -> str:
    quoted = shlex.quote(session)
    return f"""
set +e
session={quoted}
root="$(tmux list-panes -t "$session" -F '#{{pane_pid}}' 2>/dev/null | head -n 1 || true)"
collect_tree() {{
  pid="$1"
  [ -n "$pid" ] || return 0
  echo "$pid"
  for child in $(pgrep -P "$pid" 2>/dev/null || true); do
    collect_tree "$child"
  done
}}
dedupe_pids() {{
  awk 'NF && !seen[$1]++'
}}
kill_pids() {{
  sig="$1"
  pids="$2"
  for pid in $pids; do
    [ -n "$pid" ] || continue
    pgid="$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ' || true)"
    if [ -n "$pgid" ] && [ "$pgid" != "$$" ] && [ "$pgid" != "1" ]; then
      kill "-$sig" -- "-$pgid" 2>/dev/null || true
    fi
    kill "-$sig" "$pid" 2>/dev/null || true
  done
}}
if [ -n "$root" ]; then
  pids="$(collect_tree "$root" | dedupe_pids)"
  kill_pids TERM "$pids"
fi
tmux kill-session -t "$session" 2>/dev/null || true
sleep 0.5
if [ -n "$root" ]; then
  remaining="$(collect_tree "$root" | dedupe_pids)"
  kill_pids KILL "$pids $remaining"
fi
exit 0
"""


def _render_cwd_line(cwd: str) -> str:
    quoted = shlex.quote(cwd)
    return (
        f"if ! cd {quoted}; then\n"
        f"  printf 'gpu-watcher: failed to cd to %s\\n' {quoted} | "
        "tee -a \"$STDERR_LOG\" >> \"$RAW_LOG\"\n"
        "  code=90\n"
        "  printf \"%s\" \"$code\" > \"$REMOTE_DIR/exit_code\"\n"
        "  date -Is > \"$REMOTE_DIR/finished_at\"\n"
        "  exit \"$code\"\n"
        "fi"
    )


def _escape_double_quoted(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("$", "\\$")
        .replace("`", "\\`")
    )
