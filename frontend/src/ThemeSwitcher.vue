<script setup>
import { theme } from "./theme-state.js";
import Icon from "./Icon.vue";
const modes = [
  { value: "light", icon: "Sun" },
  { value: "dark", icon: "Moon" },
  { value: "system", icon: "Monitor" },
];
function setTheme(mode) {
  theme.value = mode;
  window.GpuWatcherTheme.set(mode);
}
</script>
<template>
  <div class="appearance" role="group" aria-label="Appearance">
    <button
      v-for="mode in modes"
      :key="mode.value"
      type="button"
      :aria-label="`${mode.value} appearance`"
      :title="`${mode.value} appearance`"
      :aria-pressed="theme === mode.value"
      :class="{ selected: theme === mode.value }"
      @click="setTheme(mode.value)"
    >
      <Icon :name="mode.icon" :size="16" />
    </button>
  </div>
</template>
