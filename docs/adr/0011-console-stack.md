# ADR-0011: Console stack

- Status: Accepted
- Date: 2026-11-08

## Context

The console (Phase 8) lets a person see every task, read its timeline, steer it and review memory candidates, without opening GitHub or the Temporal UI. It is a small web app over three things the agent already keeps: the Postgres tables, the blob store and the workflows in Temporal.

Four facts shape the stack:

- The backend and the frontend are built in different stories (LH-801 and LH-802 to LH-808). If the API changes shape, the console must fail at build time. A hand-written client fails when a person opens the page.
- The timeline is the main view. A task runs for hours and writes hundreds of rows: events, model calls and tool calls. The view has to expand a turn, fetch a long tool output one page at a time, and follow a running task. That is enough interaction to justify a component framework.
- The console can change a running task, and it shows model text and tool output. The first can only go through the workflow's signals, and the second can hold secrets.
- Temporal can be down while the database is up. A person should still be able to read what happened.

## Decision

**A FastAPI app in `longhaul.api`, and a React, Vite and TypeScript console in `console/`.** The console talks to the API and to nothing else. It never reads the database or calls Temporal.

**Types are generated.** `make api-types` writes the OpenAPI schema of the app to `console/openapi.json`, and the console builds its client from that file (LH-802). The file is committed, and a unit test fails when it differs from the app, so a change to a response model cannot reach the repo without the schema that the console builds from. Operation ids are the names of the route functions (`list_tasks`, `approve_plan`), so the client has readable names and not the default `list_tasks_api_tasks_get`.

**The API reads the database and the blobs, and writes through the workflow.**

| What | How |
|---|---|
| Lists, timelines, plans, memory, repos | SQL on the tables of plan section 3.5, through SQLAlchemy sessions |
| Turn requests and replies, tool outputs | The blob store, by the key the row holds |
| Live status of a task | A `status` query to its workflow, through `durable/client.py`, with a short deadline |
| Approve, reject, hint, budget, cancel | A signal, through `durable/client.py`, sent with `by="console"` |
| Approve and reject a memory item | `PgMemoryStore.promote` and `reject`, which keep the rules of ADR-0010 |

The API sends only the signals that a person can send, and it sends them under the name `console`. The workflow records the name: a hint reaches the model as `console: <text>`, and a cancel ends with "cancelled by console". For a task of an issue the workflow also repeats a hint, a budget top-up and a plan decision from the console as a short comment on the issue (LH-805), so the issue shows every steer. The API starts no workflow and changes no state of one in any other way. A task therefore behaves the same whether it was steered from the console, an issue comment or the CLI.

**The page works without Temporal.** The detail of a task has a `workflow` field (`running`, `closed`, `not_found` or `unreachable`) and a `live` status that is empty when nobody can answer. The list, the timeline, the turns and the memory pages come from the database alone. The actions answer 503 when Temporal is down, 404 for a task that has no workflow and 409 for one that has ended. `/healthz` checks the database only, for the same reason.

**Everything a model or a tool wrote is redacted when it is read.** Turn requests and replies, tool outputs, event payloads, task settings and memory notes pass through `telemetry.logging.redact`, the function the logs and the run report use, so one function decides what a secret is. The blob store keeps what the model saw, because a replay needs it. A tool output is redacted whole before it is cut into a page, so a secret cannot be split by the edge of a page.

**Lists use keyset pagination.** A cursor holds the sort key of the last row of the page. A running task writes rows between two requests, and an offset would skip or repeat some. The timeline merges the events, turns and tool calls in the database, ordered by time, then by kind, then by a key that is unique inside the kind, so a page can end in the middle of a tie.

**The console is served by the API in a deployment.** With `LONGHAUL_CONSOLE_DIR` set to the build folder, the API also serves the console's files at `/`, and any path that is not a file gets `index.html` so that a reload of a page works. Unknown paths under `/api` stay 404. The compose `full` profile builds both in one image (`docker/api/Dockerfile`), which gives the browser one origin and the console no base URL. In development Vite serves the page and proxies `/api` to the API.

**`longhaul api` serves the app with uvicorn.** The address comes from `LONGHAUL_API_HOST` (127.0.0.1 by default) and `LONGHAUL_API_PORT` (8000). The app builds its stores when the server starts. Every `/api` route needs the bearer token of `LONGHAUL_ADMIN_TOKEN` (LH-807), and the default address keeps `/metrics` open to the machine it runs on and no one else.

## Consequences

**Easier**

- The console cannot drift from the API without a build error, and the API cannot drift from the file without a test failure.
- One redaction function covers the logs, the report and the console.
- The API can run next to a stopped worker or a stopped Temporal and still show what happened.

**Harder**

- The `tasks` row can lag behind the task: only the poller writes it today, so `status` and `phase` stay at `active` and `queued` until a later story updates them. The detail page shows the live status beside the row for that reason. The list shows the row, and its `status` filter filters on the row.
- The timeline orders tool calls by the time their row was written, which is when the call ended. A tool call that took a minute appears a minute after the model call that asked for it. The duration is in the row.
- The API imports `durable.client`, which imports the workflow class so that signals and queries are typed. It runs no workflow code.
- The line numbers of a tool output page count the redacted text. They match the numbers the model saw unless a private key block was collapsed into one line.

**To watch**

- A transcript can be large, and the turn endpoint returns it whole. If the console feels slow on a long task, the answer is a page of messages and not a bigger server.
- Authentication is one shared token (LH-807), not accounts. Everyone with the token can do everything, and the log does not say who signed a hint (`by` is `console`). The rate limit of 30 actions a minute is counted in the memory of one API process; a second process would need a shared counter. The token travels in a header, so the API needs TLS outside a trusted network.

## Alternatives

- **Server-rendered pages (Jinja and htmx).** Fewer parts, and no build step. The timeline needs expand and collapse, paging inside a page, and following a running task, and that is where it stops being simple. We also know React, and a typed client is the cheapest way to keep two stories honest.
- **The console reads Postgres and Temporal itself.** It would put credentials in the browser or a second backend in the console. It would also make every redaction rule a rule of the frontend.
- **GraphQL.** The console needs about a dozen fixed views. A schema language, a resolver layer and a client cache buy flexibility it does not use.
- **tRPC or a hand-written client.** Both give types, and neither generates them from the Python models that define the data.
- **Offset pagination.** It is simpler to write and wrong for a table that a running task is writing to.
- **A health check that includes Temporal.** It would report the whole API as down for an outage that breaks only the live status and the actions.
