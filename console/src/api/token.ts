/**
 * Where the console keeps the API bearer token.
 *
 * localStorage can throw (a private window, blocked site data) and can come back empty, so every
 * access is wrapped. When it fails the token lives in memory for the life of the page, which is
 * enough to keep working and costs only a new sign-in after a reload.
 */

const KEY = "longhaul.token";

let memory: string | null = null;

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
}

export function clearToken(): void {
  memory = null;
  try {
    window.localStorage.removeItem(KEY);
  } catch {
    // Nothing stored that could be removed.
  }
}
