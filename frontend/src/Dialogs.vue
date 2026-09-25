<template>
  <div v-once>
    <div
      id="notice"
      class="notice hidden"
      role="status"
      aria-live="polite"
    ></div>

    <div
      id="logModal"
      class="modal hidden"
      role="dialog"
      aria-modal="true"
      aria-labelledby="logTitle"
    >
      <div class="modal-backdrop" data-close-logs></div>
      <section class="log-dialog">
        <div class="log-heading">
          <div>
            <h3 id="logTitle">Task Logs</h3>
            <p id="logMeta"></p>
          </div>
          <div class="toolbar">
            <label>
              <span>Backlog KB</span>
              <input
                id="logTailKb"
                type="number"
                min="1"
                max="8192"
                value="256"
              />
            </label>
            <span id="logLiveStatus" class="live-status">Live</span>
            <button id="followLogsButton" type="button">Follow</button>
            <button id="endLogsButton" type="button">End</button>
            <button id="reconnectLogsButton" type="button">Reconnect</button>
            <button
              id="closeLogsButton"
              type="button"
              aria-label="Close task logs"
            >
              Close
            </button>
          </div>
        </div>
        <div id="logTerminal" class="log-terminal"></div>
      </section>
    </div>

    <div
      id="nvtopModal"
      class="modal hidden"
      role="dialog"
      aria-modal="true"
      aria-labelledby="nvtopTitle"
    >
      <div class="modal-backdrop" data-close-nvtop></div>
      <section class="log-dialog nvtop-dialog">
        <div class="log-heading">
          <div>
            <h3 id="nvtopTitle">NVTop</h3>
            <p id="nvtopMeta"></p>
          </div>
          <div class="toolbar">
            <span id="nvtopLiveStatus" class="live-status">Connecting</span>
            <button id="reconnectNvtopButton" type="button">Reconnect</button>
            <button
              id="closeNvtopButton"
              type="button"
              aria-label="Close NVTop terminal"
            >
              Close
            </button>
          </div>
        </div>
        <div id="nvtopTerminal" class="log-terminal nvtop-terminal"></div>
      </section>
    </div>

    <div
      id="taskDetailsModal"
      class="modal hidden"
      role="dialog"
      aria-modal="true"
      aria-labelledby="taskDetailsTitle"
    >
      <div class="modal-backdrop" data-close-task-details></div>
      <section class="details-dialog">
        <div class="details-heading">
          <div>
            <h3 id="taskDetailsTitle">Task Details</h3>
            <p id="taskDetailsMeta"></p>
          </div>
          <div class="toolbar">
            <button id="detailsEditButton" type="button">Edit</button>
            <button id="detailsStartButton" type="button">Start</button>
            <button id="detailsPauseButton" type="button">Pause</button>
            <button id="detailsRerunButton" type="button">Rerun</button>
            <button id="detailsTerminateButton" type="button">Terminate</button>
            <button id="detailsLogsButton" type="button">Logs</button>
            <button
              id="closeTaskDetailsButton"
              type="button"
              aria-label="Close task details"
            >
              Close
            </button>
          </div>
        </div>
        <div id="taskDetailsBody" class="details-body"></div>
      </section>
    </div>

    <div
      id="workerEditModal"
      class="modal hidden"
      role="dialog"
      aria-modal="true"
      aria-labelledby="workerEditTitle"
    >
      <div class="modal-backdrop" data-close-worker-edit></div>
      <section class="details-dialog worker-dialog">
        <div class="details-heading">
          <div>
            <h3 id="workerEditTitle">Edit Worker</h3>
            <p id="workerEditMeta"></p>
          </div>
          <button
            id="closeWorkerEditButton"
            type="button"
            aria-label="Close worker editor"
          >
            Close
          </button>
        </div>
        <div class="details-body">
          <form id="workerEditForm" class="worker-edit-form">
            <label><span>Name</span><input name="name" readonly /></label>
            <label class="worker-edit-wide"
              ><span>Host</span><input name="host" required
            /></label>
            <label><span>User</span><input name="user" /></label>
            <label
              ><span>Port</span
              ><input name="port" type="number" min="1" max="65535" required
            /></label>
            <label class="worker-edit-wide"
              ><span>SSH key</span><input name="ssh_key"
            /></label>
            <label
              ><span>tmux prefix</span><input name="tmux_prefix" required
            /></label>
            <label class="worker-edit-wide"
              ><span>GPUs</span><input name="gpus" placeholder="0, 1, 2, 3"
            /></label>
            <label
              ><span>Max tasks</span
              ><input
                name="max_concurrent_tasks"
                type="number"
                min="1"
                placeholder="unlimited"
            /></label>
            <label
              ><span>Max background</span
              ><input
                name="max_background_tasks"
                type="number"
                min="0"
                required
            /></label>
            <div class="worker-edit-options">
              <label class="toggle"
                ><input name="enabled" type="checkbox" /><span
                  >Enabled</span
                ></label
              >
            </div>
            <div class="edit-actions">
              <span id="workerEditStatus" role="status"></span>
              <button id="cancelWorkerEditButton" type="button">Cancel</button>
              <button type="submit">Save Worker</button>
            </div>
          </form>
        </div>
      </section>
    </div>

    <div
      id="gpuDetailsModal"
      class="modal hidden"
      role="dialog"
      aria-modal="true"
      aria-labelledby="gpuDetailsTitle"
    >
      <div class="modal-backdrop" data-close-gpu-details></div>
      <section class="details-dialog gpu-details-dialog">
        <div class="details-heading">
          <div>
            <h3 id="gpuDetailsTitle">GPU Status Detail</h3>
            <p id="gpuDetailsMeta"></p>
          </div>
          <button
            id="closeGpuDetailsButton"
            type="button"
            aria-label="Close GPU status detail"
          >
            Close
          </button>
        </div>
        <div id="gpuDetailsBody" class="details-body"></div>
      </section>
    </div>
  </div>
</template>
