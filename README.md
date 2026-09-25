# gpu-watcher

`gpu-watcher` is a small GPU task manager for environments where GPU hosts cannot run a root-level daemon.

The daemon runs on an independent control node. GPU hosts only need:

- SSH access as a normal user
- `tmux`
- `nvidia-smi`
- a shell such as `bash`

It provides:

- Persistent SQLite task queue
- Priority scheduling
- Multi-GPU task requests
- Remote launch through `ssh` + `tmux`
- No root shell and no agent process on target machines
- Built-in Web UI and HTTP API exposed by the central daemon

## Agent Skill

The reusable agent skill is included in [skills/gpu-watcher-cli](skills/gpu-watcher-cli/SKILL.md), with CLI reference documentation and agent metadata. Copy that directory into your agent's skills directory to install it.

## Quick Start

Create a private local config directory. Everything under `.local/` is ignored by Git:

```bash
mkdir -p .local
cp config.example.toml .local/config.toml
chmod 700 .local
chmod 600 .local/config.toml
```

Edit `.local/config.toml`, then start the central daemon:

```bash
export GPU_WATCHER_PASSWORD_HASH="$(gpu-watcher hash-password)"
export GPU_WATCHER_SESSION_SECRET="$(openssl rand -base64 48)"
gpu-watcher daemon --config .local/config.toml
```

Open the Web UI:

```text
http://127.0.0.1:8765/
```

Submit a task:

```bash
gpu-watcher submit --priority 100 --gpus 1 --name train -- \
  'python train.py --epochs 20'
```

Submit a short task and wait for its final result:

```bash
gpu-watcher submit --wait --wait-timeout 300 --name smoke -- 'python smoke.py'
gpu-watcher wait --timeout 300 <task-id>
```

Use blocking waits only for short smoke tests or agent checks. Long-running training jobs should be submitted normally and monitored with `status`, `show`, `events`, and `logs`.

Submit a task with daemon-side callbacks. The callback command runs on the control node, not on the GPU worker:

```bash
gpu-watcher submit --worker gpu-node-1 \
  --callback-command '/path/to/notify {title} {content}' \
  --callback-on succeeded --callback-on failed --callback-on switch -- \
  'python train.py'
```

Submit a task with built-in IM notifications using the daemon's `[im_notify]` config:

```bash
gpu-watcher submit --im-notify --name train -- 'python train.py'
```

Create a task that waits for manual start:

```bash
gpu-watcher submit --paused --name train -- 'python train.py'
gpu-watcher start <task-id>
```

Schedule a long-running task to start and stop on weekdays. Cron schedules use five fields (`minute hour day-of-month month day-of-week`) and an explicit IANA timezone. A scheduled stop pauses the task so the next start window can queue it again:

```bash
gpu-watcher submit --paused --name weekday-train \
  --start-cron '0 9 * * mon-fri' \
  --stop-cron '0 18 * * mon-fri' \
  --schedule-timezone Asia/Shanghai -- \
  'python train.py'
```

By default, the CLI talks to the daemon at `http://127.0.0.1:8765`. Local CLI requests are recognized automatically and do not require a username or token. Run the CLI on the control node; the public reverse proxy is Web-only.

```bash
gpu-watcher submit --priority 100 -- 'python train.py'
```

Let the scheduler choose any suitable worker, or pin a task to one worker:

```bash
gpu-watcher submit --worker auto --priority 100 -- 'python train.py'
gpu-watcher submit --worker gpu-node-1 --priority 100 -- 'python train.py'
```

Submit a task that waits until a GPU has stayed quiet for at least 60 seconds:

```bash
gpu-watcher submit --priority 100 --gpus 1 \
  --max-memory-used-mb 1024 \
  --max-utilization-percent 5 \
  --min-free-seconds 60 \
  --name train -- 'python train.py'
```

Submit a low priority task that may yield to higher priority work:

```bash
gpu-watcher submit --priority 10 --preemptible -- \
  'python background_job.py'
```

Submit a background task. Background tasks run only in idle capacity and are automatically requeued when a higher priority regular task needs their GPU:

```bash
gpu-watcher submit --background --priority 0 -- \
  'python fill_idle_time.py'
```

Submit a high priority task that may preempt lower priority `gpu-watcher` tasks:

```bash
gpu-watcher submit --priority 1000 --allow-preempt -- \
  'python urgent_job.py'
```

List tasks:

```bash
gpu-watcher tasks --config .local/config.toml
```

Inspect the last monitored GPU condition states:

