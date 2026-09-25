<script setup>
import { computed, onMounted, ref, watch } from "vue";
import Icon from "./Icon.vue";
import ThemeSwitcher from "./ThemeSwitcher.vue";
import SubmitForm from "./SubmitForm.vue";
import Dialogs from "./Dialogs.vue";
import {
  state,
  initialize,
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
} from "./controller.js";
const autoRefresh = ref(true);
const search = ref("");
let searchTimer;
watch(search, (value) => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    state.taskSearch = value.trim();
    state.taskPage = 1;
    refresh();
  }, 250);
});
watch(
  () => state.taskSearch,
  (value) => {
    if (search.value.trim() !== value) search.value = value;
  },
);
const views = {
  tasks: { title: "Tasks" },
  infrastructure: { title: "Infrastructure" },
  submit: { title: "Create a task" },
};
const metrics = computed(() => [
  {
    label: "Running tasks",
    value: state.taskSummary.running || 0,
    icon: "Activity",
    tone: "green",
    filter: "running",
  },
  {
    label: "In queue",
    value: state.taskSummary.queued || 0,
    icon: "Layers",
    tone: "amber",
    filter: "queued",
  },
  {
    label: "Paused tasks",
    value: state.taskSummary.paused || 0,
    icon: "Pause",
    tone: "neutral",
    filter: "paused",
  },
  {
    label: "Workers",
    value: state.workers.length,
    icon: "Server",
    hint: `${state.workers.filter((w) => w.connection_state === "online").length} online`,
    tone: "blue",
    view: "infrastructure",
  },
  {
    label: "GPU devices",
    value: gpuDevices().length,
    icon: "Cpu",
    tone: "violet",
    view: "infrastructure",
  },
]);
const filters = [
  { value: "all", label: "All tasks" },
  { value: "running", label: "Running" },
  { value: "queued", label: "Queued" },
  { value: "paused", label: "Paused" },
  { value: "terminal", label: "Finished" },
];
const devices = computed(() =>
  gpuDevices().filter(
    (g) =>
      state.activeGpuWorker === "all" ||
      g.worker_name === state.activeGpuWorker,
  ),
);
const workerNames = computed(() =>
  [
    ...new Set([
      ...state.workers.map((w) => w.name),
      ...state.gpus.map((g) => g.worker_name),
    ]),
  ].sort(),
);
const pageInfo = computed(() =>
  state.taskTotal
    ? `${(state.taskPage - 1) * state.taskPageSize + 1}–${Math.min(state.taskPage * state.taskPageSize, state.taskTotal)} of ${state.taskTotal} tasks`
    : "0 tasks",
);
function filterTasks(value) {
  state.filter = value;
  state.taskPage = 1;
  refresh();
}
function selectMetric(metric) {
  setActiveView(metric.view || "tasks");
  if (metric.filter) filterTasks(metric.filter);
}
function paginate(offset) {
  state.taskPage += offset;
  refresh();
}
function pageSize(event) {
  state.taskPageSize = Number(event.target.value);
  state.taskPage = 1;
  refresh();
}
function clearSearch() {
  clearTimeout(searchTimer);
  search.value = "";
  state.taskSearch = "";
  state.filter = "all";
  state.taskPage = 1;
  refresh();
}
function gpuStatus(gpu) {
  return workerConnectionState(gpu.worker_name) === "offline"
    ? "offline"
    : runningTaskForDevice(gpu.worker_name, gpu.gpu_index)
      ? "allocated"
      : Number(gpu.is_free)
        ? "available"
        : "busy";
}
function percentage(value, total = 100) {
  return Math.max(
    0,
    Math.min(100, ((Number(value) || 0) / (Number(total) || 1)) * 100),
  );
}
function memory(value) {
  return value == null ? "—" : `${(Number(value) / 1024).toFixed(1)} GB`;
}
onMounted(() => {
  initialize();
  // Keep keyboard focus inside operational dialogs and restore it to their trigger.
  let previousFocus;
  const observer = new MutationObserver(() => {
    const modal = document.querySelector(".modal:not(.hidden)");
    if (modal) {
      if (!previousFocus) previousFocus = document.activeElement;
      if (!modal.contains(document.activeElement))
        modal.querySelector("button:not([hidden]), input")?.focus();
    } else if (previousFocus) {
      if (previousFocus.isConnected) previousFocus.focus();
      previousFocus = null;
    }
  });
  document
    .querySelectorAll(".modal")
    .forEach((el) =>
      observer.observe(el, { attributes: true, attributeFilter: ["class"] }),
    );
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Tab") return;
    const modal = document.querySelector(".modal:not(.hidden)");
    if (!modal) return;
    const focusable = [
      ...modal.querySelectorAll(
        'button, input, select, textarea, a[href], [tabindex="0"]',
      ),
    ].filter((el) => !el.disabled && el.getClientRects().length);
    const first = focusable[0],
      last = focusable.at(-1);
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last?.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first?.focus();
    }
  });
});
</script>

