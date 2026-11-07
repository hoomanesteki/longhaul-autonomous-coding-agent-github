# Architecture

Longhaul runs a coding agent as a Temporal workflow. The workflow is pure and
only schedules activities. Activities do the I/O: calling the model, running
tools in a sandbox, writing checkpoints and talking to GitHub.

All pieces share one module of types, `src/longhaul/contracts.py`. It has no
I/O and imports only the standard library and pydantic. Vendor shapes, such as
the Anthropic Messages API format, live in `src/longhaul/agent/render.py`.

## Contracts changelog

Every change to `contracts.py` gets an entry here, with the new
`contracts_version` and the reason. Changes also need a note in the PR.

| Version | Change |
|---|---|
| 1 | First version (LH-101). Records, messages and blocks, provider errors, task and plan types, carry state, and the protocols for providers, sandboxes, blobs, checkpoints, memory and GitHub. `Budgets` and `RepoSettings` moved here from `settings.py`. |
| 2 | Reviewer sign-off (LH-198). Stop reasons `refusal`, `pause_turn` and `model_context_window_exceeded`, with `LLMResponse.raw_stop_reason` and `LLMResponse.stop_details` (`StopDetails`). `CallOptions` (`effort`, `force_tool`, `output_schema`) and an `options` argument on `LLMProvider.complete()` and `count_tokens()`. `ToolSpec.strict`. `LLMResponse.request_id` and `LLMResponse.dropped_thinking_blocks`. `ProviderError.usage`, so a call that was billed and then failed can still be ledgered. `CarryState.started_at` must carry a timezone and is stored in UTC. Block `type` tags are written in every dump, also with `exclude_unset` and `exclude_defaults`. |

### Version 2 decisions

These are the choices behind the version 2 row. Two of them are things the contracts do not do: they carry no thinking blocks, and the system prompt stays a string.

- **Thinking blocks are not in the contracts.** The Anthropic provider (LH-104) drops `thinking` and `redacted_thinking` blocks when it parses a response, and counts them in `dropped_thinking_blocks`. Elision and compaction rewrite history, and a rewrite would invalidate any thinking blocks that were sent back with it. The cost is that the model loses its earlier reasoning on each turn. ADR-0003 records it. The live check S1 is meant to confirm that the API accepts the stripped loop. If it does not, the contracts gain thinking blocks then.
- **A forced tool choice is a request, not a promise.** `claude-sonnet-5-5` rejects a forced `tool_choice` with a 400. So `CallOptions.force_tool` may be refused: the Anthropic provider (LH-104) raises `INVALID_REQUEST` before any HTTP call, and the OpenAI provider maps it to a function `tool_choice`. A caller that needs JSON that parses uses `output_schema`, or a `strict` tool with `tool_choice` left on auto and a prompt instruction.
- **The system prompt stays one string.** It holds the static blocks 1 to 4 of the prompt layout and does not change for the life of a transcript, so its cache breakpoint sits at the end. The plan (block 5) and the progress notes (block 6) are text blocks of the first user message, so a change to either leaves the cached prefix alone. LH-405 settled this, and ADR-0013 gives the reasons and the four cache breakpoints.
- **Effort and output limits live in config.** `effort` per role and `loop.max_tokens_retry` are in `config/longhaul.example.yaml`. Effort reaches a provider through `CallOptions.effort` and stays fixed for the life of a transcript. The loop (LH-110) never stores or runs a turn that stopped on `max_tokens`. It retries that turn once at `max_tokens_retry`, and a second stop on `max_tokens` ends the task. The Anthropic provider streams the reply when `max_tokens` is above 16000, which includes the retry.

### Deferred contract changes

The review of `contracts.py` raised more than version 2 holds. Each of these waits for the story that first needs it, so the contracts change once and for a reason that is on the table.

