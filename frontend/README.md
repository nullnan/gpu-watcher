# GPU Watcher web interface

The dashboard and login page use Vue 3, Tailwind CSS 4, Lucide icons, and locally hosted Inter / JetBrains Mono variable fonts. The aiohttp server serves the compiled assets; Node.js is only needed when changing the frontend.

## Build

Use Node.js 22 or later:

```sh
cd frontend
npm ci
npm run build
```

The build writes `app.js`, `login.js`, `styles.css`, and shared dependencies into `gpu_watcher/web/`. Include these generated assets when distributing the Python package. Existing xterm assets and HTML entrypoints are preserved. No CDN or external font requests are made at runtime.

For local development, run the existing API server and use `npm run dev` to rebuild on source changes, then refresh its web page. This is a watch build, not a separate development API.

## Structure

- `src/App.vue`: reactive navigation, queue summary, task filtering and pagination, GPU and worker views.
- `src/Login.vue`: sign-in state, password visibility and error feedback.
- `src/SubmitForm.vue` and `src/Dialogs.vue`: stable form and terminal containers.
- `src/controller.js`: API / CSRF requests, task lifecycle actions, editing, and xterm WebSocket sessions. Vue owns the main views; this adapter owns the contents of the `v-once` form/dialog components so polling never replaces active inputs or terminal instances.
- `src/style.css`: Tailwind entrypoint and the responsive visual system; `legacy.css` retains detail, editor, and terminal layout rules used by the adapter.

Small screens use task cards and a top navigation bar. The interface includes light/dark/system themes, keyboard focus containment for dialogs, connection failures with retry, and reduced-motion support. GPU figures are latest samples, not historical charts.

## Verification

```sh
npm run build
cd ..
.venv/bin/python -m pytest -q
```

Resource tests check built module dependencies, font packaging, and aiohttp MIME types. When changing interactions, also check search, filters, pagination, task creation/editing, worker editing, logs/NVTop, sign-in failures, and 320/390/768px layouts in a browser against an isolated test server. Do not use real GPU jobs as UI test fixtures.
