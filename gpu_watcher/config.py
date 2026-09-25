from __future__ import annotations

import os
import re
import stat
import tempfile
import tomllib
from pathlib import Path
from typing import Any

import tomlkit

from .models import AuthConfig, ApiConfig, AppConfig, DaemonConfig, ImNotifyConfig, WorkerConfig


class ConfigError(ValueError):
    pass


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path).expanduser()
    if not config_path.exists():
        raise ConfigError(f"config file does not exist: {config_path}")

    with config_path.open("rb") as fh:
        raw = tomllib.load(fh)

    daemon_raw = raw.get("daemon", {})
    api_raw = raw.get("api", {})
    workers_raw = raw.get("workers", [])

    if not workers_raw:
        raise ConfigError("at least one [[workers]] entry is required")

    daemon = DaemonConfig(
        db_path=str(Path(daemon_raw.get("db_path", "./gpu-watcher.db")).expanduser()),
        poll_interval_seconds=float(daemon_raw.get("poll_interval_seconds", 10)),
        remote_base_dir=str(daemon_raw.get("remote_base_dir", "~/.gpu-watcher")),
        free_memory_mb=int(daemon_raw.get("free_memory_mb", 512)),
        free_utilization_percent=int(daemon_raw.get("free_utilization_percent", 10)),
    )

    api = ApiConfig(
        enabled=bool(api_raw.get("enabled", True)),
        host=str(api_raw.get("host", "127.0.0.1")),
        port=int(api_raw.get("port", 8765)),
    )
    auth = _parse_auth(raw.get("auth", {}))
    if api.enabled and api.host not in {"127.0.0.1", "::1", "localhost"} and not auth.enabled:
        raise ConfigError("authentication must be enabled when the API binds to a non-loopback host")
    im_notify = _parse_im_notify(raw.get("im_notify", {}))

    workers = [parse_worker_config(item) for item in workers_raw]
    names = [worker.name for worker in workers]
    duplicates = {name for name in names if names.count(name) > 1}
    if duplicates:
        raise ConfigError(f"duplicate worker names: {', '.join(sorted(duplicates))}")

    return AppConfig(
        daemon=daemon,
        api=api,
        workers=workers,
        im_notify=im_notify,
        auth=auth,
        config_path=str(config_path.resolve()),
    )


def _parse_auth(raw: dict[str, Any]) -> AuthConfig:
    enabled = bool(raw.get("enabled", False))
    username = str(raw.get("username", "admin")).strip()
    password_hash = _secret_from_env(raw, "password_hash_env", "GPU_WATCHER_PASSWORD_HASH")
    session_secret = _secret_from_env(raw, "session_secret_env", "GPU_WATCHER_SESSION_SECRET")
    session_ttl_seconds = int(raw.get("session_ttl_seconds", 12 * 60 * 60))
    login_max_attempts = int(raw.get("login_max_attempts", 5))
    login_window_seconds = int(raw.get("login_window_seconds", 5 * 60))
    cookie_name = str(raw.get("cookie_name", "gpu_watcher_session")).strip()

    if enabled:
        if not username:
            raise ConfigError("auth username cannot be empty")
        if not password_hash:
            raise ConfigError("auth password hash environment variable is not set")
        if not session_secret or len(session_secret) < 32:
            raise ConfigError("auth session secret must contain at least 32 characters")
    if session_ttl_seconds < 60:
        raise ConfigError("auth session_ttl_seconds must be at least 60")
    if login_max_attempts < 1:
        raise ConfigError("auth login_max_attempts must be at least 1")
    if login_window_seconds < 1:
        raise ConfigError("auth login_window_seconds must be at least 1")
    if not cookie_name or any(char in cookie_name for char in " ;,=\t\r\n"):
        raise ConfigError("auth cookie_name is invalid")

    return AuthConfig(
        enabled=enabled,
        username=username,
        password_hash=password_hash,
        session_secret=session_secret,
        secure_cookie=bool(raw.get("secure_cookie", False)),
        session_ttl_seconds=session_ttl_seconds,
        login_max_attempts=login_max_attempts,
        login_window_seconds=login_window_seconds,
        cookie_name=cookie_name,
    )


def _secret_from_env(raw: dict[str, Any], key: str, default_name: str) -> str | None:
    env_name = str(raw.get(key, default_name)).strip()
    if not env_name:
        return None
    return _optional_str(os.environ.get(env_name))