| Change | Story | Why it waits |
|---|---|---|
| `CarryState` keeps the window of recent stuck signals | LH-505 | The detector does not exist yet. The window needs `TurnSignal` moved into the contracts from `roles/stuck.py`. The ref of the append-only log, which this row also named, is settled in LH-404 without a contracts change: the log is written under keys made from the task id and the turn (`context/log.py`), so `CarryState.transcript_ref` stays the ref of the view. ADR-0013 has the reasons. |
| `CheckpointStore` attaches the checkpoint volume to a sandbox, and `SandboxManager.create` can mount it | LH-205 | Settled in LH-205 (deferred item contracts-8) without a contracts change. `DockerSandboxManager.create` takes an optional `mounts` argument, a map of host folder to container path, and `GitCheckpointStore` has `mounts(task_id)` and `attach(sandbox, task_id)`. The `SandboxManager` and `CheckpointStore` protocols and `contracts_version` stay as they are, because only the Docker manager can mount a host folder and the worker builds both objects. Revisit if a second manager needs mounts. The reasons are in ADR-0006. |
| Bounds on `Plan`: 2 to 8 steps, unique step ids, a `done_when` that is not blank, at least one acceptance check | LH-501 | Settled in LH-501 without a contracts change. `roles/planner.py` checks them after pydantic has read the plan (`check_plan`), and a plan that breaks one gets the repair turn of F14. The input schema of `submit_plan` is `Plan.model_json_schema()` as it is, and the prompt and the tool description say the rules. Putting the bounds in the model would change the schema that every consumer of `Plan` shares. |
| `TaskStatus` shows the plan with the status of each step | LH-503 | Settled in LH-503 without a contracts change. `status()` answers with `acts.PlannedStatus`, a `TaskStatus` with one more field, `plan`, when the task has a plan. A client asks for it with `handle.query("status", result_type=PlannedStatus)`, because the SDK takes the result type from the method only when the query is asked for by method. A reader of `TaskStatus` ignores the extra field. A `plan` field on `TaskStatus` itself, with `None` as its default, is the change to make if every client should get the plan. |
| `MemoryItem.source_ref`, and a lookup by it | LH-606 | Promotion on a merged PR is the first code that reads it. Until then `PgMemoryStore.add_candidate` takes `source_ref` as a keyword (LH-603), which the protocol allows because the extra argument is optional. |
| A size limit on `TaskSpec.body` and `comments`, with a blob ref for the full text | LH-303 | Issue intake is the first code that writes long text. |
| `Message.role == "system"` | LH-405 | Decided together with the prompt layout. |
| `Usage.thinking_tokens` and 1 hour cache writes | LH-701 | Settled in LH-701 without a contracts change. Thinking is billed as output (ADR-0003, section 5), so the ledger prices `output_tokens` at the output rate and needs no separate count. Longhaul sends no `ttl` on its cache markers, so every write is a 5 minute write and one write price covers it. A change to 1 hour entries would need a second write price and a usage field for them. |

## Tracing

Each task is one OpenTelemetry trace. `src/longhaul/telemetry/otel.py` holds all of it, and the rest of the code calls five things: `setup_tracing(settings, telemetry)`, `interceptors()`, `detached()`, `chat_span(...)` and `tool_span(...)`.

The trace has the shape that Temporal's interceptor gives it, with the model and tool spans inside the activity spans.

```
StartWorkflow:TaskWorkflow            client call that starts the task
  RunWorkflow:TaskWorkflow
  StartActivity:llm_turn
    RunActivity:llm_turn
      chat <model>                    one provider call
  StartActivity:tool_exec
    RunActivity:tool_exec
      execute_tool <name>             one tool run
```

Temporal's workflow spans have no duration, because a workflow span that stayed open would not survive a replay. Durations show on the activity spans and on the model and tool spans inside them.

### Where it is wired

- `setup_tracing` builds a tracer provider with the resource `service.name=longhaul-worker` and an OTLP gRPC exporter to `OTEL_EXPORTER_OTLP_ENDPOINT`. Without that variable nothing is set up and every span is a no-op. A bare `host:port` counts as plain gRPC, like the collector in compose. The caller calls `shutdown()` on the returned provider when the process stops, which sends the spans still queued.
- `durable.client.connect` puts `interceptors()` on the client when tracing is set up. A worker takes the interceptors of its client, so the worker needs no list of its own. Passing the interceptor to both would trace every call twice.
- The worker module (LH-208) calls `setup_tracing(settings, config.telemetry)` before it calls `connect`, and `provider.shutdown()` when it exits.
- `durable.client.start_task` starts the workflow inside `detached()`, a block with no current span, so each task opens a trace of its own. The poller (LH-307) calls `start_task` from the activity `poll_once`. Without this, every task of one poll would become a child of `RunActivity:poll_once` and share its trace id. Code that starts a task, the poller and the merge step included, goes through `start_task` for this reason.
- A workflow started outside `start_task`, from the `temporal` CLI or a Schedule, carries no trace header. Temporal's interceptor then creates no workflow spans, and each `RunActivity` span becomes its own root trace.
- `llm_turn` wraps the provider call in `chat_span`, and `tool_exec` wraps the tool run in `tool_span`.

### Attribute names

The names come from `opentelemetry-semantic-conventions` 0.66b2 (module `opentelemetry.semconv._incubating.attributes.gen_ai_attributes`), used with `opentelemetry-api`, `opentelemetry-sdk` and `opentelemetry-exporter-otlp-proto-grpc` 1.45.2. In that release every `gen_ai.*` constant is marked as moved to the OpenTelemetry GenAI conventions repository. The names are unchanged, and `gen_ai.system` is replaced by `gen_ai.provider.name`. Check them again when the package is upgraded (ADR-0002).

