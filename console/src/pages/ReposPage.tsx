import { useQuery } from "@tanstack/react-query";

import { listRepos } from "../api/client";

/** The repos Longhaul knows. It is the one page that already reads the API, as a check of the wiring. */
export default function ReposPage() {
  const { data, error, isPending } = useQuery({ queryKey: ["repos"], queryFn: listRepos });

  return (
    <section>
      <h1 className="text-lg font-semibold">Repos</h1>
      {isPending && <p className="mt-2 text-sm text-slate-600">Loading.</p>}
      {error !== null && (
        <p role="alert" className="mt-2 text-sm text-red-700">
          Could not load the repos: {error.message}
        </p>
      )}
      {data?.length === 0 && <p className="mt-2 text-sm text-slate-600">No repos yet.</p>}
      {data !== undefined && data.length > 0 && (
        <div className="mt-3 overflow-x-auto">
          <table className="w-full text-left text-sm">
            <thead className="border-b border-slate-300 text-slate-600">
              <tr>
                <th className="py-2 pr-4 font-medium">Repo</th>
                <th className="py-2 pr-4 font-medium">Allowlisted</th>
                <th className="py-2 font-medium">Tasks</th>
              </tr>
            </thead>
            <tbody>
              {data.map((repo) => (
                <tr key={repo.repo} className="border-b border-slate-100">
                  <td className="py-2 pr-4">{repo.repo}</td>
                  <td className="py-2 pr-4">{repo.allowlisted ? "yes" : "no"}</td>
                  <td className="py-2">{repo.tasks}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
