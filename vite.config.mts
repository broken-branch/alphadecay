import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

import { developmentProxy } from "./frontend/devProxy.ts";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: developmentProxy,
  },
});
