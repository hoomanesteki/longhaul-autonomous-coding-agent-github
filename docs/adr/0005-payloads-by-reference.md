# ADR-0005: Payloads by reference

- Status: Accepted
- Date: 2026-10-30

## Context

Temporal writes every activity input and output into the workflow's event history, and replays that whole history whenever a worker has to rebuild the workflow's state: after a crash, a restart, or a cache eviction. Anything that crosses an activity boundary is paid for again on every replay.

A coding agent moves large things across that boundary. The transcript grows with every turn. Tool arguments carry whole files. Tool outputs carry test logs. Diffs and git bundles can be megabytes.

The numbers show why this matters:

- A transcript of 150 KB sent into and out of an activity adds 300 KB to the history on each turn. Over the 150 turns a task may run, that is about 45 MB, close to the server's default history limit of 50 MB, before any other event is counted.
- The server's defaults also reject a single payload of about 2 MB, and terminate a workflow whose history passes 51,200 events or 50 MB.
- Long before those limits, replay time grows with history size, so recovery after a worker crash gets slower exactly when a task has run longest.

## Decision

**Pass references, not data.** Transcripts, tool arguments and outputs, diffs and bundles are written to the blob store. Workflows and activities pass a `BlobRef` (key, sha256, size) and small summaries. Anything bigger than 16 KB that crosses an activity boundary becomes a `BlobRef` (rule 5 in `CONTRIBUTING.md`).

**Cap every payload at 256 KB.** No activity input or output may serialize to more than 256,000 bytes of JSON (invariant I2). That is about eight times below the server's payload limit, which leaves room for summaries to grow. `assert_small` in `longhaul.durable.payload_guard` measures the JSON size of a pydantic model or plain value and raises `PayloadTooLargeError` with the size and the limit. Activity wrappers call it on inputs and outputs, so a mistake fails in the activity that made it, with a message that says what to do.

**Refuse an oversized payload where it is made (LH-292).** `assert_small` runs inside the activity, after the workflow has scheduled the input and Temporal has written it into the history. A 300 KB issue body therefore sat in the history twice, as the workflow input and as the input of `prepare_workspace`, before the guard fired. `GuardedPayloadConverter` in `longhaul.durable.payload_converter` is the pydantic payload converter with one change: `to_payloads` raises `PayloadTooLargeError` when any single payload is over 256,000 bytes, the same limit as `assert_small`. `guarded_data_converter` is what `durable.client.connect` gives every client, and a worker takes its converter from its client. Only encoding is checked. A payload that comes back from the server is decoded as it is, so a history written before the guard can still be replayed.

The hook is the payload converter, because every encode of the Python SDK (temporalio 1.35) goes through it: the input of a workflow, a signal, a query and an update on the client; the input of an activity, the arguments of a Continue-As-New, a memo and the result of a workflow inside the workflow; the result of an activity in the worker; and the details of a failure. A `PayloadCodec` sees the same bytes, but the worker runs it over the finished completion of a workflow task, outside the workflow code. An error there can only fail the workflow task, and the server retries that task for as long as the workflow is open.

What happens when each path meets an oversized payload:

| Path | Where the error is raised | Result |
|---|---|---|
| Start, signal or query from a client | In the calling process, before the request | The call raises `PayloadTooLargeError`. Nothing is sent. |
| Activity input built by the workflow | In the workflow code, at the call that schedules the activity | **The workflow fails** with the error. The activity is not scheduled and the payload is not written. |
| Hand-over (Continue-As-New) | `TaskWorkflow` converts its state once before it calls `continue_as_new` | **The workflow fails** in the same way. |
| Result of an activity | In the worker that ran the activity | The activity fails with `PayloadTooLargeError`, which the retry policies do not retry. |

The workflow fails, and not only its task, because retrying the task cannot help. The task would replay the same code over the same history and raise the same error, and the run would stay open and silent. `acts.FAIL_THE_WORKFLOW` lists the error, `TaskWorkflow` passes it as `failure_exception_types`, and `longhaul run` then prints the size, the limit and the advice to use a `BlobRef`. This matches what already happens to an activity that fails for good. The hand-over needs its own check because the SDK converts the arguments of a Continue-As-New after the workflow code has let go of them. An error from there is dropped, the task completes without a command, and the workflow stays open and does nothing. A test shows it: without the check, the run times out.

