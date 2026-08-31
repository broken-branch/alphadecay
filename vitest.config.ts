import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    include: ["frontend/src/**/*.test.{ts,tsx}"],
    setupFiles: ["./frontend/src/test/setup.ts"],
  },
});
