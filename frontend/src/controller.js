import { shallowReactive, nextTick } from "vue";

export const state = shallowReactive({
  loading: false,
  loaded: false,
  error: "",
  online: false,
  updatedAt: "",
  authEnabled: false,
  tasks: [],
  runningTasks: [],
  gpus: [],
  workers: [],
  filter: "all",
  taskSearch: "",
  taskPageSize: 20,
  taskPage: 1,
  taskTotal: 0,
  taskSummary: {},
  activeView: "tasks",
  activeGpuWorker: "all",
  activeLogTaskId: null,
  activeDetailsTaskId: null,
  activeWorkerName: null,
  activeGpuDevice: null,
  activeNvtopWorker: null,
  taskDetailsEditing: false,
  timer: null,
  logSocket: null,
  logTerminal: null,
  logFitAddon: null,
  logFollow: true,
  logSelectionActive: false,
  logReconnectTimer: null,
  logReconnectAttempt: 0,
  logStreamEstablished: false,
  nvtopSocket: null,
  nvtopTerminal: null,
  nvtopFitAddon: null,
  nvtopDisposables: [],
  csrfToken: "",
});

const terminalStatuses = new Set(["succeeded", "failed", "canceled"]);
const controllableStatuses = new Set(["paused", "queued", "running"]);

let elements;
export function initialize() {
  elements = {
    notice: document.querySelector("#notice"),
    taskForm: document.querySelector("#taskForm"),
    targetWorker: document.querySelector("#targetWorker"),
    submitStatus: document.querySelector("#submitStatus"),
    logModal: document.querySelector("#logModal"),
    logMeta: document.querySelector("#logMeta"),
    logTailKb: document.querySelector("#logTailKb"),
    logTerminal: document.querySelector("#logTerminal"),
    logLiveStatus: document.querySelector("#logLiveStatus"),
    followLogsButton: document.querySelector("#followLogsButton"),
    endLogsButton: document.querySelector("#endLogsButton"),
    reconnectLogsButton: document.querySelector("#reconnectLogsButton"),
    closeLogsButton: document.querySelector("#closeLogsButton"),
    nvtopModal: document.querySelector("#nvtopModal"),
    nvtopMeta: document.querySelector("#nvtopMeta"),
    nvtopTerminal: document.querySelector("#nvtopTerminal"),
    nvtopLiveStatus: document.querySelector("#nvtopLiveStatus"),
    reconnectNvtopButton: document.querySelector("#reconnectNvtopButton"),
    closeNvtopButton: document.querySelector("#closeNvtopButton"),
    taskDetailsModal: document.querySelector("#taskDetailsModal"),
    taskDetailsMeta: document.querySelector("#taskDetailsMeta"),
    taskDetailsBody: document.querySelector("#taskDetailsBody"),
    detailsEditButton: document.querySelector("#detailsEditButton"),
    detailsLogsButton: document.querySelector("#detailsLogsButton"),
    closeTaskDetailsButton: document.querySelector("#closeTaskDetailsButton"),
    workerEditModal: document.querySelector("#workerEditModal"),
    workerEditForm: document.querySelector("#workerEditForm"),
    workerEditMeta: document.querySelector("#workerEditMeta"),
    workerEditStatus: document.querySelector("#workerEditStatus"),
    closeWorkerEditButton: document.querySelector("#closeWorkerEditButton"),
    cancelWorkerEditButton: document.querySelector("#cancelWorkerEditButton"),
    gpuDetailsModal: document.querySelector("#gpuDetailsModal"),
    gpuDetailsMeta: document.querySelector("#gpuDetailsMeta"),
    gpuDetailsBody: document.querySelector("#gpuDetailsBody"),
    closeGpuDetailsButton: document.querySelector("#closeGpuDetailsButton"),
  };

  elements.taskForm.addEventListener("submit", submitTask);
  elements.followLogsButton.addEventListener("click", () => followLogs());
  elements.endLogsButton.addEventListener("click", () => scrollLogsToEnd());
  elements.reconnectLogsButton.addEventListener("click", () =>
    reconnectActiveLogs(),
  );
  elements.closeLogsButton.addEventListener("click", () => closeLogs());
  elements.reconnectNvtopButton.addEventListener("click", () =>
    reconnectNvtop(),
  );
  elements.closeNvtopButton.addEventListener("click", () => closeNvtop());
  elements.detailsEditButton.addEventListener("click", () => beginTaskEdit());
  elements.detailsLogsButton.addEventListener("click", () => {
    if (!state.activeDetailsTaskId) return;
    const taskId = state.activeDetailsTaskId;
    closeTaskDetails();
    openLogs(taskId);
  });
  document
    .querySelector("#detailsStartButton")
    .addEventListener("click", () => {
      if (state.activeDetailsTaskId) startTask(state.activeDetailsTaskId);
    });
  document
    .querySelector("#detailsPauseButton")
    .addEventListener("click", () => {
      if (state.activeDetailsTaskId) pauseTask(state.activeDetailsTaskId);
    });
  document
    .querySelector("#detailsRerunButton")
    .addEventListener("click", () => {
      if (state.activeDetailsTaskId) rerunTask(state.activeDetailsTaskId);
    });
  document
    .querySelector("#detailsTerminateButton")
    .addEventListener("click", () => {
      if (state.activeDetailsTaskId) terminateTask(state.activeDetailsTaskId);
    });
  elements.closeTaskDetailsButton.addEventListener("click", () =>
    closeTaskDetails(),
  );
  elements.workerEditForm.addEventListener("submit", saveWorkerEdit);
  elements.closeWorkerEditButton.addEventListener("click", () =>
    closeWorkerEdit(),
  );
  elements.cancelWorkerEditButton.addEventListener("click", () =>
    closeWorkerEdit(),
  );
  elements.closeGpuDetailsButton.addEventListener("click", () =>
    closeGpuDetails(),
  );
  elements.logModal.addEventListener("click", (event) => {
    if (event.target.matches("[data-close-logs]")) closeLogs();
  });
  elements.nvtopModal.addEventListener("click", (event) => {
    if (event.target.matches("[data-close-nvtop]")) closeNvtop();
  });
  elements.taskDetailsModal.addEventListener("click", (event) => {
    if (event.target.matches("[data-close-task-details]")) closeTaskDetails();
  });
  elements.workerEditModal.addEventListener("click", (event) => {
    if (event.target.matches("[data-close-worker-edit]")) closeWorkerEdit();
  });
  elements.gpuDetailsModal.addEventListener("click", (event) => {
    if (event.target.matches("[data-close-gpu-details]")) closeGpuDetails();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && state.activeNvtopWorker) closeNvtop();
    else if (event.key === "Escape" && state.activeLogTaskId) closeLogs();
    if (event.key === "Escape" && state.activeDetailsTaskId) {
      if (state.taskDetailsEditing) cancelTaskEdit();
      else closeTaskDetails();
    }
    if (event.key === "Escape" && state.activeWorkerName) closeWorkerEdit();
    if (event.key === "Escape" && state.activeGpuDevice) closeGpuDetails();
  });
  window.addEventListener("resize", () => {
    fitTerminal();
    fitNvtopTerminal();
  });
  window.addEventListener("hashchange", () =>
    setActiveView(viewFromHash(), false),
  );

  setActiveView(viewFromHash() || state.activeView);
  setAutoRefresh(true);
  refresh();
}