**`longhaul run` cuts the issue file.** The guard is the last line, and a message about a `BlobRef` is no help to a person who passed a long file. `read_issue` cuts the body of `--issue-file` to `MAX_BODY_BYTES` with the function that `intake.py` uses for a GitHub issue (`clip_text`), so a local task and a GitHub task obey one limit. It prints a note on stderr, and the text that the model reads ends with a note that says the rest is in the file.

**Continue-As-New every 40 turns.** Small payloads still add up, because every turn adds activities and each activity adds several history events. The workflow continues as new every 40 turns, or earlier when Temporal suggests it. The new run starts from `CarryState`, which holds refs and counters, not data. LH-207 measured the stub run: a turn with one tool call adds 18 history events and a turn with three calls adds 30, so 40 turns come to 720 to 1,200 events. That is under a third of the 4,096 at which the server suggests a hand-over, so 40 stays. The setting is `loop.continue_as_new_every_turns`, and the workflow carries a copy of it in `acts.CONTINUE_AS_NEW_EVERY_TURNS`.

**Verify every read.** `put` hashes the bytes with sha256 and returns the `BlobRef`. `get` checks the size and the hash before it returns any bytes, and raises `BlobIntegrityError` if either differs. `S3BlobStore` also sends the hash with the upload, so the server rejects a write that was damaged in transit instead of leaving a ref that can never be read. The size is checked from the response headers before the body is read, so a replaced object much larger than the ref says is never loaded into memory.

**Keys.** `blob_key(task_id, kind, turn_index, name)` builds `tasks/<task_id>/<kind>/<turn:04d>/<name>`, for example `tasks/lh-x/turns/0007/response.json`. Each part may hold letters, digits, dot, underscore and hyphen, must start with a letter or digit, and may not contain `..`. Every key passed to `put` or `get` is also checked: at most 1,024 bytes (the S3 limit), no leading or trailing slash, no empty or `.` segment, no `..`, and no control characters or backslash.

A key can be written again. A retried activity writes the same key, and the ref it returns names the content of the attempt that completed. A ref from an earlier attempt then fails verification instead of silently returning newer content.

**Client library: boto3 through `asyncio.to_thread`.** `S3BlobStore` holds one boto3 client, which is thread-safe, and runs each call, including the hashing, on a worker thread. The reasons:

- The dependency set does not resolve with aioboto3. The project requires `boto3>=1.43.118`. On the date above, the newest aioboto3 on the package index (15.5.0) pins aiobotocore 2.25.1, which requires boto3 below 1.40.62, and `uv pip compile` reports the pair as unsatisfiable. Choosing aioboto3 would hold boto3 back by about three minor versions and tie every later boto3 upgrade to aiobotocore's release schedule.
- The repo already runs a blocking SDK this way. The Docker sandbox manager calls the Docker SDK through `asyncio.to_thread`.
- The load does not need native async I/O. Payloads are kilobytes to a few megabytes, and a turn makes a handful of calls next to model latency measured in seconds.
- It needs no packages beyond boto3, which the project already depended on for its tests.

boto3 moves from the dev group to the runtime dependencies, and `types-boto3[s3]` joins the dev group so that mypy checks the calls.

## Consequences

**Easier**

- History stays small, so replay after a crash stays fast and Continue-As-New has headroom.
- The console and the run report read full content from blobs by key, and the Temporal UI stays readable.
- Every read by ref is verified, so a damaged, replaced or truncated object is an error and never bad input to the model. The one read without a ref is the handle index of an output (see LH-402 below), and the text it names is still checked.
- Unit tests use `MemoryBlobStore`, which applies the same key rules and the same checks as the S3 store.

**Harder**

- Every large payload costs a blob round trip. A blob store outage fails activities, which Temporal retries.
- The data is not in the history. A replay test or a chaos run needs the blob contents as well as the history.
- Blobs need a retention policy. This ADR does not define one, and nothing deletes blobs yet.
- Calls on the default thread pool cannot be cancelled once started. A cancelled activity leaves its S3 call running until it finishes or times out. Connect and read timeouts, set on the client, bound that.
- Retries overwrite keys, so activities must return the ref from the attempt that completed and must not reuse refs from an earlier attempt.

**To watch**

