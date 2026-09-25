from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gpu_watcher.auth import hash_password
from gpu_watcher.config import ConfigError, load_config, update_worker_config_file
from gpu_watcher.models import WorkerConfig


class ConfigTests(unittest.TestCase):
    def test_loads_im_notify_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(
                """
[daemon]
db_path = "./test.db"

[im_notify]
enabled = true
api = "http://onebot.local"
group = 123
token = "secret"
timeout_seconds = 7

[[workers]]
name = "node-a"
host = "node-a"
""",
                encoding="utf-8",
            )

            config = load_config(path)

            self.assertTrue(config.im_notify.enabled)
            self.assertEqual(config.im_notify.api, "http://onebot.local")
            self.assertEqual(config.im_notify.group, 123)
            self.assertEqual(config.im_notify.token, "secret")
            self.assertEqual(config.im_notify.timeout_seconds, 7)

    def test_loads_auth_secrets_from_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(
                """
[auth]
enabled = true
username = "operator"
secure_cookie = true

[[workers]]
name = "node-a"
host = "node-a"
""",
                encoding="utf-8",
            )
            environment = {
                "GPU_WATCHER_PASSWORD_HASH": hash_password("correct horse battery staple"),
                "GPU_WATCHER_SESSION_SECRET": "s" * 48,
            }

            with patch.dict("os.environ", environment, clear=False):
                config = load_config(path)

            self.assertTrue(config.auth.enabled)
            self.assertEqual(config.auth.username, "operator")
            self.assertTrue(config.auth.secure_cookie)

    def test_rejects_public_bind_without_authentication(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(
                """
[api]
host = "0.0.0.0"

[[workers]]
name = "node-a"
host = "node-a"
""",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ConfigError, "authentication must be enabled"):
                load_config(path)

    def test_worker_update_preserves_other_sections_and_comments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(
                """# keep this comment
[daemon]
db_path = "./test.db"

[[workers]]
name = "node-a"
host = "old-host" # worker comment
port = 22
gpus = [0]
""",
                encoding="utf-8",
            )

            update_worker_config_file(
                path,
                "node-a",
                WorkerConfig(
                    name="node-a",
                    host="new-host",
                    user="trainer",
                    port=2200,
                    enabled=False,
                    tmux_prefix="jobs",
                    gpus=[1, 2],
                    max_concurrent_tasks=2,
                    max_background_tasks=1,
                ),
            )

            updated = path.read_text(encoding="utf-8")
            self.assertIn("# keep this comment", updated)
            self.assertIn("# worker comment", updated)
            self.assertIn('db_path = "./test.db"', updated)
            self.assertIn('host = "new-host"', updated)


if __name__ == "__main__":
    unittest.main()
