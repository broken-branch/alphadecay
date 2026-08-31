const backend = "http://127.0.0.1:8000";

export const developmentProxy = {
  "/api": backend,
  "/docs": backend,
  "/openapi.json": backend,
};
