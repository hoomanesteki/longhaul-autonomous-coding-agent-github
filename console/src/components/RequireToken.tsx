import { Navigate, Outlet, useLocation } from "react-router-dom";

import { useToken } from "../api/useToken";

/**
 * The gate in front of every page but the login. Without a token the visitor goes to the login
 * screen, which sends them back to the page they asked for. A token the API later refuses is
 * cleared by the client (api/client.ts), which lands here the same way.
 */
export default function RequireToken() {
  const token = useToken();
  const location = useLocation();

  if (token === null) {
    return <Navigate to="/login" replace state={{ from: location.pathname + location.search }} />;
  }
  return <Outlet />;
}
