import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

const backendPort = process.env.ORCHESTRATOR_BACKEND_PORT ?? "18100";

export default defineConfig({
  plugins: [vue()],
  server: {
    host: "127.0.0.1",
    port: 8100,
    strictPort: true,
    proxy: {
      "/api": {
        target: `http://127.0.0.1:${backendPort}`,
        changeOrigin: true,
      },
    },
  },
});