| Span | Attribute | Value |
|---|---|---|
| `chat <model>` | `gen_ai.operation.name` | `chat` |
| | `gen_ai.provider.name` | the provider's name: `anthropic`, `openai`, `scripted` or `replay` |
| | `gen_ai.request.model` | the model the config names for the role |
| | `gen_ai.response.model` | the model the provider says answered |
| | `gen_ai.usage.input_tokens` | input, cache read and cache write tokens added up |
| | `gen_ai.usage.output_tokens` | output tokens |
| | `gen_ai.usage.cache_read.input_tokens` | tokens read from the prompt cache |
| | `gen_ai.usage.cache_creation.input_tokens` | tokens written to the prompt cache |
| | `gen_ai.response.finish_reasons` | the provider's own stop reason, as a list of one |
| | `error.type` | the provider error kind, or the exception class, when the call failed |
| `execute_tool <name>` | `gen_ai.operation.name` | `execute_tool` |
| | `gen_ai.tool.name` | the tool's name |
| | `gen_ai.tool.call.id` | the provider's tool_use id |
| | `gen_ai.tool.type` | `function` |
| | `error.type` | `tool_error` when the tool reported failure, or the exception class |
| both | `longhaul.task_id` | the task id |
| | `longhaul.turn_index` | the turn |
| `chat <model>` only | `longhaul.role` | planner, coder, verifier or compactor |
| | `longhaul.cost_usd` | 0.0 until the pricing table arrives (LH-701), as in `LlmTurnOutput` |
| | `longhaul.cache_ratio` | `cache_read / (input + cache_read)`, or 0.0 when both are 0 |

`longhaul.role` is not on tool spans, because a `tool_exec` call does not carry the role of the turn that made it.

### Content

Prompts hold code and may hold secrets, so content capture is off. `telemetry.capture_content` in the config turns it on. While it is off, no prompt, tool argument or tool output text goes into an attribute or an event, and a failed span keeps only an error type, never the text of the exception, because a provider error can echo the request. Token counts, timings, names and ids stay.

The spans of Temporal's interceptor (`RunActivity`, `CompleteWorkflow` and the rest) record the message and stacktrace of a failed activity or workflow by themselves, and set "Type: message" as the status description. While capture is off, `interceptors()` hands the interceptor a tracer that keeps `error.type` and drops the rest: no status description and no `exception` event. With capture on, those spans keep Temporal's own detail.

With `capture_content: true`, these attributes are added, each cut at 16,000 characters:

| Span | Attribute |
|---|---|
| `chat <model>` | `gen_ai.system_instructions`, `gen_ai.input.messages`, `gen_ai.output.messages` |
| `execute_tool <name>` | `gen_ai.tool.call.arguments`, `gen_ai.tool.call.result` |

`tests/integration/test_tracing.py` checks both settings: it puts a marker into the issue text, a tool's arguments, a tool's output and the model's reply, and looks for each marker in every attribute and event.

## Logs

Every log line that Python code in the worker writes is one JSON object on stderr, with one exception named below. `src/longhaul/telemetry/logging.py` holds all of it, and the rest of the code calls three things: `setup_logging()`, `interceptors()` and `redact(text)`.

```json
{"timestamp": "2026-11-04T13:22:06.955917Z", "level": "info", "logger": "longhaul.durable.activities.llm", "event": "calling the model", "task_id": "lh-acme-calc-42", "turn_index": 3, "activity": "llm_turn", "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736"}
```

`timestamp` (UTC), `level`, `logger` and `event` are on every line, and the fields of the call follow them. The rest are there when they apply.

| Field | Where it comes from |
|---|---|
| `task_id`, `turn_index` | The input of the running activity. `interceptors()` reads them from the model the activity was called with, so an activity whose input has no `task_id` (the poller) logs without one. |
| `activity` | `temporalio.activity.info().activity_type`, bound by the same interceptor. |
| `trace_id` | The current OpenTelemetry span, read when the line is written. It is the id that Jaeger shows for the `RunActivity` span. `interceptors()` also binds the id of that span for the rest of the activity, because temporalio writes "Completing activity as failed" after the span has closed. An open span wins over the bound id. A line with no open span and no bound id has none. |

### Where it is wired

