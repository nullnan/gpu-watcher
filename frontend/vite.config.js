import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  plugins: [vue(), tailwindcss()],
  base: "/static/",
  build: {
    outDir: "../gpu_watcher/web",
    emptyOutDir: false,
    cssCodeSplit: false,
    rollupOptions: {
      input: { app: "src/main.js", login: "src/login.js" },
      output: {
        entryFileNames: "[name].js",
        chunkFileNames: "vendor/ui-[name].js",
        assetFileNames: ({ names }) =>
          names?.some((name) => name.endsWith(".css"))
            ? "styles.css"
            : "vendor/[name][extname]",
      },
    },
  },
});
