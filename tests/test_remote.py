from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

from gpu_watcher.remote import RemoteExecutor, RemoteResult, SshClient, _kill_session_command
from gpu_watcher.models import Task, WorkerConfig


class RemoteTests(unittest.TestCase):
    def test_ssh_command_uses_keepalive_options(self) -> None:
        worker = WorkerConfig(name="node-a", host="node-a", port=2202)

        command = SshClient(worker).command("tail -F log")

        self.assertIn("ServerAliveInterval=15", command)
        self.assertIn("ServerAliveCountMax=4", command)
        self.assertIn("TCPKeepAlive=yes", command)

    def test_ssh_command_can_force_a_tty_for_interactive_tools(self) -> None:
        worker = WorkerConfig(name="node-a", host="node-a", port=2202)

        command = SshClient(worker).command("nvtop", force_tty=True)

        self.assertIn("-tt", command)
        self.assertLess(command.index("-tt"), command.index(worker.ssh_target))

    def test_log_stream_can_follow_from_current_end(self) -> None:
        command = RemoteExecutor("~/.gpu-watcher")._log_stream_command("task-1", 0)

        self.assertIn("tail -c 0 -F", command)
        self.assertNotIn("tail -n 200 -F", command)

    def test_probe_gpus_maps_process_users_by_gpu_uuid(self) -> None:
        output = """0, GPU-a, 00000000:01:00.0, 1024, 24576, 20
1, GPU-b, 00000000:02:00.0, 0, 24576, 0
GPU-WATCHER-NUMA\t0\t0
GPU-WATCHER-NUMA\t1\t1
GPU-WATCHER-PROCESS\tGPU-a\t123\t768\talice\tcHl0aG9uIHRyYWluLnB5IC0tbmFtZSBhbHBoYSxiZXRh
GPU-WATCHER-PROCESS\tGPU-a\t456\t256\tbob\tcHl0aG9uIHNlcnZlLnB5
GPU-WATCHER-PROCESS\tGPU-a\t789\t1\talice\tcHl0aG9uIHNlcnZlLnB5
"""
        worker = WorkerConfig(name="node-a", host="node-a", gpus=[0, 1])

        with patch.object(
            SshClient,
            "run",
            return_value=RemoteResult(returncode=0, stdout=output, stderr=""),
        ) as run:
            states = RemoteExecutor("~/.gpu-watcher").probe_gpus(worker)

        self.assertEqual(states[0].process_users, ("alice", "bob"))
        self.assertEqual(states[0].numa_node, 0)
        self.assertEqual([process.pid for process in states[0].processes], [123, 456, 789])
        self.assertEqual(states[0].processes[0].memory_used_mb, 768)
        self.assertEqual(states[0].processes[0].username, "alice")
        self.assertEqual(states[0].processes[0].cmdline, "python train.py --name alpha,beta")
        self.assertEqual(states[1].process_users, ())
        self.assertEqual(states[1].processes, ())
        self.assertEqual(states[1].numa_node, 1)
        self.assertIn("pci.bus_id", run.call_args.args[0])
        self.assertIn("/sys/bus/pci/devices/", run.call_args.args[0])
        self.assertIn("nvidia-smi topo -m", run.call_args.args[0])
        self.assertIn("--query-compute-apps=gpu_uuid,pid", run.call_args.args[0])
        self.assertIn("used_gpu_memory", run.call_args.args[0])
        self.assertIn("ps -ww -o args=", run.call_args.args[0])

    def test_kill_session_command_is_valid_shell(self) -> None:
        completed = subprocess.run(
            ["bash", "-n"],
            input=_kill_session_command("gw-test"),
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_session_exists_distinguishes_missing_session(self) -> None:
        worker = WorkerConfig(name="node-a", host="node-a")
        with patch.object(
            SshClient,
            "run",
            return_value=RemoteResult(returncode=0, stdout="missing", stderr=""),
        ) as run:
            self.assertFalse(RemoteExecutor("~/.gpu-watcher").session_exists(worker, "gw-test"))

        self.assertIn("tmux has-session", run.call_args.args[0])
        self.assertIn("gw-test", run.call_args.args[0])

    def test_read_exit_code_raises_for_ssh_transport_failure(self) -> None:
        worker = WorkerConfig(name="node-a", host="node-a")
        with patch.object(
            SshClient,
            "run",
            return_value=RemoteResult(
                returncode=255,
                stdout="",
                stderr="Connection closed by node-a port 22",
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "Connection closed"):
                RemoteExecutor("~/.gpu-watcher").read_exit_code(worker, "task-id")

    def test_kill_task_falls_back_to_deterministic_session_name(self) -> None:
        class CapturingRemote(RemoteExecutor):
            def __init__(self) -> None:
                super().__init__("~/.gpu-watcher")
                self.killed: list[str] = []

            def kill_session(self, worker: WorkerConfig, session: str) -> None:
                self.killed.append(session)

        worker = WorkerConfig(name="node-a", host="node-a", tmux_prefix="gw")
        task = Task(
            id="12345678-1234-5678-9abc-123456789abc",
            name="test",
            command="echo test",
            priority=0,
            requested_gpus=1,
            env={},
            cwd=None,
            target_worker=None,
            status="running",
            created_at="",
            updated_at="",
            started_at="",
            finished_at=None,
            assigned_worker="node-a",
            assigned_gpus=[0],
            tmux_session=None,
            exit_code=None,
            message="",
            max_memory_used_mb=None,
            min_free_memory_mb=None,
            max_utilization_percent=None,
            min_free_seconds=0,
            preemptible=True,
            allow_preempt=False,
            background=False,
            elastic=False,
            log_mode="pty",
            callback_command=None,
            callback_events=[],
        )
        remote = CapturingRemote()

        remote.kill_task(worker, task)

        self.assertEqual(remote.killed, ["gw-123456781234"])


if __name__ == "__main__":
    unittest.main()