- `setup_logging()` configures structlog and replaces the handlers of the root logger with one that writes JSON. Standard library records, from the modules of this repo (`logging.getLogger(__name__)`) and from temporalio, go through the same processors as structlog events, so they get the same fields and the same redaction. Calling it twice does not write a line twice.
- `interceptors()` is a Temporal client and worker interceptor. `durable.client.connect` registers it on the client after `otel.interceptors()`, so the `RunActivity` span is open when it binds the trace id, and the worker takes it from there. Registering it on the worker as well would run it twice. Each activity runs in a task of its own, so the fields stay on the lines of that activity only, including the "Completing activity as failed" warning that temporalio writes after the activity raised.
- The worker module (LH-208) is built next to this story. The change that brings it in calls `setup_logging()` first, in place of `logging.basicConfig`, before `setup_tracing` and `connect`. The `connect` it uses already registers `interceptors()`.
- Temporal Core, the Rust library under the SDK, is the exception to "one JSON object per line". It writes its own WARN and ERROR lines to stderr in a colored compact format, such as the warning about a server older than 1.16 or a lost connection, and those lines skip the JSON renderer and the redaction. A log shipper that parses every stderr line as JSON has to tolerate them. To bring them in, build the worker's `Runtime` with `LoggingConfig(filter=..., forwarding=LogForwardingConfig(logger=logging.getLogger("temporalio.core")))`, and they go through the same processors as every other record.
- The lines that temporalio writes outside an activity, such as "Beginning worker shutdown", carry none of the four fields.

### Redaction

`redact(text)` replaces these with `[redacted]`:

| Pattern | What it catches |
|---|---|
| `sk-[A-Za-z0-9_-]{20,}` | OpenAI and Anthropic keys. It must start a word, so `lh-acme-my-task-tracker-pro-edition-5` is left alone. A backslash and `n`, `r` or `t` before it counts as a line break, so a key in a repr, a `%r` argument or JSON text is caught. |
| `ghp_[A-Za-z0-9]{30,}` | GitHub personal access tokens |
| `github_pat_[A-Za-z0-9_]{30,}` | Fine-grained GitHub tokens |
| `AKIA[0-9A-Z]{16}` | AWS access key ids |
| `-----BEGIN [A-Z ]*PRIVATE KEY-----` | A PEM private key, from the header through its END line. A key that was cut off before its END line is redacted to the end of the text. |

The logging pipeline applies it last, just before the JSON is written, to the event text, every field (inside dicts and lists too), the formatted exception, and the repr of any value that JSON cannot encode. A secret in a traceback is caught as well as one in a field. The run report (LH-705) and the console view of stored tool outputs (Phase 8) call `redact` on their own text.

Redaction matches patterns, so it does not catch a secret of another shape, such as a password in a connection string. Keep those out of log calls.

`tests/unit/test_redaction.py` has a case for each pattern and for text that must stay as it is. The fake secrets in the tests are put together at run time, because CI runs gitleaks over the whole history.

## Metrics

The worker serves Prometheus metrics, and `docker/grafana/dashboards/longhaul.json` draws them. `src/longhaul/telemetry/metrics.py` holds all of it, and the metric list, the labels and the operating notes are in "Metrics and the dashboard" in `docs/runbook.md`. This section gives the design choices.

**Only activities update metrics.** A workflow runs again on every replay, so a counter bumped in workflow code would count a turn once for each replay. Each activity updates the metrics of the thing it does: `llm_turn` the turns, the cost, the errors and the cache ratio, `tool_exec` the tool durations, `ensure_sandbox` the rebuilds, and `prepare_workspace` the start of a task. An interceptor on the worker (`RetryCounter`) counts the attempts after the first of any activity, from the attempt number that Temporal sets.

**The end of a task goes through `record_event`.** The workflow cannot count a finished task, so when `run()` ends it calls the `record_event` local activity once with the type `outcome`. That also leaves a row in the `events` table with the kind of the outcome, the repo, the turns and the cost. The worker counts the task from that call, before it writes the row, so a task that ends while Postgres is down is still counted. A hand-over to a new run is not an end and makes no call. The count is made once for each workflow run id, because a workflow task that is retried runs the local activity again; a replay does not, since Temporal keeps its result in the history. Events that later stories send (`compaction` from LH-404, `stuck` from LH-505) move their counters through the same activity.

**An event is counted after its row is written.** The metric then follows the events table: a write that failed is not counted, and the retry that writes it is.

**Labels come from fixed sets.** Each label value is a series that Prometheus keeps. Roles, outcomes, provider error kinds, stuck reasons, tool names of the registry and activity names are fixed. Repos are the allowlisted ones, and models come from the config. A tool name the model made up, a stuck reason nobody defined and every `local:<path>` repo become one label each (`unknown`, `other` and `local`).

**The registry belongs to the object.** `Metrics` has a `CollectorRegistry` of its own, and the process uses the one in `metrics.current()`. A test makes a fresh `Metrics` and reads only what it made. The Prometheus global registry would carry counts from one test to the next.
