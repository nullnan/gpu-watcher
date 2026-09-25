from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from .callbacks import task_event_message
from .models import ImNotifyConfig, Task


class ImNotifyError(RuntimeError):
    pass


@dataclass(frozen=True)
class ImNotifyResult:
    event: str
    target_type: str
    target_id: int
    response: dict[str, object]


def send_task_im_notification(config: ImNotifyConfig, task: Task, event: str, detail: str | None = None) -> ImNotifyResult:
    target_type, target_id, path = _target(config)
    title, content = task_event_message(task, event, detail)
    payload = {
        f"{target_type}_id": target_id,
        "message": f"【{title}】\n{content}",
    }
    response = _post_json(
        urljoin(f"{config.api.rstrip('/')}/", path),
        payload,
        token=config.token,
        timeout_seconds=config.timeout_seconds,
    )
    if response.get("status") != "ok":
        raise ImNotifyError(str(response))
    return ImNotifyResult(
        event=event,
        target_type=target_type,
        target_id=target_id,
        response=response,
    )


def _target(config: ImNotifyConfig) -> tuple[str, int, str]:
    if not config.enabled:
        raise ImNotifyError("im_notify is disabled")
    if config.user is not None:
        return "user", int(config.user), "send_private_msg"
    if config.group is not None:
        return "group", int(config.group), "send_group_msg"
    raise ImNotifyError("im_notify target is not configured")


def _post_json(url: str, payload: dict[str, object], *, token: str | None, timeout_seconds: float) -> dict[str, object]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "content-type": "application/json",
        "accept": "application/json",
    }
    if token:
        headers["authorization"] = f"Bearer {token}"
    request = Request(url, data=body, method="POST", headers=headers)
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            text = response.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise ImNotifyError(f"HTTP {exc.code}: {detail or exc.reason}") from exc
    except URLError as exc:
        raise ImNotifyError(f"request failed: {exc.reason}") from exc
    except TimeoutError as exc:
        raise ImNotifyError("request timed out") from exc
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ImNotifyError(f"invalid json response: {text[:200]}") from exc
    if not isinstance(decoded, dict):
        raise ImNotifyError(f"unexpected response: {decoded!r}")
    return decoded
