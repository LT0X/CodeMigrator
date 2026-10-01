import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const apiTarget = process.env.CODEMIGRATOR_WEB_API_TARGET;

export default defineConfig({
  plugins: [react()],
  server: apiTarget
    ? {
        proxy: {
          "/api/v1": {
            target: apiTarget,
            changeOrigin: false,
          },
        },
      }
    : undefined,
  test: {
    environment: "jsdom",
    include: ["src/**/*.test.ts", "src/**/*.test.tsx"],
  },
});
