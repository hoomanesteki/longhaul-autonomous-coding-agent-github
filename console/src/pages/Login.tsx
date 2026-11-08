import { type FormEvent, useState } from "react";
import { Navigate, useLocation, useNavigate } from "react-router-dom";

import { ApiError, tokenWorks } from "../api/client";
import { setToken } from "../api/token";
import { useToken } from "../api/useToken";

/** Where to go after signing in: the page that sent the visitor here, or the task list. */
function destination(state: unknown): string {
  if (typeof state === "object" && state !== null && "from" in state) {
    const { from } = state;
    // Only a path of this console. A stored address with a host in it would be an open redirect.
    if (typeof from === "string" && from.startsWith("/") && !from.startsWith("//")) return from;
  }
  return "/tasks";
}

function problemWith(error: unknown): string {
  if (error instanceof ApiError) return `The API answered ${error.status}: ${error.detail}`;
  return "Could not reach the API. Check that it is running, then try again.";
}

/**
 * Asks for the admin token (LONGHAUL_ADMIN_TOKEN) and keeps it only after the API accepts it,
 * so a typing mistake shows here and not as an empty page later.
 */
export default function Login() {
  const stored = useToken();
  const location = useLocation();
  const navigate = useNavigate();
  const [text, setText] = useState("");
  const [problem, setProblem] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const next = destination(location.state);

  if (stored !== null) return <Navigate to={next} replace />;

  async function submit(event: FormEvent): Promise<void> {
    event.preventDefault();
    const token = text.trim();
    if (token === "") {
      setProblem("Enter the admin token.");
      return;
    }
    setBusy(true);
    setProblem(null);
    try {
      if (await tokenWorks(token)) {
        setToken(token);
        void navigate(next, { replace: true });
      } else {
        setProblem("The API did not accept that token.");
      }
    } catch (error) {
      setProblem(problemWith(error));
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="flex min-h-screen items-center justify-center bg-white p-4 text-slate-900">
      <form onSubmit={(event) => void submit(event)} className="w-full max-w-sm space-y-4">
        <h1 className="text-lg font-semibold">Sign in</h1>
        <p className="text-sm text-slate-600">
          Enter the admin token. It is the value of LONGHAUL_ADMIN_TOKEN on the server.
        </p>
        <div>
          <label htmlFor="token" className="block text-sm font-medium">
            Admin token
          </label>
          <input
            id="token"
            type="password"
            autoComplete="current-password"
            autoFocus
            value={text}
            onChange={(event) => setText(event.target.value)}
            className="mt-1 w-full rounded border border-slate-300 px-3 py-2 text-sm"
          />
        </div>
        {problem !== null && (
          <p role="alert" className="text-sm text-red-700">
            {problem}
          </p>
        )}
        <button
          type="submit"
          disabled={busy}
          className="rounded bg-slate-800 px-4 py-2 text-sm text-white disabled:opacity-50"
        >
          Sign in
        </button>
      </form>
    </main>
  );
}