```bash
gpu-watcher gpus --config .local/config.toml
```

Pause, resume, or terminate a task:

```bash
gpu-watcher pause --config .local/config.toml <task-id>
gpu-watcher start --config .local/config.toml <task-id>
gpu-watcher rerun --config .local/config.toml <failed-task-id>
gpu-watcher terminate --config .local/config.toml <task-id>
```

## Command Line Interface

The CLI supports two operating modes.

Direct control-node mode reads the SQLite DB path and SSH settings from config:

```bash
gpu-watcher status --config .local/config.toml
gpu-watcher workers --config .local/config.toml
gpu-watcher tasks --config .local/config.toml
gpu-watcher show --config .local/config.toml <task-id>
gpu-watcher events --config .local/config.toml <task-id>
gpu-watcher wait --config .local/config.toml --timeout 300 <short-task-id>
gpu-watcher logs --config .local/config.toml <task-id>
gpu-watcher tick --config .local/config.toml
```

Local daemon mode talks to the running daemon over loopback. If `--api-url`, `--config`, and `--db` are omitted, commands default to `http://127.0.0.1:8765`; no credentials are required.

```bash
gpu-watcher status
gpu-watcher submit --priority 100 -- 'python train.py'
gpu-watcher submit --worker gpu-node-1 -- 'python train.py'
gpu-watcher logs <task-id>
gpu-watcher pause <task-id>
gpu-watcher start <task-id>
gpu-watcher rerun <failed-task-id>
gpu-watcher terminate <task-id>
```

The `logs` command in API mode asks the daemon to tail the remote task logs, so the client machine does not need SSH access or `config.toml`:

```bash
gpu-watcher logs <task-id>
gpu-watcher logs --lines 200 <task-id>
```

New tasks default to `--log-mode pty`, which captures output through a pseudo-terminal into `terminal.raw.log` on the worker. The Web UI streams that raw terminal log over WebSocket and renders it with xterm.js, preserving ANSI colors and carriage-return progress updates from tools such as `tqdm`. Use `--log-mode pipe` for tasks that must not detect a TTY.

For local scripting or tests, commands that only need queue state can still use `--db ./.local/gpu-watcher.db`.

Task detail output includes the actual placement and timing fields:

- `assigned_worker`: actual worker that ran the task
- `assigned_gpus`: actual GPU indexes assigned on that worker
- `started_at` and `finished_at`: run timestamps
- `duration_seconds`: elapsed run time; running tasks use the current time
- `exit_code`: final process exit code
- `status`: `paused` means the scheduler will not run the task until `start` is requested

## HTTP API

When `[api].enabled = true`, protected routes accept either a signed Web session or the built-in CLI marker on a direct loopback connection. The marker is not accepted through a reverse proxy, and Bearer authentication is not supported. `/health`, login assets, and the login endpoint remain public. Browser mutations additionally require the session CSRF token.

Use the CLI for scripts and agent operations on the control node. The Internet-facing endpoint is intentionally Web-only.

`PUT /workers/<worker-name>` updates a worker's connection, enabled state, GPU allowlist, tmux prefix, and concurrency limits. Worker names are immutable so historical task references remain valid. The Infrastructure tab exposes this endpoint through each worker's Edit dialog; successful changes are written atomically to the daemon TOML file and applied without a restart.

Task options can be replaced with `PUT /tasks/<task-id>` only while the task is paused or queued and has not started. The Web task details dialog exposes the same operation through its `Edit` button. Lifecycle state remains controlled by the start, pause, rerun, and terminate actions.

## Scheduling Model

The scheduler sorts queued tasks by:

1. Higher `priority`
2. Earlier `created_at`

For each enabled worker, it probes GPU state with `nvidia-smi` over SSH. The same probe reads each GPU PCI device's NUMA node from sysfs, falling back to `nvidia-smi topo -m` when sysfs reports an unknown node, and records the compute process PID, GPU memory, username, and command line. The Infrastructure view exposes this snapshot through Status Detail. It does not start a separate monitor and does not require root. A GPU is considered free when its memory and utilization are below the task's thresholds, or the daemon defaults if the task does not override them, and it is not already assigned to a running `gpu-watcher` task.

For multi-GPU tasks, the scheduler first prefers a complete allocation within one known NUMA node, then compares GPU utilization and memory use. If a lower-priority background task, or a regular preemptible task allowed by the incoming task's `--allow-preempt`, blocks an otherwise complete same-NUMA allocation, the scheduler preempts the smallest suitable victim set, probes the worker again, dispatches the higher-priority task within that NUMA node, and immediately considers the displaced work for restart on remaining capacity. If no same-NUMA allocation exists or topology cannot be detected, it falls back to the normal low-load ordering.