<template>
  <div class="workspace">
    <a class="skip-link" href="#workspace-main">Skip to content</a>
    <aside class="sidebar">
      <a class="workspace-brand" href="#tab:tasks" aria-label="GPU Watcher home"
        ><span class="brand-symbol"><Icon name="Cpu" :size="23" /></span
        ><span>GPU Watcher</span></a
      >
      <nav class="nav-list" aria-label="Main navigation">
        <button
          type="button"
          aria-label="Tasks"
          :class="{ active: state.activeView === 'tasks' }"
          :aria-current="state.activeView === 'tasks' ? 'page' : undefined"
          @click="setActiveView('tasks')"
        >
          <Icon name="Layers" /><span>Tasks</span
          ><span class="nav-count">{{ state.taskSummary.running || 0 }}</span>
        </button>
        <button
          type="button"
          aria-label="Infrastructure"
          data-view="infrastructure"
          :class="{ active: state.activeView === 'infrastructure' }"
          :aria-current="
            state.activeView === 'infrastructure' ? 'page' : undefined
          "
          @click="setActiveView('infrastructure')"
        >
          <Icon name="Server" /><span>Infrastructure</span>
        </button>
        <button
          type="button"
          aria-label="Create task"
          :class="{ active: state.activeView === 'submit' }"
          :aria-current="state.activeView === 'submit' ? 'page' : undefined"
          @click="setActiveView('submit')"
        >
          <Icon name="Plus" /><span>Create task</span>
        </button>
      </nav>
      <div class="sidebar-bottom">
        <div class="sidebar-footer">
          <span>Appearance</span><ThemeSwitcher />
        </div>
        <button
          v-if="state.authEnabled"
          class="signout"
          type="button"
          @click="logout"
        >
          <Icon name="LogOut" :size="16" /> Sign out
        </button>
      </div>
    </aside>

    <div class="workspace-body">
      <header class="workspace-topbar">
        <div class="breadcrumb">
          <span>Workspace</span
          ><Icon name="ChevronRight" :size="14" /><strong>{{
            state.activeView === "tasks"
              ? "Tasks"
              : state.activeView === "submit"
                ? "Create task"
                : "Infrastructure"
          }}</strong>
        </div>
        <div
          class="connection-indicator"
          :class="{ online: state.online, degraded: state.error }"
        >
          <span></span
          >{{
            state.error
              ? "Connection issue"
              : state.online
                ? "System online"
                : "Connecting"
          }}
        </div>
        <div class="mobile-theme"><ThemeSwitcher /></div>
      </header>
      <main id="workspace-main" class="workspace-main" tabindex="-1">
        <div class="page-intro">
          <div>
            <h1>{{ views[state.activeView].title }}</h1>
          </div>
          <button
            id="submitViewButton"
            class="primary-button"
            type="button"
            @click="setActiveView('submit')"
          >
            <Icon name="Plus" :size="17" /> Create task
          </button>
        </div>
        <div v-if="state.error" class="connection-error" role="alert">
          <Icon name="Activity" />
          <div>
            <strong>Couldn’t sync your workspace</strong>
            <p>
              {{ state.error
              }}{{
                state.loaded
                  ? " Displaying the last received data."
                  : " Check your connection and try again."
              }}
            </p>
          </div>
          <button type="button" @click="refresh">Retry</button>
        </div>
        <section class="summary-grid" aria-label="Workspace summary">
          <button
            v-for="metric in metrics"
            :key="metric.label"
            class="summary-card"
            type="button"
            :class="metric.tone"
            @click="selectMetric(metric)"
          >
            <div class="summary-label">
              <span>{{ metric.label }}</span
              ><Icon :name="metric.icon" :size="17" />
            </div>
            <strong :class="{ skeleton: !state.loaded && state.loading }">{{
              state.loaded ? metric.value : "—"
            }}</strong
            ><span v-if="metric.hint" class="summary-hint">{{
              metric.hint
            }}</span>
          </button>
        </section>
        <section
          v-show="state.activeView === 'tasks'"
          class="content-section"
          aria-label="Tasks"
        >
          <div class="section-title">
            <div>
              <h2>
                Workloads <span class="count-label">{{ state.taskTotal }}</span>
              </h2>
            </div>
            <div class="sync-controls">
              <label class="auto-toggle"
                ><input
                  type="checkbox"
                  v-model="autoRefresh"
                  @change="setAutoRefresh(autoRefresh)"
                /><span class="switch-track"></span
                ><span>Live updates</span></label
              ><button
                class="icon-button"
                type="button"
                aria-label="Refresh workspace"
                title="Refresh workspace"
                :disabled="state.loading"
                @click="refresh"
              >
                <Icon
                  name="RefreshCw"
                  :class="{ spinning: state.loading }"
                  :size="17"
                />
              </button>
            </div>
          </div>
          <div class="workload-panel" :aria-busy="state.loading">
            <div class="workload-toolbar">
              <div
                class="filter-list"
                role="group"
                aria-label="Task status filter"
              >
                <button
                  v-for="filter in filters"
                  :key="filter.value"
                  type="button"
                  :class="{ selected: state.filter === filter.value }"
                  :aria-pressed="state.filter === filter.value"
                  @click="filterTasks(filter.value)"
                >
                  <span
                    v-if="filter.value === 'running'"
                    class="running-dot"
                  ></span
                  >{{ filter.label }}
                </button>
              </div>
              <label class="search-box"
                ><Icon name="Search" :size="17" /><span class="visually-hidden"
                  >Search tasks by name or ID</span
                ><input
                  id="taskSearch"
                  v-model="search"
                  type="search"
                  placeholder="Search tasks…"
                  autocomplete="off"
              /></label>
            </div>
            <div class="task-table-wrap">
              <table class="workload-table">
                <thead>
                  <tr>
                    <th>Task name</th>
                    <th>Status</th>
                    <th>Resources</th>
                    <th>Worker</th>
                    <th>Duration</th>
                    <th>Priority</th>
                    <th><span class="visually-hidden">Actions</span></th>
                  </tr>
                </thead>
                <tbody id="tasksBody">
                  <tr v-if="!state.loaded && state.loading">
                    <td colspan="7">
                      <div class="empty-state">
                        <Icon name="RefreshCw" class="spinning" :size="28" />
                        <h3>Loading your workspace</h3>
                        <p>Fetching tasks and compute resources…</p>
                      </div>
                    </td>
                  </tr>
                  <tr v-else-if="!state.tasks.length">
                    <td colspan="7">
                      <div class="empty-state">
                        <span class="empty-icon"
                          ><Icon
                            :name="state.error ? 'Activity' : 'Inbox'"
                            :size="27"
                        /></span>
                        <h3>
                          {{
                            state.error && !state.loaded
                              ? "Workspace unavailable"
                              : state.taskSearch || state.filter !== "all"
                                ? "No matching tasks"
                                : "Ready for your next workload"
                          }}
                        </h3>
                        <p>
                          {{
                            state.error && !state.loaded
                              ? "Retry to load your tasks."
                              : state.taskSearch || state.filter !== "all"
                                ? "Try a different search or status filter."
                                : "Create a task to put your GPUs to work."
                          }}
                        </p>
                        <button
                          v-if="state.error && !state.loaded"
                          type="button"
                          @click="refresh"
                        >
                          Retry</button
                        ><button
                          v-else-if="state.taskSearch || state.filter !== 'all'"
                          type="button"
                          @click="clearSearch"
                        >
                          Clear filters</button
                        ><button
                          v-else
                          class="primary-button"
                          type="button"
                          @click="setActiveView('submit')"
                        >
                          <Icon name="Plus" :size="16" /> Create task
                        </button>
                      </div>
                    </td>
                  </tr>
                  <tr
                    v-for="task in state.tasks"
                    :key="task.id"
                    :data-task-id="task.id"
                  >
                    <td data-label="Task" class="task-identity">
                      <span class="task-symbol"
                        ><Icon name="Terminal" :size="18"
                      /></span>
                      <div>
                        <button
                          class="task-name"
                          type="button"
                          @click="openTaskDetails(task.id)"
                        >
                          {{ task.name }}</button
                        ><span class="task-id"
                          >{{ shortId(task.id)
                          }}<span v-if="task.background"> · background</span
                          ><span v-if="task.elastic"> · elastic</span></span
                        >
                      </div>
                    </td>
                    <td data-label="Status">
                      <span class="status-badge" :class="task.status"
                        ><i></i>{{ task.status }}</span
                      >
                    </td>
                    <td data-label="Resources">
                      <span class="resource-label"
                        ><Icon name="Cpu" :size="15" />{{
                          task.requested_gpus
                        }}
                        GPU{{ task.requested_gpus > 1 ? "s" : "" }}</span
                      ><small
                        v-if="task.assigned_gpus?.length"
                        class="cell-secondary"
                        >Device {{ task.assigned_gpus.join(", ") }}</small
                      >
                    </td>
                    <td data-label="Worker">
                      <button
                        v-if="task.assigned_worker"
                        class="worker-link"
                        type="button"
                        @click="jumpToInfrastructure(task.assigned_worker)"
                      >
                        {{ task.assigned_worker
                        }}<Icon name="ArrowUpRight" :size="12" /></button
                      ><span v-else class="muted">{{
                        task.target_worker || "Auto assign"
                      }}</span>
                    </td>
                    <td data-label="Duration" class="mono">
                      {{ formatDuration(task.duration_seconds) }}
                    </td>
                    <td data-label="Priority">
                      <span class="priority-value">{{ task.priority }}</span>
                    </td>
                    <td class="task-row-actions" data-label="Actions">
                      <div class="task-controls">
                        <button
                          class="icon-button"
                          type="button"
                          :aria-label="`Logs for ${task.name}`"
                          title="View logs"
                          @click="openLogs(task.id)"
                        >
                          <Icon name="Terminal" :size="17" />
                        </button>
                        <button
                          v-if="task.status === 'paused'"
                          class="icon-button inline-task-action"
                          type="button"
                          :aria-label="`Start ${task.name}`"
                          title="Start task"
                          @click="startTask(task.id)"
                        >
                          <Icon name="Play" :size="17" />
                        </button>
                        <button
                          v-if="
                            ['queued', 'running'].includes(task.status) &&
                            task.preemptible
                          "
                          class="icon-button inline-task-action"
                          type="button"
                          :aria-label="`Pause ${task.name}`"
                          title="Pause task"
                          @click="pauseTask(task.id)"
                        >
                          <Icon name="Pause" :size="17" />
                        </button>
                        <button
                          v-if="
                            ['paused', 'queued', 'running'].includes(
                              task.status,
                            )
                          "
                          type="button"
                          :aria-label="`Terminate ${task.name}`"
                          title="Terminate task"
                          class="icon-button inline-task-action danger-text"
                          @click="terminateTask(task.id)"
                        >
                          <Icon name="Square" :size="17" />
                        </button>
                        <button
                          v-if="task.status === 'failed'"
                          class="icon-button inline-task-action"
                          type="button"
                          :aria-label="`Rerun ${task.name}`"
                          title="Rerun task"
                          @click="rerunTask(task.id)"
                        >
                          <Icon name="RefreshCw" :size="17" />
                        </button>
                        <button
                          class="icon-button inline-task-action"
                          type="button"
                          :aria-label="`Details for ${task.name}`"
                          title="Details / edit"
                          @click="openTaskDetails(task.id)"
                        >
                          <Icon name="SlidersHorizontal" :size="17" />
                        </button>
                      </div>
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
            <div class="table-pagination">
              <span>{{ pageInfo }}</span>
              <div class="flex items-center gap-3">
                <label class="rows-select"
                  ><span>Rows</span
                  ><select
                    aria-label="Tasks per page"
                    :value="state.taskPageSize"
                    @change="pageSize"
                  >
                    <option v-for="count in [10, 20, 50, 100]" :key="count">
                      {{ count }}
                    </option>
                  </select></label
                ><span class="page-number"
                  >{{ state.taskPage }} / {{ maxTaskPage() }}</span
                ><button
                  class="icon-button"
                  type="button"
                  aria-label="Previous page"
                  :disabled="state.taskPage <= 1"
                  @click="paginate(-1)"
                >
                  <Icon name="ChevronLeft" :size="17" /></button
                ><button
                  class="icon-button"
                  type="button"
                  aria-label="Next page"
                  :disabled="state.taskPage >= maxTaskPage()"
                  @click="paginate(1)"
                >
                  <Icon name="ChevronRight" :size="17" />
                </button>
              </div>
            </div>
          </div>
          <div class="workspace-footnote">
            <span
              ><Icon name="RefreshCw" :size="13" />{{
                autoRefresh
                  ? "Updates automatically every 5 seconds"
                  : "Live updates paused"
              }}</span
            ><span>{{
              state.updatedAt
                ? `Last synced ${state.updatedAt}`
                : "Waiting for first sync"
            }}</span>
          </div>
        </section>

        <section
          v-show="state.activeView === 'infrastructure'"
          class="content-section"
          aria-label="Infrastructure"
        >
          <div class="section-title">
            <div>
              <h2>
                GPU devices
                <span class="count-label">{{ devices.length }}</span>
              </h2>
            </div>
            <button
              class="icon-button"
              type="button"
              aria-label="Refresh infrastructure"
              :disabled="state.loading"
              @click="refresh"
            >
              <Icon name="RefreshCw" :class="{ spinning: state.loading }" />
            </button>
          </div>
          <div
            class="worker-filters filter-list"
            role="group"
            aria-label="Filter by worker"
          >
            <button
              type="button"
              :class="{ selected: state.activeGpuWorker === 'all' }"
              @click="state.activeGpuWorker = 'all'"
            >
              All workers</button
            ><button
              v-for="name in workerNames"
              :key="name"
              type="button"
              :class="{ selected: state.activeGpuWorker === name }"
              @click="state.activeGpuWorker = name"
            >
              {{ name }}
            </button>
          </div>
          <div class="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
            <article
              v-for="gpu in devices"
              :key="`${gpu.worker_name}:${gpu.gpu_index}`"
              class="gpu-card"
            >
              <div class="flex items-center justify-between gap-3">
                <span class="device-mark"><Icon name="Cpu" :size="23" /></span
                ><span class="status-badge" :class="gpuStatus(gpu)"
                  ><i></i>{{ gpuStatus(gpu) }}</span
                >
              </div>
              <h3>
                GPU {{ gpu.gpu_index }} <span>{{ gpu.worker_name }}</span>
              </h3>
              <div class="gpu-meter">
                <div>
                  <span>Memory</span
                  ><strong>{{
                    gpuStatus(gpu) === "offline"
                      ? "N/A"
                      : memory(gpu.memory_used_mb) +
                        " / " +
                        memory(gpu.memory_total_mb)
                  }}</strong>
                </div>
                <div class="meter-track">
                  <span
                    :style="{
                      width:
                        gpuStatus(gpu) === 'offline'
                          ? '0%'
                          : percentage(
                              gpu.memory_used_mb,
                              gpu.memory_total_mb,
                            ) + '%',
                    }"
                  ></span>
                </div>
              </div>
              <div class="gpu-meter">
                <div>
                  <span>Utilization</span
                  ><strong>{{
                    gpuStatus(gpu) === "offline"
                      ? "N/A"
                      : (gpu.utilization_percent ?? "—") + "%"
                  }}</strong>
                </div>
                <div class="meter-track utilization">
                  <span
                    :style="{
                      width:
                        gpuStatus(gpu) === 'offline'
                          ? '0%'
                          : percentage(gpu.utilization_percent) + '%',
                    }"
                  ></span>
                </div>
              </div>
              <div class="gpu-owner">
                <span>{{
                  gpuStatus(gpu) === "offline"
                    ? "Worker offline"
                    : (gpu.process_users || []).join(", ") || "No active users"
                }}</span
                ><span>NUMA {{ gpu.numa_node ?? "—" }}</span>
              </div>
              <button
                v-if="runningTaskForDevice(gpu.worker_name, gpu.gpu_index)"
                class="allocated-link"
                type="button"
                @click="
                  jumpToTask(
                    runningTaskForDevice(gpu.worker_name, gpu.gpu_index).id,
                  )
                "
              >
                <Icon name="Play" :size="13" />{{
                  runningTaskForDevice(gpu.worker_name, gpu.gpu_index).name
                }}<Icon name="ArrowUpRight" :size="14" />
              </button>
              <div class="gpu-card-footer">
                <small :title="formatTime(gpu.last_seen)"
                  >Seen {{ formatTime(gpu.last_seen) }}</small
                ><button
                  type="button"
                  @click="
                    openGpuDetails(gpu.worker_name, Number(gpu.gpu_index))
                  "
                >
                  Details<Icon name="ArrowUpRight" :size="14" />
                </button>
              </div>
            </article>
          </div>
          <div v-if="!devices.length" class="empty-state workload-panel">
            <Icon name="Cpu" :size="28" />
            <h3>No GPU samples yet</h3>
            <p>Devices will appear when a worker reports its first sample.</p>
          </div>
          <div class="section-title mt-4">
            <div>
              <h2>
                Workers
                <span class="count-label">{{ state.workers.length }}</span>
              </h2>
            </div>
          </div>
          <div class="grid grid-cols-1 lg:grid-cols-2 gap-3">
            <article
              v-for="worker in state.workers"
              :key="worker.name"
              class="worker-card"
            >
              <div class="flex items-start justify-between gap-3">
                <div class="flex items-center gap-3 min-w-0">
                  <span class="device-mark"
                    ><Icon name="Server" :size="20"
                  /></span>
                  <div class="min-w-0">
                    <h3>{{ worker.name }}</h3>
                    <p class="mono break-all">
                      {{ worker.user ? `${worker.user}@` : ""
                      }}{{ worker.host }}:{{ worker.port }}
                    </p>
                  </div>
                </div>
                <span
                  class="status-badge"
                  :class="worker.connection_state || 'unknown'"
                  >{{
                    worker.connection_state ||
                    (worker.enabled ? "unknown" : "disabled")
                  }}</span
                >
              </div>
              <dl class="worker-capacity">
                <div>
                  <dt>GPU devices</dt>
                  <dd>{{ (worker.gpus || []).join(", ") || "Auto" }}</dd>
                </div>
                <div>
                  <dt>Task capacity</dt>
                  <dd>{{ worker.max_concurrent_tasks || "Unlimited" }}</dd>
                </div>
                <div>
                  <dt>Background</dt>
                  <dd>{{ worker.max_background_tasks ?? 1 }}</dd>
                </div>
              </dl>
              <p v-if="worker.last_probe_error" class="worker-error">
                {{ worker.last_probe_error }}
              </p>
              <div class="flex justify-between items-center gap-3">
                <span class="muted text-xs">{{
                  worker.enabled ? "Scheduling enabled" : "Scheduling disabled"
                }}</span>
                <div class="flex gap-2">
                  <button type="button" @click="openNvtop(worker.name)">
                    <Icon name="Terminal" :size="15" /> NVTop</button
                  ><button type="button" @click="openWorkerEdit(worker.name)">
                    <Icon name="SlidersHorizontal" :size="15" /> Edit
                  </button>
                </div>
              </div>
            </article>
          </div>
          <div v-if="!state.workers.length" class="empty-state workload-panel">
            <Icon name="Server" :size="28" />
            <h3>No workers configured</h3>
            <p>Add a worker to your server configuration to get started.</p>
          </div>
        </section>
        <section
          v-show="state.activeView === 'submit'"
          class="content-section submit-workspace"
          aria-label="Create task"
        >
          <div class="panel">
            <div class="panel-heading">
              <h2>Task configuration</h2>
              <span id="submitStatus" role="status"></span>
            </div>
            <SubmitForm />
          </div>
        </section>
      </main>
    </div>
    <Dialogs />
  </div>
</template>
