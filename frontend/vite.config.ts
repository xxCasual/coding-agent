import { defineConfig, type UserConfig } from "vite";
import type { InlineConfig } from "vitest/node";
import react from "@vitejs/plugin-react";

const config: UserConfig & { test: InlineConfig } = {
  root: ".",
  base: "/",
  plugins: [react()],
  build: {
    outDir: "../src/review_agent/web",
    emptyOutDir: true,
    sourcemap: false,
    rollupOptions: {
      output: {
        entryFileNames: "static/app.js",
        chunkFileNames: "static/[name].js",
        assetFileNames: (assetInfo) => {
          if (assetInfo.name?.endsWith(".css")) {
            return "static/styles.css";
          }
          return "static/[name][extname]";
        },
      },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    css: true,
  },
};

export default defineConfig(config);
