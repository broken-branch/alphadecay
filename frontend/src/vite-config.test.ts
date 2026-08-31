import { describe, expect, it } from "vitest";

import { developmentProxy } from "../devProxy.ts";

describe("development API routing", () => {
  it("proxies the product API and interactive reference to the backend", () => {
    expect(developmentProxy).toEqual({
      "/api": "http://127.0.0.1:8000",
      "/docs": "http://127.0.0.1:8000",
      "/openapi.json": "http://127.0.0.1:8000",
    });
  });
});
