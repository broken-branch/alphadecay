import { HealthResponseSchema, type HealthResponse } from "../contracts/v1";

export async function loadRuntimeStatus(): Promise<HealthResponse> {
  const response = await fetch("/api/health", {
    method: "GET",
    headers: { Accept: "application/json" },
    credentials: "omit",
  });
  if (!response.ok) throw new Error("RUNTIME_STATUS_UNAVAILABLE");
  return HealthResponseSchema.parse(await response.json());
}
