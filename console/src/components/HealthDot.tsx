import { useQuery } from "@tanstack/react-query";

import { healthz } from "../api/client";

const POLL_MS = 15_000;

/**
 * A dot and a word for whether the API answers `/healthz`. The probe covers the database only,
 * so a green dot says the lists and timelines will load, not that Temporal is up.
 */
export default function HealthDot() {
  const { isSuccess, isPending } = useQuery({
    queryKey: ["healthz"],
    queryFn: healthz,
    refetchInterval: POLL_MS,
  });
  const label = isPending ? "Checking API" : isSuccess ? "API up" : "API down";
  const color = isPending ? "bg-slate-400" : isSuccess ? "bg-green-600" : "bg-red-600";
  return (
    <span className="flex items-center gap-2 text-sm text-slate-700" role="status">
      <span aria-hidden="true" className={`inline-block h-2.5 w-2.5 rounded-full ${color}`} />
      {label}
    </span>
  );
}
