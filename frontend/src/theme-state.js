import { ref } from "vue";
export const theme = ref(
  document.documentElement.dataset.themePreference || "system",
);
