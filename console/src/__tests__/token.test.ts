import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { clearToken, getToken, setToken } from "../api/token";

beforeEach(() => {
  clearToken();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("token storage", () => {
  it("keeps the token in localStorage", () => {
    setToken("abc");

    expect(getToken()).toBe("abc");
    expect(window.localStorage.getItem("longhaul.token")).toBe("abc");
  });

  it("falls back to memory when localStorage throws", () => {
    // A private window or blocked site data makes every access throw.
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new DOMException("blocked", "SecurityError");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("blocked", "SecurityError");
    });
    vi.spyOn(Storage.prototype, "removeItem").mockImplementation(() => {
      throw new DOMException("blocked", "SecurityError");
    });

    setToken("abc");
    expect(getToken()).toBe("abc");

    clearToken();
    expect(getToken()).toBeNull();
  });

  it("gives null when nothing is stored", () => {
    expect(getToken()).toBeNull();
  });
});
