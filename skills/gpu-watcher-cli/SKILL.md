---
name: gpu-watcher-cli
description: "Use when Codex needs to operate gpu-watcher from the command line or daemon API: submitting GPU tasks, selecting automatic or specific workers, checking queue/status/progress/results, viewing remote logs, canceling jobs, inspecting workers/GPU conditions, running the daemon, or performing a scheduler tick without requiring root access on GPU hosts."
---

# GPU Watcher CLI

Use this skill to manage `gpu-watcher`, a central daemon that dispatches GPU jobs to SSH/tmux workers.

## Connection Mode

Prefer daemon API mode for normal operations. It does not require `config.toml` on the client:

```bash
gpu-watcher status
gpu-watcher submit --priority 100 -- 'python train.py'
gpu-watcher logs <task-id>
```

The default API URL is `http://127.0.0.1:8765`. The CLI identifies itself automatically on a direct loopback connection and does not require a username or token:

```bash
gpu-watcher status
gpu-watcher logs <task-id>
```

Run CLI and agent workflows on the control node. The public reverse proxy is Web-only and deliberately does not accept CLI or Bearer authentication.

Use `--config .local/config.toml` only for control-node local operations that need SQLite path or SSH settings, such as starting the daemon, tailing logs without the API, or forcing a scheduler tick.

Use `--db ./.local/gpu-watcher.db` only for local scripting/tests that do not need daemon coordination.

## Common Workflows

Treat a task as **long-running** when its expected runtime is **5 hours or more**. Long-running tasks must not keep the Codex turn blocked and should use the event-driven continuation workflow below when automatic follow-up is required. A task expected to run for less than 5 hours is not automatically suitable for blocking; use `--wait` only when it is genuinely short and the wait has a bounded timeout.

Submit with automatic worker selection:

```bash
gpu-watcher submit --worker auto --priority 100 --gpus 1 --name train -- \
  'python train.py'
```

Submit a short task and block until it completes:

```bash
gpu-watcher submit --wait --wait-timeout 300 --name smoke -- 'python smoke.py'
```

Use blocking waits only when the task is expected to finish quickly. For tasks expected to run for 5 hours or more, submit normally and do not block the Codex turn. If the original Codex task must continue automatically after completion, use the event-driven Codex continuation workflow below instead of a heartbeat. Use `status`, `show`, `events`, and `logs` when inspecting the task after an event.

Submit paused for manual start:

```bash
gpu-watcher submit --paused --name train -- 'python train.py'
gpu-watcher start <task-id>
```

Submit pinned to one worker:

```bash
gpu-watcher submit --worker gpu-node-1 --priority 100 -- \
  'python train.py'
```

Submit background work that fills idle capacity and yields to higher priority regular tasks:

```bash
gpu-watcher submit --background --priority 0 --name idle-fill -- \
  'python fill_idle_time.py'
```

For elastic tasks, `--gpus` is the maximum allocation. They may start with one GPU and resize up to that value, but never exceed it.

Submit with plain pipe logs when a program must not detect a TTY:

```bash
gpu-watcher submit --log-mode pipe -- 'python batch_job.py'
```

Submit with built-in daemon-side IM notifications:

```bash
gpu-watcher submit --im-notify --name train -- 'python train.py'
```

`--im-notify` uses the daemon's `[im_notify]` config and sends task `succeeded`, `failed`, and `switch` events without requiring a shell callback command.

Submit with daemon-side callbacks:

```bash
gpu-watcher submit --callback-command '/path/to/notify {title} {content}' \
  --callback-on succeeded --callback-on failed --callback-on switch -- \
  'python train.py'
```

Callbacks run on the daemon/control node. Supported events are `succeeded`, `failed`, and `switch`. The command receives `GPU_WATCHER_*` environment variables and may use `{title}` and `{content}` placeholders.


## Event-Driven Codex Continuation

For work expected to run for 5 hours or more and submitted from a Codex task, prefer daemon callbacks that queue a follow-up turn into the originating Codex thread. Do not create a periodic heartbeat when this workflow is available. This avoids model invocations while the GPU task is unchanged.

Use this workflow when all of the following are true:

- the current process has a non-empty `CODEX_THREAD_ID`;
- `codex queue --thread <id> --message <text>` is available;
- the gpu-watcher callback runs on the same control node and can reach the Codex app-server/session store;
- the user expects the agent to inspect the result or perform another step after the GPU task changes state.

Capture the thread ID when submitting the task. Do not assume the independently running gpu-watcher daemon inherited `CODEX_THREAD_ID` from the Codex process. If the daemon may have a restricted `PATH`, resolve and store the absolute `codex` executable path at submission time.