def _parse_im_notify(raw: dict[str, Any]) -> ImNotifyConfig:
    enabled = bool(raw.get("enabled", False))
    user = _optional_int(raw.get("user", raw.get("user_id")))
    group = _optional_int(raw.get("group", raw.get("group_id")))
    if enabled and user is None and group is None:
        raise ConfigError("im_notify requires user/user_id or group/group_id when enabled")
    timeout_seconds = float(raw.get("timeout_seconds", 5))
    if timeout_seconds <= 0:
        raise ConfigError("im_notify timeout_seconds must be positive")
    return ImNotifyConfig(
        enabled=enabled,
        api=str(raw.get("api", "http://127.0.0.1:3000")).rstrip("/"),
        token=_optional_str(raw.get("token")),
        user=user,
        group=group,
        timeout_seconds=timeout_seconds,
    )


def parse_worker_config(raw: dict[str, Any]) -> WorkerConfig:
    for field in ("name", "host"):
        if not raw.get(field):
            raise ConfigError(f"worker is missing required field: {field}")

    name = str(raw["name"]).strip()
    host = str(raw["host"]).strip()
    user = _optional_str(raw.get("user"))
    port = int(raw.get("port", 22))
    tmux_prefix = str(raw.get("tmux_prefix", "gw")).strip()
    gpus = [int(item) for item in raw.get("gpus", [])]
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
        raise ConfigError("worker name may contain only letters, digits, dot, underscore, and hyphen")
    if any(char.isspace() for char in host):
        raise ConfigError("worker host cannot contain whitespace")
    if user and any(char.isspace() for char in user):
        raise ConfigError("worker user cannot contain whitespace")
    if not 1 <= port <= 65535:
        raise ConfigError("worker port must be between 1 and 65535")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", tmux_prefix):
        raise ConfigError("worker tmux_prefix may contain only letters, digits, dot, underscore, and hyphen")
    if any(gpu < 0 for gpu in gpus):
        raise ConfigError("worker GPU indexes cannot be negative")
    if len(set(gpus)) != len(gpus):
        raise ConfigError("worker GPU indexes cannot contain duplicates")

    return WorkerConfig(
        name=name,
        host=host,
        user=user,
        port=port,
        ssh_key=_optional_str(raw.get("ssh_key")),
        enabled=bool(raw.get("enabled", True)),
        tmux_prefix=tmux_prefix,
        gpus=gpus,
        max_concurrent_tasks=_optional_positive_int(raw.get("max_concurrent_tasks"), "max_concurrent_tasks"),
        max_background_tasks=_optional_nonnegative_int(raw.get("max_background_tasks", 1), "max_background_tasks"),
    )


def update_worker_config_file(
    path: str | Path,
    current_name: str,
    worker: WorkerConfig,
) -> None:
    config_path = Path(path).expanduser()
    document = tomlkit.parse(config_path.read_text(encoding="utf-8"))
    workers = document.get("workers")
    if workers is None:
        raise ConfigError("config file has no [[workers]] entries")

    target = next((item for item in workers if str(item.get("name")) == current_name), None)
    if target is None:
        raise ConfigError(f"worker not found in config file: {current_name}")
    if worker.name != current_name:
        raise ConfigError("worker name cannot be changed")

    target["host"] = worker.host
    _set_optional_toml_value(target, "user", worker.user)
    target["port"] = worker.port
    _set_optional_toml_value(target, "ssh_key", worker.ssh_key)
    target["enabled"] = worker.enabled
    target["tmux_prefix"] = worker.tmux_prefix
    target["gpus"] = worker.gpus
    _set_optional_toml_value(target, "max_concurrent_tasks", worker.max_concurrent_tasks)
    target["max_background_tasks"] = worker.max_background_tasks

    mode = stat.S_IMODE(config_path.stat().st_mode)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{config_path.name}.", dir=config_path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(tomlkit.dumps(document))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, mode)
        os.replace(temporary_name, config_path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def _set_optional_toml_value(table: Any, key: str, value: Any) -> None:
    if value is None:
        if key in table:
            del table[key]
        return
    table[key] = value


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


def _optional_positive_int(value: Any, field: str) -> int | None:
    if value is None:
        return None
    number = int(value)
    if number < 1:
        raise ConfigError(f"worker {field} must be at least 1")
    return number


def _optional_nonnegative_int(value: Any, field: str) -> int:
    number = int(value)
    if number < 0:
        raise ConfigError(f"worker {field} cannot be negative")
    return number
