# gpu-watcher CLI Reference

## Command Overview

```bash
gpu-watcher daemon --config .local/config.toml [--log-level INFO]
gpu-watcher hash-password
gpu-watcher submit [--api-url URL|--config FILE|--db DB] [options] [--background] -- COMMAND
gpu-watcher tasks|list [--api-url URL|--config FILE|--db DB] [--status STATUS] [--limit N] [--json]
gpu-watcher show [--api-url URL|--config FILE|--db DB] [--json] TASK_ID
gpu-watcher events [--api-url URL|--config FILE|--db DB] [--json] TASK_ID
gpu-watcher wait [--api-url URL|--config FILE|--db DB] [--json] [--timeout SECONDS] [--interval SECONDS] TASK_ID
gpu-watcher logs [--api-url URL|--config FILE|--db DB] [--lines N] TASK_ID
gpu-watcher start [--api-url URL|--config FILE|--db DB] TASK_ID
gpu-watcher pause [--api-url URL|--config FILE|--db DB] TASK_ID
gpu-watcher rerun [--api-url URL|--config FILE|--db DB] TASK_ID
gpu-watcher terminate [--api-url URL|--config FILE|--db DB] TASK_ID
gpu-watcher gpus [--api-url URL|--config FILE|--db DB] [--worker NAME] [--json]
gpu-watcher workers [--api-url URL|--config FILE] [--json]
gpu-watcher status [--api-url URL|--config FILE|--db DB] [--json]
gpu-watcher cancel [--api-url URL|--config FILE|--db DB] TASK_ID
gpu-watcher tick --config .local/config.toml
```

Connection resolution:

1. `--api-url URL`: talk to the daemon API.
2. `--config FILE`: local control-node mode; reads DB path and SSH settings.
3. `--db DB`: direct local SQLite mode.
4. No connection argument: daemon API at `$GPU_WATCHER_API_URL`, else `http://127.0.0.1:8765`.

The built-in CLI marker is accepted only on a direct loopback connection, so no username or token is required. Run CLI and agent operations on the control node. The public reverse proxy is Web-only.

## Submit

```bash
gpu-watcher submit \
  [--name NAME] \
  [--priority INT] \
  [--gpus INT] \
  [--worker NAME|auto] \
  [--cwd REMOTE_DIR] \
  [--env KEY=VALUE]... \
  [--max-memory-used-mb INT] \
  [--max-utilization-percent INT] \
  [--min-free-seconds INT] \
  [--preemptible] \
  [--allow-preempt] \
  [--background] \
  [--log-mode pty|pipe] \
  [--im-notify] \
  [--callback-command COMMAND] \
  [--callback-on EVENT]... \
  [--paused] \
  [--wait] \
  [--wait-timeout SECONDS] \
  [--wait-interval SECONDS] \
  -- COMMAND
```

Semantics:

- `--worker auto` or omitted: scheduler may choose any suitable enabled worker.
- `--worker NAME`: task can only run on that configured worker.
- `--priority`: higher runs first.
- `--gpus`: exact GPU count for regular tasks; hard maximum for elastic tasks, which may run on any count from 1 through this value.
- `--cwd`: remote working directory on the GPU host.
- `--env`: repeatable environment variables.
- `--max-memory-used-mb`, `--max-utilization-percent`: per-task launch thresholds.
- `--min-free-seconds`: GPU must continuously satisfy thresholds before launch.
- `--preemptible`: this task may be stopped and requeued by a higher-priority task.
- `--allow-preempt`: this task may preempt lower-priority preemptible tasks.
- `--background`: run only in idle capacity, count against the worker's `max_background_tasks` sub-limit and the shared `max_concurrent_tasks` GPU-slot limit, and automatically yield to higher priority regular tasks.
- `--elastic`: allow a background task to start with one suitable GPU and resize up to the `--gpus` hard maximum as capacity changes.
- `--log-mode pty`: default; capture a raw terminal log with ANSI and carriage-return progress output preserved for the Web UI.
- `--log-mode pipe`: use plain stdout/stderr pipes when the program must not detect a TTY.
- `--im-notify`: send built-in daemon-side IM notifications for `succeeded`, `failed`, and `switch` events using the daemon's `[im_notify]` config.
- `--callback-command`: shell command executed by the daemon/control node for selected callback events. It supports `{title}`, `{content}`, `{event}`, `{task_id}`, `{task_name}`, `{status}`, `{worker}`, `{gpus}`, `{exit_code}`, and `{detail}` placeholders.
- `--callback-on`: callback event, one of `succeeded`, `failed`, or `switch`; repeatable or comma-separated. If a callback command is set and no events are provided, all three are enabled.
- `--paused`: create the task in `paused` state so it will not run until `gpu-watcher start TASK_ID`.
- `--wait`: after submission, block until the task reaches `succeeded`, `failed`, or `canceled`, print the final task JSON, and exit with the task result.
- `--wait-timeout`: maximum seconds to wait with `--wait`; omit for no timeout.
- `--wait-interval`: polling interval for `--wait`, default 5 seconds.

