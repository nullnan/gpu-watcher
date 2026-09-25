from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gpu_watcher.im_notify import send_task_im_notification
from gpu_watcher.models import ImNotifyConfig
from gpu_watcher.store import Store


class FakeResponse:
    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args) -> None:
        return None

    def read(self) -> bytes:
        return b'{"status":"ok"}'


class ImNotifyTests(unittest.TestCase):
    def test_send_group_notification_uses_onebot_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "test.db")
            store.init()
            task = store.create_task(name="notify", command="echo notify", im_notify=True)
            config = ImNotifyConfig(enabled=True, api="http://onebot.local", token="secret", group=123)

            with patch("gpu_watcher.im_notify.urlopen", return_value=FakeResponse()) as mocked:
                result = send_task_im_notification(config, task, "succeeded", "done")

            request = mocked.call_args.args[0]
            payload = json.loads(request.data.decode("utf-8"))
            self.assertEqual(request.full_url, "http://onebot.local/send_group_msg")
            self.assertEqual(payload["group_id"], 123)
            self.assertIn("gpu-watcher succeeded: notify", payload["message"])
            self.assertIn(f"id={task.id}", payload["message"])
            self.assertEqual(request.get_header("Authorization"), "Bearer secret")
            self.assertEqual(result.target_type, "group")
            self.assertEqual(result.target_id, 123)


if __name__ == "__main__":
    unittest.main()
