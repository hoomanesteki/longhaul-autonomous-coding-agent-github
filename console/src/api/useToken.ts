import { useSyncExternalStore } from "react";

import { getToken, subscribeToken } from "./token";

/** The stored token, or null. The component renders again when it is set or cleared. */
export function useToken(): string | null {
  return useSyncExternalStore(subscribeToken, getToken);
}