- The 256 KB cap is enforced twice. `GuardedPayloadConverter` refuses the payload on every encode (LH-292), so it never reaches the history. `assert_small` in the activity wrappers (LH-204) maps `PayloadTooLargeError` to a non-retryable activity error, since retrying cannot make a payload smaller. A process that connects with another data converter has only the second. A test server or a script has to use `guarded_data_converter` to get the first.
- A hint is cut to 2,000 bytes when its signal arrives (`agent/hints.py`, LH-506), with a note that says so. `llm_turn` still carries every hint of the task, so a very long task with very many hints would reach the cap in the end; the count of hints is not limited.
- The upload checksum needs a backend that supports the `x-amz-checksum-sha256` header. S3 and the MinIO build in the compose stack do. A different S3-compatible store has to be tried before it is used.
- The server limits quoted above are defaults. Check them against the Temporal version in use when it changes.

## Alternatives

- **aioboto3.** It gives native async I/O and real cancellation, but it does not resolve with the boto3 the project already pins (see Decision), and the gain is not needed at this load.
- **Pass payloads inline and raise the server limits.** Dynamic config can raise the payload limit, but history size and replay time keep growing, so the problem only moves later.
- **A Temporal payload codec that compresses.** Compression shrinks payloads by a constant factor and leaves the linear growth. It also hides content from the Temporal UI. It could be added later as a second layer.
- **Guard in a `PayloadCodec`.** It sees the same bytes, but it runs after the workflow code, over the completion of the whole task. Its error fails the workflow task and cannot fail the workflow, so a run would retry the same task for as long as it stays open. The payload converter raises inside the call that builds the payload.
- **Postgres large objects or `bytea`.** One service fewer, but large writes would sit in the database that holds task state and the cost ledger, and the AWS deployment would not use the same API as the local stack.
- **A shared volume or local disk.** Activities can run on any worker, so this ties a task to one host and defeats recovery after a host is lost (failure mode F4).
- **Content-addressed keys, where the key is the hash.** This removes duplicates, but keys become opaque to people debugging a run. Readable keys plus a hash in the ref give the same integrity guarantee.

## Appendix: how LH-204 applies it

The LLM and tool activities were the first code to put the rule into practice. These are the choices it made, with the numbers in `longhaul.durable.acts`.

- **Tool arguments.** A call's arguments travel inline up to 16,000 bytes. A whole model reply may keep 96,000 bytes of arguments inline, because twelve calls of 10 KB are each under the limit and together fill half of the 256 KB cap. A call over either limit is stored and named by `args_ref`. The full list of calls is stored too, as `tool_calls_ref`.
- **Tool output.** `tool_exec` stores the full text of the output and returns a `ToolResult` with a preview of 2,000 characters in `output` and the blob in `output_ref`. The preview is for the Temporal UI. `append_results` takes one `ToolResultSummary` per result that names the blob, so a turn of fifty calls with 15 KB of output each adds a few KB to the history and not 750 KB. A result that no tool made, such as the answer to a `finish` without a summary, carries its short text inline.
- **Output handles (LH-402).** `append_results` reads the blob and gives the model the whole text when it is within `tool_output_max_tokens`. A longer output becomes its first 60 lines, its last 60 lines and a marker with the handle `out_<call id>`. The cut is made there because it is a plain function of the stored text and the cap, so a retry builds the same transcript. `tool_exec` also stores a small index blob at a key made from the task and the call (`blobs.base.handle_key`), which holds the `BlobRef` of the text. `read_output` reads the index by key (`BlobStoreBase.get_by_key`, the one read that has no ref to check) and then the text by its ref, which is checked. A handle therefore resolves from the blob store alone, without the database and without knowing which turn made the call. The registry that holds `read_output` has to be built on the same store as the tool activities, and `ToolActivities` refuses one that is not, because with two stores every handle would be unknown.
- **The guard.** Every activity input and output goes through `support.small`, which calls `assert_small` and turns `PayloadTooLargeError` into a non-retryable `ApplicationError`. The retry policies also list the error type, as a second guard. Since LH-292 this is the second layer: the data converter refuses an oversized input before the activity is scheduled, and `support.small` still checks every input and output inside the activity, so a mistake there fails that activity for good.
- **Blob keys of a model call.** A turn can be asked more than once (the retry after `max_tokens`, a continued `pause_turn`, a Temporal retry), so the response and the transcript are stored under `response-<attempt>` and `transcript-<attempt>`. A `turns` row keeps the key of its response, and a later call does not overwrite it. The attempt number is one more than the rows of the turn, so a key is written again only by the retry of a call that left no row, and then no ref to the earlier content exists.
- **The task statement.** `LlmTurnInput` carries the `TaskSpec`, because the activity builds the system prompt from it on every call. The issue body and the comments have no size limit yet. LH-303 adds one, as the deferred contract change in `docs/architecture.md` says.
