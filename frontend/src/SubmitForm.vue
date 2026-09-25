<template>
  <div v-once>
    <form id="taskForm" class="task-form">
      <section class="form-section">
        <h3>Placement</h3>
        <div class="form-grid form-grid-four">
          <label
            ><span>Name</span
            ><input name="name" value="task" autocomplete="off"
          /></label>
          <label
            ><span>Priority</span
            ><input name="priority" type="number" value="0"
          /></label>
          <label
            ><span>GPUs</span
            ><input name="requested_gpus" type="number" min="1" value="1"
          /></label>
          <label>
            <span>Target worker</span>
            <select name="target_worker" id="targetWorker">
              <option value="auto">Auto</option>
            </select>
          </label>
        </div>
      </section>

      <section class="form-section">
        <h3>Launch Conditions</h3>
        <div class="form-grid form-grid-four">
          <label
            ><span>Max memory used MB</span
            ><input
              name="max_memory_used_mb"
              type="number"
              min="0"
              placeholder="default"
          /></label>
          <label
            ><span>Min free memory MB</span
            ><input
              name="min_free_memory_mb"
              type="number"
              min="0"
              placeholder="optional"
          /></label>
          <label
            ><span>Max utilization %</span
            ><input
              name="max_utilization_percent"
              type="number"
              min="0"
              max="100"
              placeholder="default"
          /></label>
          <label
            ><span>Min free seconds</span
            ><input name="min_free_seconds" type="number" min="0" value="0"
          /></label>
        </div>
      </section>

      <section class="form-section">
        <h3>Execution</h3>
        <div class="form-grid form-grid-execution">
          <label class="command-field"
            ><span>Command</span
            ><textarea
              name="command"
              rows="5"
              required
              placeholder="python train.py"
            ></textarea>
          </label>
          <label
            ><span>Working directory</span
            ><input name="cwd" placeholder="/path/on/gpu/host"
          /></label>
          <label
            ><span>Log mode</span
            ><select name="log_mode">
              <option value="pty" selected>PTY</option>
              <option value="pipe">Pipe</option>
            </select></label
          >
          <label class="wide"
            ><span>Environment</span
            ><textarea name="env" rows="3" placeholder="KEY=value"></textarea>
          </label>
          <label class="wide"
            ><span>Callback command</span
            ><textarea
              name="callback_command"
              rows="3"
              placeholder="/path/to/notify {title} {content}"
            ></textarea>
          </label>
          <label
            ><span>Callback events</span
            ><input
              name="callback_events"
              placeholder="succeeded,failed,switch"
          /></label>
          <label
            ><span>Start cron</span
            ><input name="start_cron" placeholder="0 9 * * 1-5"
          /></label>
          <label
            ><span>Stop cron</span
            ><input name="stop_cron" placeholder="0 18 * * 1-5"
          /></label>
          <label
            ><span>Schedule timezone</span
            ><input
              name="schedule_timezone"
              value="UTC"
              placeholder="Asia/Shanghai"
          /></label>
        </div>
      </section>

      <section class="form-section form-options">
        <h3>Options</h3>
        <div class="switch-row">
          <label class="toggle"
            ><input name="preemptible" type="checkbox" /><span
              >Preemptible</span
            ></label
          >
          <label class="toggle"
            ><input name="allow_preempt" type="checkbox" /><span
              >Allow preempt</span
            ></label
          >
          <label class="toggle"
            ><input name="background" type="checkbox" /><span
              >Background</span
            ></label
          >
          <label class="toggle"
            ><input name="elastic" type="checkbox" /><span>Elastic</span></label
          >
          <label class="toggle"
            ><input name="im_notify" type="checkbox" /><span
              >IM notify</span
            ></label
          >
          <label class="toggle"
            ><input name="paused" type="checkbox" /><span>Paused</span></label
          >
          <button type="submit">Submit Task</button>
        </div>
      </section>
    </form>
  </div>
</template>