function setAutoRefresh(enabled) {
  if (state.timer) {
    clearInterval(state.timer);
    state.timer = null;
  }
  if (enabled) {
    state.timer = setInterval(refresh, 5000);
  }
}

let refreshSequence = 0;
async function refresh() {
  const sequence = ++refreshSequence;
  state.loading = true;
  try {
    const [tasks, runningTasks, gpus, workers, health, session] =
      await Promise.all([
        getJson(tasksPath()),
        getJson("/tasks?status=running&limit=200&offset=0"),
        getJson("/gpus"),
        getJson("/workers"),
        getJson("/health"),
        getJson("/auth/session"),
      ]);
    if (sequence !== refreshSequence) return;
    state.csrfToken = session.csrf_token || "";
    state.authEnabled = Boolean(session.auth_enabled);
    const maxPageBeforeRender = Math.max(
      1,
      Math.ceil(Number(tasks.total || 0) / state.taskPageSize),
    );
    if (state.taskPage > maxPageBeforeRender) {
      state.taskPage = maxPageBeforeRender;
      await refresh();
      return;
    }
    state.tasks = tasks.tasks || [];
    state.runningTasks = runningTasks.tasks || [];
    state.taskTotal = Number(tasks.total || 0);
    state.taskSummary = tasks.summary || {};
    state.gpus = gpus.gpus || [];
    state.workers = workers.workers || [];
    reconcileActiveGpuWorker();
    renderWorkerOptions();
    state.online = Boolean(health.ok);
    state.updatedAt = new Date().toLocaleTimeString();
    state.loaded = true;
    state.error = health.ok ? "" : "The API is reporting degraded health.";
    render();
    if (state.activeDetailsTaskId && !state.taskDetailsEditing) {
      renderTaskDetails(state.activeDetailsTaskId);
    }
  } catch (error) {
    if (sequence !== refreshSequence) return;
    state.online = false;
    state.error = error.message;
  } finally {
    if (sequence === refreshSequence) state.loading = false;
  }
}

