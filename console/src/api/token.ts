/**
 * Where the console keeps the API bearer token.
 *
 * localStorage can throw (a private window, blocked site data) and can come back empty, so every
 * access is wrapped. When it fails the token lives in memory for the life of the page, which is
 * enough to keep working and costs only a new sign-in after a reload.
 *
 * Pages that depend on the token (the login gate, the sign-out button) subscribe to changes, so
 * a token cleared by a 401 reply or by another tab sends them back to the login screen.
 */

const KEY = "longhaul.token";

let memory: string | null = null;

const listeners = new Set<() => void>();

function notify(): void {
  for (const listener of listeners) listener();
}

/** Call `listener` whenever the token changes, here or in another tab. Returns the way to stop. */
export function subscribeToken(listener: () => void): () => void {
  listeners.add(listener);
  // The storage event fires in the other tabs of the same origin, never in the tab that wrote.
  const onStorage = (event: StorageEvent): void => {
    if (event.key === KEY) listener();
  };
  window.addEventListener("storage", onStorage);
  return () => {
    listeners.delete(listener);
    window.removeEventListener("storage", onStorage);
  };
}

export function getToken(): string | null {
  try {
    const stored = window.localStorage.getItem(KEY);
    if (stored !== null) return stored;
  } catch {
    // Fall through to the copy in memory.
  }
  return memory;
}

export function setToken(token: string): void {
  memory = token;
  try {
    window.localStorage.setItem(KEY, token);
  } catch {
    // The copy in memory is the only one.
  }
  notify();
}

export function clearToken(): void {
  memory = null;
  try {
    window.localStorage.removeItem(KEY);
  } catch {
    // Nothing stored that could be removed.
  }
  notify();
}