Use `--wait` only for tasks expected to finish quickly, such as smoke tests or short agent checks. For long-running training jobs, submit normally and monitor with `status`, `show`, `events`, and `logs`.

Examples:

```bash
gpu-watcher submit --priority 50 -- 'python train.py'
gpu-watcher submit --worker gpu-node-1 --gpus 2 -- 'bash run.sh'
gpu-watcher submit --env WANDB_MODE=offline --cwd /srv/exp -- 'python train.py'
gpu-watcher submit --background --priority 0 -- 'python fill_idle_time.py'
gpu-watcher submit --paused --name manual -- 'python train.py'
gpu-watcher submit --wait --wait-timeout 300 --name smoke -- 'python smoke.py'
gpu-watcher submit --im-notify --name train -- 'python train.py'
gpu-watcher submit --callback-command '/path/to/notify {title} {content}' --callback-on succeeded -- 'python train.py'
```

## Progress And Results

Use:

```bash
gpu-watcher status
gpu-watcher tasks
gpu-watcher show <task-id>
gpu-watcher events <task-id>
gpu-watcher wait --timeout 300 <short-task-id>
```

Important `show` fields:

- `id`
- `name`
- `command`
- `status`
- `priority`
- `requested_gpus`
- `target_worker`
- `assigned_worker`
- `assigned_gpus`
- `started_at`
- `finished_at`
- `duration_seconds`
- `exit_code`
- `message`
- `background`
- `im_notify`
- `log_mode`

`wait` blocks until a task reaches a terminal status, prints the final task, and exits 0 for `succeeded`, the task exit code for `failed`, or 130 for `canceled`. Use it only when the expected runtime is short.

## Manual Task Control

```bash
gpu-watcher start <task-id>
gpu-watcher pause <task-id>
gpu-watcher rerun <failed-task-id>
gpu-watcher terminate <task-id>
```

Semantics:

- `start`: move a paused task back to `queued` and request immediate scheduling. If the task is already queued, it requests an immediate dispatch tick.
- `pause`: only valid for preemptible tasks, including background tasks; mark a queued task `paused`, or kill a running task's process tree and tmux session before marking it `paused`.
- `terminate`: cancel a queued, paused, or running task. If running, only the task's own tmux session is killed.
- `rerun`: return a failed task to the queue and request immediate scheduling.
- `cancel`: compatibility alias for `terminate`.

`tasks` prints a table including status, priority, GPU count, actual worker, target worker, devices, duration, preemption policy, and name.

## Logs

Preferred daemon/API mode:

```bash
gpu-watcher logs <task-id>
gpu-watcher logs --lines 200 <task-id>
gpu-watcher logs <task-id>
```

In API mode, the daemon tails logs over SSH from the control node, so the CLI client does not need SSH keys or `config.toml`.

The Web UI uses `GET /tasks/<task-id>/logs/stream` over WebSocket and xterm.js to display the raw terminal log for new `pty` tasks. The CLI `logs` command remains line-oriented and uses the HTTP logs endpoint.

Local control-node mode:

```bash
gpu-watcher logs --config .local/config.toml --lines 200 <task-id>
```

If the task has not started, logs may fail with "task has no assigned worker".

## Workers And GPUs

```bash
gpu-watcher workers
gpu-watcher workers --json
gpu-watcher gpus
gpu-watcher gpus --worker gpu-node-1
gpu-watcher gpus --json
```

