(() => {
  const storageKey = "gpu-watcher.theme";
  const modes = new Set(["light", "dark", "system"]);
  const colorScheme = window.matchMedia?.("(prefers-color-scheme: dark)");

  function savedMode() {
    try {
      const mode = window.localStorage.getItem(storageKey);
      return modes.has(mode) ? mode : "system";
    } catch {
      return "system";
    }
  }

  function resolvedTheme(mode = savedMode()) {
    return mode === "system" ? (colorScheme?.matches ? "dark" : "light") : mode;
  }

  function syncControls(mode = savedMode()) {
    document.querySelectorAll("[data-theme-option]").forEach((button) => {
      const active = button.dataset.themeOption === mode;
      button.classList.toggle("active", active);
      button.setAttribute("aria-pressed", String(active));
    });
  }

  function apply(mode = savedMode()) {
    const preference = modes.has(mode) ? mode : "system";
    const theme = resolvedTheme(preference);
    document.documentElement.dataset.theme = theme;
    document.documentElement.dataset.themePreference = preference;
    document.documentElement.style.colorScheme = theme;
    syncControls(preference);
  }

  function set(mode) {
    const preference = modes.has(mode) ? mode : "system";
    try {
      window.localStorage.setItem(storageKey, preference);
    } catch {
      // Theme preference remains available for this page even if storage is unavailable.
    }
    apply(preference);
  }

  function init() {
    document.querySelectorAll("[data-theme-option]").forEach((button) => {
      button.addEventListener("click", () => set(button.dataset.themeOption));
    });
    apply();
  }

  if (colorScheme) {
    const updateForSystemPreference = () => {
      if (savedMode() === "system") apply("system");
    };
    if (typeof colorScheme.addEventListener === "function") {
      colorScheme.addEventListener("change", updateForSystemPreference);
    } else if (typeof colorScheme.addListener === "function") {
      colorScheme.addListener(updateForSystemPreference);
    }
  }

  window.GpuWatcherTheme = { apply, init, set };
  apply();
})();