async function submitTask(event) {
  event.preventDefault();
  const button = elements.taskForm.querySelector('button[type="submit"]');
  if (button.disabled) return;
  elements.submitStatus.textContent = "Submitting";
  const payload = taskPayloadFromForm(elements.taskForm, true);

  if (!payload.command) {
    elements.submitStatus.textContent = "Command is required";
    return;
  }

  button.disabled = true;
  try {
    await postJson("/tasks", payload);
    elements.submitStatus.textContent = "Submitted";
    elements.taskForm.reset();
    elements.taskForm.elements.name.value = "task";
    elements.taskForm.elements.priority.value = "0";
    elements.taskForm.elements.requested_gpus.value = "1";
    elements.taskForm.elements.target_worker.value = "auto";
    elements.taskForm.elements.min_free_seconds.value = "0";
    elements.taskForm.elements.log_mode.value = "pty";
    await refresh();
    setActiveView("tasks");
  } catch (error) {
    elements.submitStatus.textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

async function startTask(taskId) {
  await runTaskAction("Start", `/tasks/${taskId}/start`);
}

async function pauseTask(taskId) {
  await runTaskAction("Pause", `/tasks/${taskId}/pause`);
}

async function rerunTask(taskId) {
  await runTaskAction("Rerun", `/tasks/${taskId}/rerun`);
}

async function terminateTask(taskId) {
  await runTaskAction("Terminate", `/tasks/${taskId}/terminate`);
}

async function runTaskAction(label, path) {
  try {
    await postJson(path, {});
    showNotice(`${label} completed`, "success");
    await refresh();
  } catch (error) {
    showNotice(`${label} failed: ${error.message}`, "error");
  }
}

function render() {
  renderGpuDetails();
}

function setActiveView(view, updateHash = true) {
  state.activeView = ["tasks", "infrastructure", "submit"].includes(view)
    ? view
    : "tasks";
  const nextHash = `#tab:${state.activeView}`;
  if (updateHash && window.location.hash !== nextHash)
    history.replaceState(null, "", nextHash);
  window.scrollTo({ top: 0, behavior: "instant" });
}

function viewFromHash() {
  return window.location.hash.replace(/^#(?:tab:)?/, "");
}

function openTaskDetails(taskId) {
  state.activeDetailsTaskId = taskId;
  state.taskDetailsEditing = false;
  elements.taskDetailsModal.classList.remove("hidden");
  document.body.classList.add("modal-open");
  renderTaskDetails(taskId);
}

function beginTaskEdit() {
  const task = state.tasks.find(
    (item) => item.id === state.activeDetailsTaskId,
  );
  if (!task || !taskIsEditable(task)) return;
  state.taskDetailsEditing = true;
  renderTaskDetailsControls(task);
  renderTaskEditForm(task);
}

function cancelTaskEdit() {
  state.taskDetailsEditing = false;
  if (state.activeDetailsTaskId) renderTaskDetails(state.activeDetailsTaskId);
}

function renderTaskEditForm(task) {
  elements.taskDetailsMeta.textContent = `${shortId(task.id)} · ${task.name} · ${task.status}`;
  elements.taskDetailsBody.innerHTML = `
    <form id="taskEditForm" class="task-edit-form">
      <label>
        <span>Name</span>
        <input name="name" value="${escapeHtml(task.name || "task")}" autocomplete="off" />
      </label>
      <label>
        <span>Priority</span>
        <input name="priority" type="number" value="${escapeHtml(task.priority ?? 0)}" />
      </label>
      <label>
        <span>GPUs</span>
        <input name="requested_gpus" type="number" min="1" value="${escapeHtml(task.requested_gpus ?? 1)}" />
      </label>
      <label>
        <span>Target worker</span>
        <select name="target_worker">${workerOptionsHtml(task.target_worker || "auto")}</select>
      </label>
      <label>
        <span>Max memory used MB</span>
        <input name="max_memory_used_mb" type="number" min="0" value="${escapeHtml(editValue(task.max_memory_used_mb))}" placeholder="default" />
      </label>
      <label>
        <span>Min free memory MB</span>
        <input name="min_free_memory_mb" type="number" min="0" value="${escapeHtml(editValue(task.min_free_memory_mb))}" placeholder="optional" />
      </label>
      <label>
        <span>Max utilization %</span>
        <input name="max_utilization_percent" type="number" min="0" max="100" value="${escapeHtml(editValue(task.max_utilization_percent))}" placeholder="default" />
      </label>
      <label>
        <span>Min free seconds</span>
        <input name="min_free_seconds" type="number" min="0" value="${escapeHtml(task.min_free_seconds ?? 0)}" />
      </label>
      <label>
        <span>Log mode</span>
        <select name="log_mode">
          <option value="pty" ${selectedAttr((task.log_mode || "pty") === "pty")}>PTY</option>
          <option value="pipe" ${selectedAttr(task.log_mode === "pipe")}>Pipe</option>
        </select>
      </label>
      <label>
        <span>Start cron</span>
        <input name="start_cron" value="${escapeHtml(task.start_cron || "")}" placeholder="0 9 * * 1-5" />
      </label>
      <label>
        <span>Stop cron</span>
        <input name="stop_cron" value="${escapeHtml(task.stop_cron || "")}" placeholder="0 18 * * 1-5" />
      </label>
      <label>
        <span>Schedule timezone</span>
        <input name="schedule_timezone" value="${escapeHtml(task.schedule_timezone || "UTC")}" placeholder="Asia/Shanghai" />
      </label>
      <label class="edit-wide">
        <span>Working directory</span>
        <input name="cwd" value="${escapeHtml(task.cwd || "")}" placeholder="/path/on/gpu/host" />
      </label>
      <label class="edit-wide">
        <span>Callback events</span>
        <input name="callback_events" value="${escapeHtml((task.callback_events || []).join(","))}" placeholder="succeeded,failed,switch" />
      </label>
      <label class="edit-wide">
        <span>Environment</span>
        <textarea name="env" rows="4" placeholder="KEY=value">${escapeHtml(envToText(task.env))}</textarea>
      </label>
      <label class="edit-wide">
        <span>Callback command</span>
        <textarea name="callback_command" rows="4" placeholder="/path/to/notify {title} {content}">${escapeHtml(task.callback_command || "")}</textarea>
      </label>
      <label class="edit-command">
        <span>Command</span>
        <textarea name="command" rows="6" required placeholder="python train.py">${escapeHtml(task.command || "")}</textarea>
      </label>
      <div class="edit-switch-row">
        <label class="toggle"><input name="preemptible" type="checkbox" ${checkedAttr(task.preemptible)} /><span>Preemptible</span></label>
        <label class="toggle"><input name="allow_preempt" type="checkbox" ${checkedAttr(task.allow_preempt)} /><span>Allow preempt</span></label>
        <label class="toggle"><input name="background" type="checkbox" ${checkedAttr(task.background)} /><span>Background</span></label>
        <label class="toggle"><input name="elastic" type="checkbox" ${checkedAttr(task.elastic)} /><span>Elastic</span></label>
        <label class="toggle"><input name="im_notify" type="checkbox" ${checkedAttr(task.im_notify)} /><span>IM notify</span></label>
      </div>
      <div class="edit-actions">
        <span id="taskEditStatus" role="status"></span>
        <button id="cancelTaskEditButton" type="button">Cancel</button>
        <button type="submit">Save</button>
      </div>
    </form>
  `;
  const form = document.querySelector("#taskEditForm");
  form.addEventListener("submit", saveTaskEdit);
  document
    .querySelector("#cancelTaskEditButton")
    .addEventListener("click", cancelTaskEdit);
}

async function saveTaskEdit(event) {
  event.preventDefault();
  const status = document.querySelector("#taskEditStatus");
  const payload = taskPayloadFromForm(event.currentTarget, false);
  if (!payload.command) {
    status.textContent = "Command is required";
    return;
  }
  status.textContent = "Saving";
  try {
    await putJson(`/tasks/${state.activeDetailsTaskId}`, payload);
    state.taskDetailsEditing = false;
    await refresh();
  } catch (error) {
    status.textContent = error.message;
  }
}

function renderTaskDetails(taskId) {
  const task = state.tasks.find((item) => item.id === taskId);
  if (!task) {
    elements.taskDetailsMeta.textContent = shortId(taskId);
    elements.taskDetailsBody.innerHTML = `<p class="empty">Task no longer appears in the latest list.</p>`;
    renderTaskDetailsControls(null);
    return;
  }
  elements.taskDetailsMeta.textContent = `${shortId(task.id)} · ${task.name} · ${task.status}`;
  renderTaskDetailsControls(task);
  elements.taskDetailsBody.innerHTML = `
    <section class="details-section">
      <h4>Placement</h4>
      <dl class="details-grid">
        ${detailItem("ID", task.id)}
        ${detailItem("Status", badgeHtml(task.status), { html: true })}
        ${detailItem("Name", task.name)}
        ${detailItem("Priority", task.priority)}
        ${detailItem("Requested GPUs", task.requested_gpus)}
        ${detailItem("Target worker", task.target_worker || "auto")}
        ${detailItem("Assigned worker", task.assigned_worker || "-")}
        ${detailItem("Assigned GPUs", (task.assigned_gpus || []).join(",") || "-")}
        ${detailItem("tmux session", task.tmux_session || "-")}
      </dl>
    </section>
    <section class="details-section">
      <h4>Scheduling</h4>
      <dl class="details-grid">
        ${detailItem("Background", yesNo(task.background))}
        ${detailItem("Elastic", yesNo(task.elastic))}
        ${detailItem("IM notify", yesNo(task.im_notify))}
        ${detailItem("Log mode", task.log_mode || "pty")}
        ${detailItem("Start cron", task.start_cron || "-")}
        ${detailItem("Stop cron", task.stop_cron || "-")}
        ${detailItem("Schedule timezone", task.schedule_timezone || "UTC")}
        ${detailItem("Callback events", (task.callback_events || []).join(",") || "-")}
        ${detailItem("Preemptible", yesNo(task.preemptible))}
        ${detailItem("Allow preempt", yesNo(task.allow_preempt))}
        ${detailItem("Max memory used MB", nullableValue(task.max_memory_used_mb))}
        ${detailItem("Min free memory MB", nullableValue(task.min_free_memory_mb))}
        ${detailItem("Max utilization %", nullableValue(task.max_utilization_percent))}
        ${detailItem("Min free seconds", task.min_free_seconds)}
      </dl>
    </section>
    <section class="details-section">
      <h4>Timing</h4>
      <dl class="details-grid">
        ${detailItem("Created", formatTime(task.created_at))}
        ${detailItem("Updated", formatTime(task.updated_at))}
        ${detailItem("Started", formatTime(task.started_at))}
        ${detailItem("Finished", formatTime(task.finished_at))}
        ${detailItem("Duration", formatDuration(task.duration_seconds))}
        ${detailItem("Exit code", nullableValue(task.exit_code))}
      </dl>
    </section>
    <section class="details-section">
      <h4>Execution</h4>
      <dl class="details-grid">
        ${detailItem("Working directory", task.cwd || "-")}
        ${detailItem("Message", task.message || "-")}
      </dl>
      <h5>Command</h5>
      <pre class="details-code">${escapeHtml(task.command || "")}</pre>
      <h5>Environment</h5>
      <pre class="details-code">${escapeHtml(JSON.stringify(task.env || {}, null, 2))}</pre>
      <h5>Callback command</h5>
      <pre class="details-code">${escapeHtml(task.callback_command || "")}</pre>
      <h5>Raw JSON</h5>
      <pre class="details-code">${escapeHtml(JSON.stringify(task, null, 2))}</pre>
    </section>
  `;
}

function renderTaskDetailsControls(task) {
  const editing = state.taskDetailsEditing;
  elements.detailsEditButton.hidden = editing || !task || !taskIsEditable(task);
  document.querySelector("#detailsStartButton").hidden =
    editing || !task || task.status !== "paused";
  document.querySelector("#detailsPauseButton").hidden =
    editing ||
    !task ||
    !task.preemptible ||
    (task.status !== "queued" && task.status !== "running");
  document.querySelector("#detailsRerunButton").hidden =
    editing || !task || task.status !== "failed";
  document.querySelector("#detailsTerminateButton").hidden =
    editing || !task || !controllableStatuses.has(task.status);
  elements.detailsLogsButton.hidden = editing;
}

function closeTaskDetails() {
  state.activeDetailsTaskId = null;
  state.taskDetailsEditing = false;
  elements.taskDetailsModal.classList.add("hidden");
  if (
    !state.activeLogTaskId &&
    !state.activeGpuDevice &&
    !state.activeWorkerName &&
    !state.activeNvtopWorker
  ) {
    document.body.classList.remove("modal-open");
  }
  elements.taskDetailsMeta.textContent = "";
  elements.taskDetailsBody.textContent = "";
  renderTaskDetailsControls(null);
}

async function openLogs(taskId) {
  state.activeLogTaskId = taskId;
  elements.logModal.classList.remove("hidden");
  document.body.classList.add("modal-open");
  setupLogTerminal();
  scheduleTerminalFit();
  connectActiveLogStream();
}

function setupLogTerminal() {
  stopLogStream();
  disposeLogTerminal();
  state.logStreamEstablished = false;
  elements.logTerminal.textContent = "";
  const terminal = new Terminal({
    allowProposedApi: true,
    convertEol: false,
    cursorBlink: false,
    fontFamily: "JetBrains Mono Variable, ui-monospace, monospace",
    fontSize: 12,
    lineHeight: 1.25,
    scrollback: 10000,
    theme: {
      background: "#101828",
      foreground: "#e6edf3",
      selectionBackground: "#475467",
    },
  });
  const fitAddon = new FitAddon.FitAddon();
  terminal.loadAddon(fitAddon);
  terminal.open(elements.logTerminal);
  state.logTerminal = terminal;
  state.logFitAddon = fitAddon;
  state.logFollow = true;
  state.logSelectionActive = false;
  fitTerminal();
  terminal.write("Connecting...\r\n");
  terminal.onSelectionChange(() => {
    state.logSelectionActive = terminal.hasSelection();
    if (state.logSelectionActive) {
      state.logFollow = false;
    }
    updateLogFollowStatus();
  });
  terminal.onScroll(() => {
    if (isTerminalAtBottom()) {
      state.logFollow = !terminal.hasSelection();
    } else {
      state.logFollow = false;
    }
    updateLogFollowStatus();
  });
  updateLogFollowStatus();
}

function scheduleTerminalFit() {
  requestAnimationFrame(() => {
    fitTerminal();
    setTimeout(() => fitTerminal(), 100);
  });
}

function fitTerminal() {
  try {
    state.logFitAddon?.fit();
  } catch (_error) {
    // The fit addon can throw while the modal is still being laid out.
  }
}

function disposeLogTerminal() {
  if (state.logTerminal) {
    state.logTerminal.dispose();
    state.logTerminal = null;
  }
  state.logFitAddon = null;
}

function connectActiveLogStream({ recovery = false } = {}) {
  if (!state.activeLogTaskId) return;
  const task = state.tasks.find((item) => item.id === state.activeLogTaskId);
  elements.logMeta.textContent = task
    ? `${shortId(task.id)} · ${task.name} · ${task.assigned_worker || "not assigned"}`
    : shortId(state.activeLogTaskId);

  stopLogStream();
  if (state.logReconnectTimer) {
    clearTimeout(state.logReconnectTimer);
    state.logReconnectTimer = null;
  }
  const tailBytes =
    Math.max(1, numberValue(elements.logTailKb.value, 256)) * 1024;
  const socket = new WebSocket(
    logStreamUrl(state.activeLogTaskId, recovery ? 0 : tailBytes),
  );
  socket.binaryType = "arraybuffer";
  state.logSocket = socket;
  elements.logLiveStatus.textContent = "Connecting";

  socket.addEventListener("open", () => {
    elements.logLiveStatus.textContent = `Live · ${new Date().toLocaleTimeString()}`;
    scheduleTerminalFit();
    if (!recovery) {
      state.logTerminal?.clear();
    }
    state.logStreamEstablished = true;
  });
  socket.addEventListener("message", (event) => {
    if (!state.logTerminal) return;
    const shouldFollow = state.logFollow && !state.logTerminal.hasSelection();
    const data =
      typeof event.data === "string" ? event.data : new Uint8Array(event.data);
    state.logTerminal.write(data, () => {
      if (shouldFollow) {
        state.logTerminal?.scrollToBottom();
      }
    });
  });
  socket.addEventListener("close", () => {
    if (state.logSocket === socket) {
      state.logSocket = null;
      if (state.activeLogTaskId) {
        scheduleLogReconnect();
      }
    }
  });
  socket.addEventListener("error", () => {
    elements.logLiveStatus.textContent = "Error";
  });
}

function reconnectActiveLogs() {
  if (!state.activeLogTaskId) return;
  if (state.logReconnectTimer) {
    clearTimeout(state.logReconnectTimer);
    state.logReconnectTimer = null;
  }
  state.logReconnectAttempt = 0;
  state.logStreamEstablished = false;
  state.logTerminal?.reset();
  state.logTerminal?.write("Reconnecting...\r\n");
  connectActiveLogStream({ recovery: false });
}

function scheduleLogReconnect() {
  if (!state.activeLogTaskId || state.logReconnectTimer) return;
  const delay = Math.min(
    15000,
    500 * 2 ** Math.min(state.logReconnectAttempt, 5),
  );
  state.logReconnectAttempt += 1;
  elements.logLiveStatus.textContent = `Reconnecting in ${Math.ceil(delay / 1000)}s`;
  state.logReconnectTimer = setTimeout(() => {
    state.logReconnectTimer = null;
    connectActiveLogStream({ recovery: state.logStreamEstablished });
  }, delay);
}

function followLogs() {
  state.logFollow = true;
  state.logSelectionActive = false;
  state.logTerminal?.clearSelection();
  scrollLogsToEnd();
  updateLogFollowStatus();
}

function scrollLogsToEnd() {
  state.logTerminal?.scrollToBottom();
}

function updateLogFollowStatus() {
  elements.followLogsButton.classList.toggle("active", state.logFollow);
  if (state.logFollow) {
    elements.followLogsButton.textContent = "Following";
  } else if (state.logSelectionActive) {
    elements.followLogsButton.textContent = "Selection";
  } else {
    elements.followLogsButton.textContent = "Follow";
  }
}

function isTerminalAtBottom() {
  if (!state.logTerminal) return true;
  const buffer = state.logTerminal.buffer.active;
  return buffer.viewportY >= buffer.baseY - 1;
}

function stopLogStream() {
  if (state.logSocket) {
    state.logSocket.close();
    state.logSocket = null;
  }
}

function closeLogs() {
  stopLogStream();
  if (state.logReconnectTimer) {
    clearTimeout(state.logReconnectTimer);
    state.logReconnectTimer = null;
  }
  state.logReconnectAttempt = 0;
  state.logStreamEstablished = false;
  state.activeLogTaskId = null;
  elements.logModal.classList.add("hidden");
  if (
    !state.activeDetailsTaskId &&
    !state.activeGpuDevice &&
    !state.activeWorkerName &&
    !state.activeNvtopWorker
  ) {
    document.body.classList.remove("modal-open");
  }
  elements.logMeta.textContent = "";
  elements.logLiveStatus.textContent = "Live";
  disposeLogTerminal();
  elements.logTerminal.textContent = "";
}

function openNvtop(workerName) {
  const worker = state.workers.find((item) => item.name === workerName);
  if (!worker) {
    showNotice("Worker is no longer available", "error");
    return;
  }
  state.activeNvtopWorker = worker.name;
  elements.nvtopMeta.textContent = `${worker.name} · ${worker.user ? `${worker.user}@` : ""}${worker.host}:${worker.port || 22}`;
  elements.nvtopModal.classList.remove("hidden");
  document.body.classList.add("modal-open");
  setupNvtopTerminal();
  scheduleNvtopFit();
  connectNvtop();
}

function setupNvtopTerminal() {
  stopNvtopStream();
  disposeNvtopTerminal();
  elements.nvtopTerminal.textContent = "";
  const terminal = new Terminal({
    allowProposedApi: true,
    convertEol: false,
    cursorBlink: true,
    fontFamily: "JetBrains Mono Variable, ui-monospace, monospace",
    fontSize: 12,
    lineHeight: 1.15,
    scrollback: 2000,
    theme: {
      background: "#101828",
      foreground: "#e6edf3",
      selectionBackground: "#475467",
    },
  });
  const fitAddon = new FitAddon.FitAddon();
  terminal.loadAddon(fitAddon);
  terminal.open(elements.nvtopTerminal);
  state.nvtopTerminal = terminal;
  state.nvtopFitAddon = fitAddon;
  state.nvtopDisposables = [
    terminal.onData((data) => sendNvtopControl({ type: "input", data })),
    terminal.onResize(({ cols, rows }) =>
      sendNvtopControl({ type: "resize", cols, rows }),
    ),
  ];
  fitNvtopTerminal();
  terminal.write("Connecting to NVTop...\r\n");
  terminal.focus();
}

function scheduleNvtopFit() {
  requestAnimationFrame(() => {
    fitNvtopTerminal();
    setTimeout(() => fitNvtopTerminal(), 100);
  });
}

function fitNvtopTerminal() {
  try {
    state.nvtopFitAddon?.fit();
  } catch (_error) {
    // The fit addon can throw while the modal is still being laid out.
  }
}

function connectNvtop() {
  if (!state.activeNvtopWorker || !state.nvtopTerminal) return;
  stopNvtopStream();
  fitNvtopTerminal();
  const { cols = 120, rows = 36 } = state.nvtopTerminal;
  const socket = new WebSocket(
    nvtopStreamUrl(state.activeNvtopWorker, cols, rows),
  );
  socket.binaryType = "arraybuffer";
  state.nvtopSocket = socket;
  elements.nvtopLiveStatus.textContent = "Connecting";

  socket.addEventListener("open", () => {
    if (state.nvtopSocket !== socket) return;
    elements.nvtopLiveStatus.textContent = `Live · ${new Date().toLocaleTimeString()}`;
    state.nvtopTerminal?.reset();
    scheduleNvtopFit();
    sendNvtopControl({
      type: "resize",
      cols: state.nvtopTerminal?.cols || cols,
      rows: state.nvtopTerminal?.rows || rows,
    });
    state.nvtopTerminal?.focus();
  });
  socket.addEventListener("message", (event) => {
    if (!state.nvtopTerminal) return;
    const data =
      typeof event.data === "string" ? event.data : new Uint8Array(event.data);
    state.nvtopTerminal.write(data);
  });
  socket.addEventListener("close", () => {
    if (state.nvtopSocket === socket) {
      state.nvtopSocket = null;
      if (state.activeNvtopWorker)
        elements.nvtopLiveStatus.textContent = "Disconnected";
    }
  });
  socket.addEventListener("error", () => {
    elements.nvtopLiveStatus.textContent = "Error";
  });
}

function sendNvtopControl(payload) {
  if (state.nvtopSocket?.readyState === WebSocket.OPEN) {
    state.nvtopSocket.send(JSON.stringify(payload));
  }
}

function reconnectNvtop() {
  if (!state.activeNvtopWorker) return;
  state.nvtopTerminal?.reset();
  state.nvtopTerminal?.write("Reconnecting to NVTop...\r\n");
  connectNvtop();
}

function stopNvtopStream() {
  if (state.nvtopSocket) {
    state.nvtopSocket.close();
    state.nvtopSocket = null;
  }
}

function disposeNvtopTerminal() {
  for (const disposable of state.nvtopDisposables) disposable.dispose();
  state.nvtopDisposables = [];
  if (state.nvtopTerminal) {
    state.nvtopTerminal.dispose();
    state.nvtopTerminal = null;
  }
  state.nvtopFitAddon = null;
}

function closeNvtop() {
  stopNvtopStream();
  state.activeNvtopWorker = null;
  elements.nvtopModal.classList.add("hidden");
  elements.nvtopMeta.textContent = "";
  elements.nvtopLiveStatus.textContent = "Connecting";
  disposeNvtopTerminal();
  elements.nvtopTerminal.textContent = "";
  if (
    !state.activeLogTaskId &&
    !state.activeDetailsTaskId &&
    !state.activeGpuDevice &&
    !state.activeWorkerName
  ) {
    document.body.classList.remove("modal-open");
  }
}

function renderWorkerOptions() {
  const current = elements.targetWorker.value || "auto";
  elements.targetWorker.innerHTML = `<option value="auto">Auto</option>`;
  for (const worker of state.workers) {
    const option = document.createElement("option");
    option.value = worker.name;
    option.textContent = worker.enabled
      ? worker.name
      : `${worker.name} (disabled)`;
    elements.targetWorker.appendChild(option);
  }
  elements.targetWorker.value = state.workers.some(
    (worker) => worker.name === current,
  )
    ? current
    : "auto";
}

function openGpuDetails(workerName, gpuIndex) {
  state.activeGpuDevice = { workerName, gpuIndex };
  elements.gpuDetailsModal.classList.remove("hidden");
  document.body.classList.add("modal-open");
  renderGpuDetails();
}

function closeGpuDetails() {
  state.activeGpuDevice = null;
  elements.gpuDetailsModal.classList.add("hidden");
  elements.gpuDetailsMeta.textContent = "";
  elements.gpuDetailsBody.textContent = "";
  if (
    !state.activeLogTaskId &&
    !state.activeDetailsTaskId &&
    !state.activeWorkerName &&
    !state.activeNvtopWorker
  ) {
    document.body.classList.remove("modal-open");
  }
}

function renderGpuDetails() {
  if (!state.activeGpuDevice) return;
  const { workerName, gpuIndex } = state.activeGpuDevice;
  const device = gpuDevices().find(
    (item) =>
      item.worker_name === workerName &&
      Number(item.gpu_index) === Number(gpuIndex),
  );
  elements.gpuDetailsMeta.textContent = `${workerName} · GPU ${gpuIndex}`;
  if (!device) {
    elements.gpuDetailsBody.innerHTML = `<p class="empty-state">GPU sample is unavailable</p>`;
    return;
  }

  const runningTask = runningTaskForDevice(
    device.worker_name,
    device.gpu_index,
  );
  const connection = workerConnectionState(device.worker_name);
  const status =
    connection === "offline"
      ? "offline"
      : runningTask
        ? allocatedTaskHtml(runningTask)
        : Number(device.is_free)
          ? "available"
          : "busy";
  const processes = Array.isArray(device.processes) ? device.processes : [];
  const processRows = processes.length
    ? processes
        .map(
          (process) => `
            <tr>
              <td data-label="PID" class="code">${escapeHtml(process.pid)}</td>
              <td data-label="User">${escapeHtml(process.username || "-")}</td>
              <td data-label="GPU Mem MB">${escapeHtml(process.memory_used_mb)}</td>
              <td data-label="Command" class="process-command"><code>${escapeHtml(process.cmdline || "-")}</code></td>
            </tr>
          `,
        )
        .join("")
    : `<tr><td class="empty" colspan="4">No active compute processes</td></tr>`;

  elements.gpuDetailsBody.innerHTML = `
    <section class="details-section">
      <h4>Current Status</h4>
      <dl class="details-grid">
        ${detailItem("Device", `${device.worker_name} · GPU ${device.gpu_index}`)}
        ${detailItem("Status", status, { html: Boolean(runningTask && connection !== "offline") })}
        ${detailItem("Worker connection", connection)}
        ${detailItem("Memory MB", connection === "offline" ? "N/A" : formatMemory(device))}
        ${detailItem("Utilization", connection === "offline" ? "N/A" : `${device.utilization_percent}%`)}
        ${detailItem("NUMA node", device.numa_node ?? "-")}
        ${detailItem("Users", connection === "offline" ? "N/A" : (device.process_users || []).join(", ") || "-")}
        ${detailItem("Free since", connection === "offline" ? "N/A" : formatTime(device.free_since))}
        ${detailItem("Last seen", formatTime(device.last_seen))}
      </dl>
    </section>
    <section class="details-section">
      <h4>Processes</h4>
      <div class="table-wrap process-table-wrap">
        <table class="responsive-table process-table">
          <thead><tr><th>PID</th><th>User</th><th>GPU Mem MB</th><th>Command</th></tr></thead>
          <tbody>${processRows}</tbody>
        </table>
      </div>
    </section>
  `;
  bindTaskJumpLinks(elements.gpuDetailsBody);
}

function workerJumpHtml(workerName) {
  return `<button class="worker-jump-link" type="button" data-worker-name="${escapeHtml(workerName)}" title="View in Infrastructure" aria-label="View worker ${escapeHtml(workerName)} in Infrastructure">${escapeHtml(workerName)}</button>`;
}

function bindWorkerJumpLinks(root) {
  root.querySelectorAll(".worker-jump-link").forEach((button) => {
    button.addEventListener("click", () =>
      jumpToInfrastructure(button.dataset.workerName),
    );
  });
}

function jumpToInfrastructure(workerName) {
  if (!workerName) return;
  state.activeGpuWorker = workerName;
  setActiveView("infrastructure");
}

function allocatedTaskHtml(task) {
  return `
    <div class="device-cell allocated-task">
      <strong>allocated</strong>
      <span>
        ${escapeHtml(shortId(task.id))}
        <button class="task-jump-link" type="button" data-task-id="${escapeHtml(task.id)}" title="View in Tasks" aria-label="View task ${escapeHtml(task.name)} in Tasks">${escapeHtml(task.name)}</button>
      </span>
    </div>
  `;
}

function bindTaskJumpLinks(root) {
  root.querySelectorAll(".task-jump-link").forEach((button) => {
    button.addEventListener("click", () => jumpToTask(button.dataset.taskId));
  });
}

async function jumpToTask(taskId) {
  if (!taskId) return;
  if (state.activeGpuDevice) closeGpuDetails();

  state.filter = "all";
  state.taskSearch = taskId;
  state.taskPage = 1;

  setActiveView("tasks");
  await refresh();

  await nextTick();
  const row = Array.from(document.querySelectorAll("#tasksBody tr")).find(
    (item) => item.dataset.taskId === taskId,
  );
  if (!row) {
    showNotice("Task is no longer available", "error");
    return;
  }
  row.classList.remove("task-jump-highlight");
  void row.offsetWidth;
  row.classList.add("task-jump-highlight");
  row.scrollIntoView({ behavior: "smooth", block: "center" });
  setTimeout(() => row.classList.remove("task-jump-highlight"), 2200);
}

function workerConnectionState(workerName) {
  const worker = state.workers.find((item) => item.name === workerName);
  if (!worker) return "unknown";
  return worker.connection_state || (worker.enabled ? "unknown" : "disabled");
}

function openWorkerEdit(workerName) {
  const worker = state.workers.find((item) => item.name === workerName);
  if (!worker) {
    showNotice("Worker is no longer available", "error");
    return;
  }
  state.activeWorkerName = worker.name;
  const form = elements.workerEditForm.elements;
  form.name.value = worker.name;
  form.host.value = worker.host || "";
  form.user.value = worker.user || "";
  form.port.value = String(worker.port || 22);
  form.ssh_key.value = worker.ssh_key || "";
  form.tmux_prefix.value = worker.tmux_prefix || "gw";
  form.gpus.value = (worker.gpus || []).join(", ");
  form.max_concurrent_tasks.value = worker.max_concurrent_tasks ?? "";
  form.max_background_tasks.value = worker.max_background_tasks ?? 1;
  form.enabled.checked = Boolean(worker.enabled);
  elements.workerEditMeta.textContent = `${worker.name} · ${worker.host}:${worker.port}`;
  elements.workerEditStatus.textContent = "";
  elements.workerEditModal.classList.remove("hidden");
  document.body.classList.add("modal-open");
  requestAnimationFrame(() => form.host.focus());
}

function closeWorkerEdit() {
  state.activeWorkerName = null;
  elements.workerEditModal.classList.add("hidden");
  elements.workerEditStatus.textContent = "";
  if (
    !state.activeLogTaskId &&
    !state.activeDetailsTaskId &&
    !state.activeGpuDevice &&
    !state.activeNvtopWorker
  ) {
    document.body.classList.remove("modal-open");
  }
}

async function saveWorkerEdit(event) {
  event.preventDefault();
  if (!state.activeWorkerName) return;
  const submitButton = elements.workerEditForm.querySelector(
    'button[type="submit"]',
  );
  submitButton.disabled = true;
  elements.workerEditStatus.textContent = "Saving";
  try {
    const payload = workerPayloadFromForm(elements.workerEditForm);
    await putJson(
      `/workers/${encodeURIComponent(state.activeWorkerName)}`,
      payload,
    );
    showNotice(`Worker ${state.activeWorkerName} updated`, "success");
    closeWorkerEdit();
    await refresh();
  } catch (error) {
    elements.workerEditStatus.textContent = error.message;
  } finally {
    submitButton.disabled = false;
  }
}

function workerPayloadFromForm(form) {
  const data = new FormData(form);
  const gpuText = stringValue(data.get("gpus"));
  const gpus = gpuText
    ? gpuText
        .split(/[\s,]+/)
        .filter(Boolean)
        .map((value) => Number(value))
    : [];
  if (gpus.some((value) => !Number.isInteger(value) || value < 0)) {
    throw new Error("GPU indexes must be non-negative integers");
  }
  return {
    name: stringValue(data.get("name")),
    host: stringValue(data.get("host")),
    user: stringValue(data.get("user")) || null,
    port: numberValue(data.get("port"), 22),
    ssh_key: stringValue(data.get("ssh_key")) || null,
    enabled: data.get("enabled") === "on",
    tmux_prefix: stringValue(data.get("tmux_prefix")) || "gw",
    gpus,
    max_concurrent_tasks: optionalNumber(data.get("max_concurrent_tasks")),
    max_background_tasks: numberValue(data.get("max_background_tasks"), 1),
  };
}

function taskMatchesFilter(task) {
  if (state.filter === "all") return true;
  if (state.filter === "terminal") return terminalStatuses.has(task.status);
  return task.status === state.filter;
}

function policyHtml(task) {
  const labels = [];
  if (task.background) labels.push("background");
  if (task.elastic) labels.push("elastic");
  if (task.im_notify) labels.push("notify");
  if (task.allow_preempt) labels.push("preempt");
  if (task.preemptible) labels.push("yield");
  if (task.min_free_seconds > 0) labels.push(`${task.min_free_seconds}s`);
  if (labels.length === 0) return `<span class="pill">standard</span>`;
  return `<span class="policy">${labels.map((item) => `<span class="pill">${escapeHtml(item)}</span>`).join("")}</span>`;
}

function badgeHtml(status) {
  return `<span class="badge ${escapeHtml(status)}">${escapeHtml(status)}</span>`;
}

function detailItem(label, value, options = {}) {
  return `
    <div class="details-item">
      <dt>${escapeHtml(label)}</dt>
      <dd>${options.html ? value : escapeHtml(value)}</dd>
    </div>
  `;
}

function yesNo(value) {
  return value ? "yes" : "no";
}

function nullableValue(value) {
  return value === null || value === undefined || value === "" ? "-" : value;
}

function formatMemory(device) {
  const used = Number(device.memory_used_mb);
  const total = Number(device.memory_total_mb);
  if (Number.isFinite(total) && total > 0) {
    return `${used}/${total}`;
  }
  return String(device.memory_used_mb);
}

function uniqueGpuCount() {
  const seen = new Set();
  state.gpus.forEach((gpu) => seen.add(`${gpu.worker_name}:${gpu.gpu_index}`));
  return seen.size;
}

function reconcileActiveGpuWorker() {
  if (state.activeGpuWorker === "all") return;
  const workerNames = new Set(state.gpus.map((gpu) => gpu.worker_name));
  if (!workerNames.has(state.activeGpuWorker)) {
    state.activeGpuWorker = "all";
  }
}

function gpuDevices() {
  const byDevice = new Map();
  for (const sample of state.gpus) {
    const key = `${sample.worker_name}:${sample.gpu_index}`;
    const previous = byDevice.get(key);
    const isNewer = previous && sampleTime(sample) > sampleTime(previous);
    const isSameTimeAndPreferred =
      previous &&
      sampleTime(sample) === sampleTime(previous) &&
      sampleRank(sample) < sampleRank(previous);
    if (!previous || isNewer || isSameTimeAndPreferred) {
      byDevice.set(key, sample);
    }
  }
  return Array.from(byDevice.values()).sort((left, right) => {
    const workerCompare = String(left.worker_name).localeCompare(
      String(right.worker_name),
    );
    if (workerCompare !== 0) return workerCompare;
    return Number(left.gpu_index) - Number(right.gpu_index);
  });
}

function sampleTime(sample) {
  const timestamp = Date.parse(sample.last_seen);
  return Number.isFinite(timestamp) ? timestamp : Number.NEGATIVE_INFINITY;
}

function sampleRank(sample) {
  const memory = Number(sample.max_memory_used_mb);
  const utilization = Number(sample.max_utilization_percent);
  const memoryRank = Number.isFinite(memory) ? memory : Number.MAX_SAFE_INTEGER;
  const utilizationRank = Number.isFinite(utilization)
    ? utilization
    : Number.MAX_SAFE_INTEGER;
  return memoryRank * 1000 + utilizationRank;
}

function runningTaskForDevice(workerName, gpuIndex) {
  return state.runningTasks.find((task) => {
    if (task.status !== "running" || task.assigned_worker !== workerName)
      return false;
    return (task.assigned_gpus || []).map(Number).includes(Number(gpuIndex));
  });
}

function tasksPath() {
  const params = new URLSearchParams({
    limit: String(state.taskPageSize),
    offset: String((state.taskPage - 1) * state.taskPageSize),
  });
  if (state.filter !== "all") {
    params.set("status", state.filter);
  }
  if (state.taskSearch) {
    params.set("q", state.taskSearch);
  }
  return `/tasks?${params.toString()}`;
}

function nvtopStreamUrl(workerName, cols, rows) {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const params = new URLSearchParams({
    cols: String(cols),
    rows: String(rows),
  });
  return `${protocol}//${window.location.host}/workers/${encodeURIComponent(workerName)}/nvtop/stream?${params.toString()}`;
}

function logStreamUrl(taskId, tailBytes) {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const params = new URLSearchParams({ tail_bytes: String(tailBytes) });
  return `${protocol}//${window.location.host}/tasks/${encodeURIComponent(taskId)}/logs/stream?${params.toString()}`;
}

function maxTaskPage() {
  return Math.max(
    1,
    Math.ceil(Number(state.taskTotal || 0) / state.taskPageSize),
  );
}

async function getJson(path) {
  const response = await fetch(path, {
    headers: { accept: "application/json" },
  });
  return readResponse(response);
}

async function postJson(path, payload) {
  const response = await fetch(path, {
    method: "POST",
    headers: requestHeaders(),
    body: JSON.stringify(payload),
  });
  return readResponse(response);
}

async function putJson(path, payload) {
  const response = await fetch(path, {
    method: "PUT",
    headers: requestHeaders(),
    body: JSON.stringify(payload),
  });
  return readResponse(response);
}

async function readResponse(response) {
  if (response.status === 401) {
    window.location.assign("/login");
    throw new Error("Session expired");
  }
  const data = await response.json();
  if (!response.ok) {
    throw new Error(data.error || response.statusText);
  }
  return data;
}

let noticeTimer = null;

function showNotice(message, tone = "success") {
  if (noticeTimer) clearTimeout(noticeTimer);
  elements.notice.textContent = message;
  elements.notice.className = `notice ${tone}`;
  noticeTimer = setTimeout(() => elements.notice.classList.add("hidden"), 7000);
}

function requestHeaders() {
  const headers = {
    "content-type": "application/json",
    accept: "application/json",
  };
  if (state.csrfToken) headers["x-csrf-token"] = state.csrfToken;
  return headers;
}

async function logout() {
  try {
    await postJson("/auth/logout", {});
  } finally {
    window.location.assign("/login");
  }
}

function taskPayloadFromForm(form, includePaused) {
  const data = new FormData(form);
  const payload = {
    name: stringValue(data.get("name")) || "task",
    command: stringValue(data.get("command")),
    priority: numberValue(data.get("priority"), 0),
    requested_gpus: numberValue(data.get("requested_gpus"), 1),
    min_free_seconds: numberValue(data.get("min_free_seconds"), 0),
    preemptible: data.get("preemptible") === "on",
    allow_preempt: data.get("allow_preempt") === "on",
    background: data.get("background") === "on",
    elastic: data.get("elastic") === "on",
    im_notify: data.get("im_notify") === "on",
    log_mode: stringValue(data.get("log_mode")) || "pty",
    schedule_timezone: stringValue(data.get("schedule_timezone")) || "UTC",
    env: parseEnv(stringValue(data.get("env"))),
  };
  if (includePaused) payload.paused = data.get("paused") === "on";

  const cwd = stringValue(data.get("cwd"));
  const targetWorker = stringValue(data.get("target_worker"));
  const maxMemory = optionalNumber(data.get("max_memory_used_mb"));
  const minFreeMemory = optionalNumber(data.get("min_free_memory_mb"));
  const maxUtilization = optionalNumber(data.get("max_utilization_percent"));
  const callbackCommand = stringValue(data.get("callback_command"));
  const callbackEvents = stringValue(data.get("callback_events"));
  const startCron = stringValue(data.get("start_cron"));
  const stopCron = stringValue(data.get("stop_cron"));
  if (cwd) payload.cwd = cwd;
  if (targetWorker && targetWorker !== "auto")
    payload.target_worker = targetWorker;
  if (maxMemory !== null) payload.max_memory_used_mb = maxMemory;
  if (minFreeMemory !== null) payload.min_free_memory_mb = minFreeMemory;
  if (maxUtilization !== null) payload.max_utilization_percent = maxUtilization;
  if (callbackCommand) payload.callback_command = callbackCommand;
  if (startCron) payload.start_cron = startCron;
  if (stopCron) payload.stop_cron = stopCron;
  if (callbackEvents) {
    payload.callback_events = callbackEvents
      .split(",")
      .map((item) => item.trim())
      .filter(Boolean);
  }
  return payload;
}

function parseEnv(text) {
  const env = {};
  text
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)
    .forEach((line) => {
      const index = line.indexOf("=");
      if (index > 0) {
        env[line.slice(0, index)] = line.slice(index + 1);
      }
    });
  return env;
}

function envToText(env) {
  return Object.entries(env || {})
    .map(([key, value]) => `${key}=${value}`)
    .join("\n");
}

function taskIsEditable(task) {
  if (task.status === "paused") return true;
  return (
    task.status === "queued" &&
    !task.started_at &&
    !task.assigned_worker &&
    !task.tmux_session
  );
}

function workerOptionsHtml(selectedWorker) {
  const options = [{ name: "auto", label: "Auto" }].concat(
    state.workers.map((worker) => ({
      name: worker.name,
      label: worker.enabled
        ? worker.connection_state === "offline"
          ? `${worker.name} (offline)`
          : worker.name
        : `${worker.name} (disabled)`,
    })),
  );
  return options
    .map(
      (option) =>
        `<option value="${escapeHtml(option.name)}" ${selectedAttr(option.name === selectedWorker)}>${escapeHtml(option.label)}</option>`,
    )
    .join("");
}

function editValue(value) {
  return value === null || value === undefined ? "" : value;
}

function selectedAttr(selected) {
  return selected ? "selected" : "";
}

function checkedAttr(checked) {
  return checked ? "checked" : "";
}

function stringValue(value) {
  return String(value || "").trim();
}

function numberValue(value, fallback) {
  const number = Number(value);
  return Number.isFinite(number) ? number : fallback;
}

function optionalNumber(value) {
  const text = stringValue(value);
  if (!text) return null;
  const number = Number(text);
  return Number.isFinite(number) ? number : null;
}

function shortId(id) {
  return String(id || "").slice(0, 8);
}

function formatTime(value) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}

function formatDuration(value) {
  if (value === null || value === undefined) return "-";
  let seconds = Number(value);
  if (!Number.isFinite(seconds)) return "-";
  seconds = Math.max(0, Math.floor(seconds));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const rest = seconds % 60;
  if (hours)
    return `${hours}h${String(minutes).padStart(2, "0")}m${String(rest).padStart(2, "0")}s`;
  if (minutes) return `${minutes}m${String(rest).padStart(2, "0")}s`;
  return `${rest}s`;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

export {
  refresh,
  setActiveView,
  setAutoRefresh,
  logout,
  openTaskDetails,
  openLogs,
  startTask,
  pauseTask,
  rerunTask,
  terminateTask,
  openWorkerEdit,
  openNvtop,
  openGpuDetails,
  gpuDevices,
  runningTaskForDevice,
  workerConnectionState,
  jumpToInfrastructure,
  jumpToTask,
  formatDuration,
  formatTime,
  shortId,
  maxTaskPage,
};
