# Public deployment

Keep gpu-watcher bound to the loopback interface and publish it through an HTTPS reverse proxy. Do not expose port `8765` directly to the Internet.

## 1. Configure authentication

Use this daemon configuration:

```toml
[api]
enabled = true
host = "127.0.0.1"
port = 8765

[auth]
enabled = true
username = "admin"
secure_cookie = true
session_ttl_seconds = 43200
login_max_attempts = 5
login_window_seconds = 300
```

Create `~/.config/gpu-watcher/auth.env` and restrict it to the daemon user:

```bash
mkdir -p ~/.config/gpu-watcher
gpu-watcher hash-password
openssl rand -base64 48
chmod 600 ~/.config/gpu-watcher/auth.env
```

Place the generated values in the environment file using the names shown in `deploy/auth.env.example`. No public API or CLI credential is configured; Internet-facing access is through the signed Web session only.

## 2. Run the user service

Install `deploy/gpu-watcher.service.template` as `~/.config/systemd/user/gpu-watcher.service`, adjust its paths, then run:

```bash
systemctl --user daemon-reload
systemctl --user enable --now gpu-watcher.service
loginctl enable-linger "$USER"
```

The final command lets the user service run when the account has no interactive login session. It may require an administrator depending on host policy.

## 3. Terminate TLS at a reverse proxy

Caddy automatically supports WebSocket upgrades:

```caddyfile
gpu.example.com {
    reverse_proxy 127.0.0.1:8765 {
        header_up -Authorization
        header_up -X-GPU-Watcher-Local-Client
    }
    header Strict-Transport-Security "max-age=31536000; includeSubDomains"
}
```

Equivalent Nginx location settings are:

```nginx
location / {
    proxy_pass http://127.0.0.1:8765;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header Authorization "";
    proxy_set_header X-GPU-Watcher-Local-Client "";
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
}
```

Configure the Nginx virtual host with a valid certificate, an HTTP-to-HTTPS redirect, and an HSTS header. Restrict firewall access to the HTTPS proxy port; the daemon port should remain loopback-only.

## 4. Keep CLI access local

The public endpoint accepts Web sessions, not Bearer tokens or CLI credentials. Run CLI commands directly on the control node:

```bash
gpu-watcher status
gpu-watcher logs <task-id>
```

The daemon recognizes its CLI only on a direct loopback connection. The proxy must set `X-Forwarded-For` and strip `Authorization` and `X-GPU-Watcher-Local-Client` as shown above. Rotate the session secret to invalidate all browser sessions and restart the service after changing it.
