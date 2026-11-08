import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { clearToken, getToken, setToken, subscribeToken } from "../api/token";

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

describe("token changes", () => {
  it("tells a subscriber when the token is set and when it is cleared", () => {
    const seen = vi.fn();
    const unsubscribe = subscribeToken(seen);

    setToken("abc");
    clearToken();
    unsubscribe();
    setToken("def");

    expect(seen).toHaveBeenCalledTimes(2);
  });

  it("tells a subscriber when another tab changes the stored token", () => {
    const seen = vi.fn();
    const unsubscribe = subscribeToken(seen);

    window.dispatchEvent(new StorageEvent("storage", { key: "longhaul.token" }));
    window.dispatchEvent(new StorageEvent("storage", { key: "something.else" }));
    unsubscribe();

    expect(seen).toHaveBeenCalledTimes(1);
  });
});
