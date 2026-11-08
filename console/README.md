# Longhaul console

The operator's view of Longhaul: tasks, timelines, actions and memory. It reads the console API (`longhaul api`) and nothing else. The stack and the reasons are in `docs/adr/0011-console-stack.md`, and the commands are in "The console" in `docs/runbook.md`.

```bash
npm ci
npm run dev        # needs `longhaul api` on http://127.0.0.1:8000
npm run lint && npm run typecheck && npm test && npm run build
```

`src/api/schema.ts` is generated from `openapi.json` by `make api-types` at the top of the repo. Do not edit it.

The console asks for `LONGHAUL_ADMIN_TOKEN` at the login screen and sends it as a bearer token with every call. `npm run dev` proxies `/api` to the API, so start `longhaul api` with a token set.