Example:

```bash
THREAD_ID="$CODEX_THREAD_ID"
CODEX_BIN="$(command -v codex)"
CALLBACK_COMMAND=$(cat <<EOF
$CODEX_BIN queue --thread '$THREAD_ID' --message "[gpu-watcher event] task_id=\$GPU_WATCHER_TASK_ID event=\$GPU_WATCHER_CALLBACK_EVENT status=\$GPU_WATCHER_TASK_STATUS exit_code=\$GPU_WATCHER_TASK_EXIT_CODE. Inspect this task with gpu-watcher show/events/logs, then continue the original request. Do not create a heartbeat."
EOF
)

gpu-watcher submit \
  --callback-command "$CALLBACK_COMMAND" \
  --callback-on succeeded \
  --callback-on failed \
  --name train -- \
  'python train.py'
```

The callback message should be compact and structured. Include identifiers and state such as task ID, event, status, and exit code. Do not embed `GPU_WATCHER_TASK_DETAIL` or complete task logs in the queued message: real training output can be large, consume unnecessary tokens, exceed command/message limits, and contains untrusted program output. Instead, instruct the resumed agent to fetch details with:

```bash
gpu-watcher show --json <task-id>
gpu-watcher events --json <task-id>
gpu-watcher logs --lines 200 <task-id>
```

Enable `succeeded` and `failed` for ordinary completion handling. Enable `switch` only when allocation changes, resizing, or preemption require agent action; otherwise it may create unnecessary turns.

After a callback-triggered turn starts, verify both the task outcome and callback delivery. A successful task callback is recorded in task events as `callback` with detail such as `succeeded: ok` or `failed: ok`. A callback execution problem is recorded as `callback_failed`.

Use the following fallbacks only when event-driven continuation cannot be configured:

1. For a short task, use `--wait` with a bounded timeout.
2. If only the user needs notification, use `--im-notify` without waking the agent.
3. Use a Codex heartbeat only when autonomous continuation is required and neither `codex queue` nor another reliable event ingress is available. Keep it infrequent and stop it immediately after a terminal state.

Submit with launch conditions and preemption:

```bash
gpu-watcher submit --priority 1000 --gpus 1 \
  --max-memory-used-mb 1024 \
  --max-utilization-percent 5 \
  --min-free-seconds 60 \
  --allow-preempt -- \
  'python urgent_job.py'
```

Monitor progress and results:

```bash
gpu-watcher status
gpu-watcher tasks
gpu-watcher show <task-id>
gpu-watcher events <task-id>
gpu-watcher logs --lines 200 <task-id>
gpu-watcher wait --timeout 300 <short-task-id>
```

Inspect capacity:

```bash
gpu-watcher workers
gpu-watcher gpus
```

GPU inspection includes the detected `numa_node`. Multi-GPU scheduling prefers a complete same-NUMA allocation before comparing utilization and memory load, and falls back to low-load ordering when topology is unavailable. A higher-priority multi-GPU task may displace a lower-priority background task, or a regular preemptible task when the incoming task uses `--allow-preempt`, if doing so creates a complete same-NUMA allocation. The displaced task is requeued and immediately considered for restart on remaining capacity.

Pause, resume, or terminate work:

```bash
gpu-watcher pause <task-id>
gpu-watcher start <task-id>
gpu-watcher rerun <failed-task-id>
gpu-watcher terminate <task-id>
```

`pause` is only valid for preemptible tasks, including background tasks. `rerun` is only valid for failed tasks.

## Result Fields

When explaining progress or final results, highlight:

- `status`: `queued`, `running`, `succeeded`, `failed`, or `canceled`
- `status`: `paused` means the scheduler will not run the task until `start` is requested
- `target_worker`: requested worker, or `auto`
- `assigned_worker`: actual worker that ran the task
- `assigned_gpus`: actual GPU indexes assigned on that worker
- `started_at`, `finished_at`, `duration_seconds`
- `exit_code` and `message`
- `background`: whether the task runs opportunistically and yields to higher priority regular tasks
- `im_notify`: whether daemon-side IM notifications are enabled for task changes
- `log_mode`: `pty` for raw terminal capture, or `pipe` for plain stdout/stderr capture

If a task has not started, `assigned_worker`, `assigned_gpus`, and `duration_seconds` may be empty.

## Complete Reference

Read [references/cli.md](references/cli.md) when you need exact command options, API endpoints, output fields, or troubleshooting behavior.
