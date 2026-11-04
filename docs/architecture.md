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
- **The system prompt stays one string.** It holds the static blocks 1 to 4 of the prompt layout and does not change for the life of a transcript, so its cache breakpoint sits at the end. Changes to the plan and the notes are added as messages.
- **Effort and output limits live in config.** `effort` per role and `loop.max_tokens_retry` are in `config/longhaul.example.yaml`. Effort reaches a provider through `CallOptions.effort` and stays fixed for the life of a transcript. The loop (LH-110) never stores or runs a turn that stopped on `max_tokens`. It retries that turn once at `max_tokens_retry`, and a second stop on `max_tokens` ends the task. The Anthropic provider streams the reply when `max_tokens` is above 16000, which includes the retry.

### Deferred contract changes

The review of `contracts.py` raised more than version 2 holds. Each of these waits for the story that first needs it, so the contracts change once and for a reason that is on the table.

| Change | Story | Why it waits |
|---|---|---|
| `CarryState` keeps the ref of the append-only log blob and the window of recent stuck signals | LH-404, LH-505 | The log and the detector do not exist yet. The window needs `TurnSignal` moved into the contracts from `roles/stuck.py`. |
| `CheckpointStore` attaches the checkpoint volume to a sandbox, and `SandboxManager.create` can mount it | LH-205 | Settled in LH-205 (deferred item contracts-8) without a contracts change. `DockerSandboxManager.create` takes an optional `mounts` argument, a map of host folder to container path, and `GitCheckpointStore` has `mounts(task_id)` and `attach(sandbox, task_id)`. The `SandboxManager` and `CheckpointStore` protocols and `contracts_version` stay as they are, because only the Docker manager can mount a host folder and the worker builds both objects. Revisit if a second manager needs mounts. The reasons are in ADR-0006. |
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