`workers` shows configured worker name, host, user, port, enabled state, and GPU allowlist.
It also shows `max_concurrent_tasks` and `max_background_tasks` when configured.
`max_concurrent_tasks` is the hard limit on GPUs assigned to all gpu-watcher tasks on the worker; `max_background_tasks` limits background task instances within that shared capacity.

`gpus` shows last observed condition state:

- `worker_name`
- `gpu_index`
- `condition_key`
- `is_free`
- `free_since`
- `last_seen`
- `memory_used_mb`
- `utilization_percent`
- `numa_node`: PCI device NUMA node detected from sysfs, or null when unavailable
- `process_users`: unique usernames of compute processes currently using the GPU; empty when no user is observed
- `processes`: current compute process snapshots containing `pid`, `memory_used_mb`, `username`, and `cmdline`

An empty GPU table usually means the daemon has not yet probed workers, all workers are disabled, or worker probes are failing.

## Daemon And Scheduler

Start the control-node daemon:

```bash
gpu-watcher daemon --config .local/config.toml
gpu-watcher daemon --config .local/config.toml --log-level DEBUG
```

Generate an Argon2id hash for `[auth]` setup:

```bash
gpu-watcher hash-password
```

The daemon reads `GPU_WATCHER_PASSWORD_HASH` and `GPU_WATCHER_SESSION_SECRET` from its environment. Keep them in a mode-`0600` environment file and keep the API listener on loopback behind an HTTPS reverse proxy.

Run exactly one scheduler iteration from the control node:

```bash
gpu-watcher tick --config .local/config.toml
```

Use `tick` for debugging queue decisions. Normal deployments should use the daemon loop.

## HTTP API Equivalents

```bash
GET  /health
GET  /login
POST /auth/login
GET  /auth/session
POST /auth/logout
GET  /workers
PUT  /workers/<worker-name>
GET  /gpus
GET  /tasks
GET  /tasks/<task-id>
PUT  /tasks/<task-id>
GET  /tasks/<task-id>/events
GET  /tasks/<task-id>/logs?lines=100
WS   /tasks/<task-id>/logs/stream?tail_bytes=262144
POST /tasks/<task-id>/start
POST /tasks/<task-id>/pause
POST /tasks/<task-id>/rerun
POST /tasks/<task-id>/terminate
POST /tasks
POST /tasks/<task-id>/cancel
```

`PUT /workers/<worker-name>` is used by the Infrastructure Web editor. It persists mutable worker fields to the daemon TOML file and hot-updates the scheduler; the worker name cannot be changed.

All task, worker, GPU, log, and session routes require either a signed browser session or a direct local CLI request. Bearer authentication is not supported. `/health`, `/login`, static assets, and `POST /auth/login` are public. Browser writes require the session CSRF header, and the log WebSocket requires a same-origin browser request.

Task creation JSON accepts:

```json
{
  "name": "train",
  "command": "python train.py",
  "priority": 100,
  "requested_gpus": 1,
  "target_worker": "gpu-node-1",
  "cwd": "/srv/exp",
  "env": {"KEY": "VALUE"},
  "max_memory_used_mb": 1024,
  "max_utilization_percent": 5,
  "min_free_seconds": 60,
  "preemptible": false,
  "allow_preempt": true,
  "background": false,
  "im_notify": true,
  "log_mode": "pty",
  "paused": false
}
```

Use `"target_worker": "auto"` or omit `target_worker` for automatic worker selection.
`PUT /tasks/<task-id>` replaces the same task options, except `paused`; it succeeds only for paused tasks or queued tasks that have not started. Use the lifecycle endpoints to start, pause, rerun, or terminate a task.

## Troubleshooting

- Unknown worker on submit: run `gpu-watcher workers`; use the exact `name`.
- Task is queued forever: check `gpu-watcher gpus`, worker enabled state, launch thresholds, and `min_free_seconds`.
- Logs fail with no assigned worker: task has not started.
- Logs fail over API: daemon/control node may lack SSH reachability to the assigned worker.
- CLI receives API 401: run it on the daemon's control node against the loopback API URL.
- Client cannot connect: set `--api-url` or `GPU_WATCHER_API_URL`, and check daemon `/health`.
- Need raw machine-readable output: add `--json` to commands that support it.