Each worker can set `max_concurrent_tasks`; when set, it is a hard limit on the total number of GPUs assigned to gpu-watcher tasks on that worker. Multi-GPU and elastic tasks consume one slot for each assigned GPU.

For regular tasks, `gpus` is the exact number required before launch. For elastic tasks, it is a hard maximum: the task may start with one suitable GPU and resize between one and `gpus`, subject to the worker limit. An elastic task never receives more GPUs than it requested.

Each worker can also set `max_background_tasks`. This is a sub-limit on the number of background task instances, not additional capacity beyond `max_concurrent_tasks`. Background tasks run after regular queued tasks have been considered and are intended for opportunistic work. A higher priority regular task can preempt lower priority background tasks without requiring `allow_preempt=true`; the background task is returned to the queue and can resume later.

Tasks can also require `min_free_seconds`, which means the GPU must continuously satisfy the requested memory and utilization thresholds before the task starts.

Preemption is intentionally scoped. A task with `allow_preempt=true` can stop and requeue lower priority tasks only when those tasks were launched by `gpu-watcher` and submitted with `preemptible=true`. It never kills arbitrary external GPU processes on the target host.

When a task starts, the control node creates a task directory on the GPU host, uploads a small shell script, then starts it inside a detached `tmux` session. The task runs with `CUDA_VISIBLE_DEVICES` set to the selected GPU indexes.

Manual task control is scoped to tasks launched by `gpu-watcher`. `pause` is allowed only for preemptible tasks, including background tasks; it marks a queued task as `paused`, or kills a running task's process tree and tmux session before marking it `paused`. `start` returns a paused task to the queue and immediately triggers scheduling. `rerun` returns a failed task to the queue. `terminate` cancels queued, paused, or running tasks; for running tasks it first kills that task's process tree and tmux session. The old `cancel` CLI/API remains as a compatibility alias for termination.

Tasks may also define `start_cron` and/or `stop_cron`, through the CLI, `POST`/`PUT /tasks`, or the Web UI. Each expression uses standard five-field cron syntax: lists, ranges, steps, named months/weekdays, and `@hourly`/`@daily`-style aliases are supported. `schedule_timezone` is an IANA timezone and defaults to `UTC`. The scheduler evaluates schedules once per minute and records each firing persistently, so daemon polling and restarts do not duplicate an action within the same minute. A matching start moves a paused task to the queue; a matching stop pauses a queued or running task and deliberately overrides the normal manual `preemptible` restriction. If start and stop both match in one minute, stop wins. Schedules never rerun terminal tasks automatically.

Task callbacks are optional per-task shell commands run by the daemon/control node. Supported events are `succeeded`, `failed`, and `switch`; `switch` covers preemption or elastic resize events that requeue a running task. Callback failures do not change task status; they are recorded as `callback_failed` events.

Built-in IM notifications are enabled per task with `--im-notify` or the Web UI checkbox. The daemon sends OneBot v11 HTTP messages using `[im_notify]` config for `succeeded`, `failed`, and `switch` task events, and records `im_notify` or `im_notify_failed` task events.

Task logs are stored under the remote task directory. For new tasks, `terminal.raw.log` is the primary log source used by the Web UI. Legacy `stdout.log` and `stderr.log` remain available as a fallback and for compatibility with older task runs.

## Notes

- `gpu-watcher` intentionally does not kill arbitrary foreign GPU processes.
- Canceling a running task kills only the tmux session created for that task.
- Preempted tasks are returned to the queue and can run again later.
- If SSH is unavailable while a task is running, the daemon keeps the task in `running` and retries status checks.

See [docs/architecture.md](docs/architecture.md) for the full control-node design and [deploy/gpu-watcher.service.template](deploy/gpu-watcher.service.template) for a systemd template.

For Internet-facing use, follow [docs/public-deployment.md](docs/public-deployment.md). Keep the daemon on loopback, terminate TLS at a reverse proxy, enable secure cookies, and keep secrets in a mode-`0600` environment file.

## Web 界面开发

界面使用 Vue 3、Tailwind CSS 4 和 Lucide，包含本地字体、深浅主题及移动端布局。前端源码、构建方式见 [frontend/README.md](frontend/README.md)。发布资源已生成在 `gpu_watcher/web/`，服务运行时不需要 Node.js。更新本版本后重启服务一次，以加载新增的字体资源路由。
