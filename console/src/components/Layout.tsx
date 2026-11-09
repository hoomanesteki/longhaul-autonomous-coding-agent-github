import { useQueryClient } from "@tanstack/react-query";
import { NavLink, Outlet } from "react-router-dom";

import { clearToken } from "../api/token";
import HealthDot from "./HealthDot";

const LINKS = [
  { to: "/tasks", label: "Tasks" },
  { to: "/memory", label: "Memory" },
  { to: "/repos", label: "Repos" },
];

function linkClass({ isActive }: { isActive: boolean }): string {
  const base = "block rounded px-3 py-2 text-sm";
  return isActive
    ? `${base} bg-slate-200 font-medium text-slate-900`
    : `${base} text-slate-700 hover:bg-slate-100`;
}

/** The frame of every page: the nav on the left, the API health in the header, the page. */
export default function Layout() {
  const queryClient = useQueryClient();

  function signOut(): void {
    // The pages cached under this token must not show to whoever signs in next.
    clearToken();
    queryClient.clear();
  }

  return (
    <div className="flex min-h-screen flex-col bg-white text-slate-900">
      <header className="flex items-center justify-between border-b border-slate-200 px-4 py-3">
        <span className="font-semibold">Longhaul console</span>
        <div className="flex items-center gap-4">
          <HealthDot />
          <button
            type="button"
            onClick={signOut}
            className="rounded px-2 py-1 text-sm text-slate-700 hover:bg-slate-100"
          >
            Sign out
          </button>
        </div>
      </header>
      <div className="flex flex-1 flex-col sm:flex-row">
        <nav
          aria-label="Main"
          className="flex gap-1 border-b border-slate-200 p-2 sm:w-44 sm:flex-col sm:border-r sm:border-b-0"
        >
          {LINKS.map(({ to, label }) => (
            <NavLink key={to} to={to} className={linkClass}>
              {label}
            </NavLink>
          ))}
        </nav>
        <main className="min-w-0 flex-1 p-4">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
